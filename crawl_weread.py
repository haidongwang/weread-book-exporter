#!/usr/bin/env python3
"""WeRead Full-Book Crawler (crawl_weread.py)

核心特性：
1. 自动切换至双栏水平翻页模式，解决网页端上下滚动模式下因虚拟滚动分片导致章节下半部分漏字的问题。
2. 捕获 Canvas 2D 绘制指令与分栏坐标，精准切分每一单页，消除跨页文字重复或交错。
3. 基于官方目录（_catalog.json）精确反推章节分界，支持标题粘连自动剥离，杜绝单章塌陷。
4. 基于 Cookie (wr_vid/wr_skey) 精准鉴权，支持 300 秒扫码等待。
5. 逐页实时落盘（断点保护），每页输出详细抓取进度。
6. 自动抓取书籍元数据（书名、作者、简介）并下载原版高清封面（cover.jpg）。
7. 智能适配系统内置 Edge / Chrome，免去下载 200MB 浏览器的漫长等待。
"""
import asyncio
import collections
import json
import hashlib
from html import escape
import os
import random
import re
import sys
import time
import urllib.request

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.environ.get("WEREAD_PROFILE", os.path.join(BASE, "cache", "browser_profile"))

# 翻页节奏配置：温和防风控
PAGE_DELAY_RANGE = (8.0, 20.0)       # 每次翻页后的随机等待（秒）
LONG_PAUSE_EVERY = (10, 20)          # 每隔 10~20 页触发一次长停顿
LONG_PAUSE_RANGE = (60.0, 120.0)     # 长停顿时长（秒）


def next_long_pause_at(step):
    return step + random.randint(*LONG_PAUSE_EVERY)


def get_crawl_state(book_dir):
    sf = os.path.join(book_dir, "_state.json")
    if os.path.exists(sf):
        try:
            with open(sf, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed": False, "saved_chapter_count": 0, "last_step": 0, "total_words": 0}


def save_crawl_state(book_dir, state):
    sf = os.path.join(book_dir, "_state.json")
    try:
        with open(sf, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存状态失败: {e}")


def rebuild_chapters_from_raw(book_id):
    """
    从 raw/page_*.json 中恢复并重新生成所有章节 chapters/*.md 与 raw/chap_*.json
    即使爬取过程异常终止，也可完整抢救出所有已捕获的章节内容。
    """
    book_dir = os.path.join(BASE, "output", book_id + "_v2")
    raw_dir = os.path.join(book_dir, "raw")
    md_dir = os.path.join(book_dir, "chapters")
    os.makedirs(md_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    if not os.path.isdir(raw_dir):
        return 0

    page_files = sorted([f for f in os.listdir(raw_dir) if f.startswith("page_") and f.endswith(".json")])
    if not page_files:
        return 0

    sections = []
    cur_title = None
    cur_blocks = []

    for pf in page_files:
        fp = os.path.join(raw_dir, pf)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                pdata = json.load(f)
        except Exception:
            continue

        page_ch = pdata.get("chapter") or "扉页"
        pblocks = pdata.get("blocks")
        if not pblocks:
            pblocks = []
            for ln in pdata.get("lines", []):
                pblocks.append({"type": "line", "cls": "js", "text": ln})
            for im in pdata.get("images", []):
                pblocks.append({"type": "img", "src": im.get("src", "")})

        if cur_title is None:
            cur_title = page_ch
            cur_blocks = list(pblocks)
        elif norm(page_ch) == norm(cur_title):
            cur_blocks.extend(pblocks)
        else:
            if cur_blocks:
                sections.append((cur_title, cur_blocks))
            cur_title = page_ch
            cur_blocks = list(pblocks)

    if cur_title and cur_blocks:
        sections.append((cur_title, cur_blocks))

    # 合并同名相邻章节
    merged = []
    for t, b in sections:
        if merged and norm(merged[-1][0]) == norm(t):
            merged[-1][1].extend(b)
        else:
            merged.append((t, b))

    saved_idx = 0
    total_words = 0
    for t, blocks in merged:
        saved_idx += 1
        body, im_list = render_md(t, blocks, saved_idx)
        txt_len = len(re.sub(r"<[^>]+>", "", body))
        with open(os.path.join(md_dir, "%04d.md" % saved_idx), "w", encoding="utf-8") as f:
            f.write(body)
        with open(os.path.join(raw_dir, "chap_%04d.json" % saved_idx), "w", encoding="utf-8") as f:
            json.dump({"title": t, "images": im_list, "text_len": txt_len}, f, ensure_ascii=False)
        total_words += txt_len

    save_crawl_state(book_dir, {
        "book_id": book_id,
        "completed": False,
        "last_step": len(page_files),
        "saved_chapter_count": saved_idx,
        "last_title": cur_title or "",
        "total_words": total_words,
    })
    return saved_idx


def download_all_images(raw_dir, img_dir):
    """统一检查并补全下载所有章节配图"""
    os.makedirs(img_dir, exist_ok=True)
    tasks = []
    if os.path.isdir(raw_dir):
        for jf in sorted(os.listdir(raw_dir)):
            if jf.startswith("chap_") and jf.endswith(".json"):
                try:
                    with open(os.path.join(raw_dir, jf), encoding="utf-8") as f:
                        for im in json.load(f).get("images", []):
                            tasks.append((im["src"], im.get("name", "")))
                except Exception:
                    pass

    tasks = dict.fromkeys(tasks)
    ok_img = 0
    for url, name in tasks:
        if not name:
            ext = "jpg"
            m = re.search(r"\.(jpg|jpeg|png|gif|webp)", url.lower())
            if m:
                ext = m.group(1).replace("jpeg", "jpg")
            name = f"img_{abs(hash(url)) % 1000000:06d}.{ext}"
        fp = os.path.join(img_dir, name)
        if os.path.exists(fp) and os.path.getsize(fp) > 500:
            ok_img += 1
            continue
        try:
            req = urllib.request.Request(url, headers={"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                with open(fp, "wb") as f:
                    f.write(raw)
                ok_img += 1
        except Exception:
            pass
    if tasks:
        print(f"🖼️ 章节配图就绪: {ok_img}/{len(tasks)}", flush=True)


def extract_book_id(raw_input):
    """从纯 ID、完整网页链接或带参数的分享链接中智能提取书籍 ID"""
    if not raw_input:
        return ""
    text = str(raw_input).strip()
    m = re.search(r"(?:reader|bookDetail)/([a-zA-Z0-9_-]+)", text)
    if m:
        return m.group(1)
    m_id = re.search(r"^[a-zA-Z0-9_-]{6,36}$", text)
    if m_id:
        return m_id.group(0)
    clean = re.sub(r"^https?://[^/]+/", "", text).split("?")[0].strip("/")
    return clean or text


HOOK = """
(function() {
    window.__wr_chars = [];
    window.__wr_seq = 0;
    var origClear = CanvasRenderingContext2D.prototype.clearRect;
    CanvasRenderingContext2D.prototype.clearRect = function(x, y, w, h) {
        if (w > 100 && h > 100) window.__wr_seq++;
        return origClear.apply(this, arguments);
    };
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (typeof text === 'string' && text.length) {
            var rect = this.canvas.getBoundingClientRect();
            var side = rect.left + rect.width / 2 >= window.innerWidth / 2 ? 1 : 0;
            const point = this.getTransform().transformPoint({x, y});
            const viewportY = rect.top + point.y * rect.height / (this.canvas.height || 1);
            const width = this.measureText(text).width;
            const align = this.textAlign;
            const rightAligned = align === 'right' || (align === 'end' && this.direction !== 'rtl')
                || (align === 'start' && this.direction === 'rtl');
            const textLeft = x - (align === 'center' ? width / 2 : rightAligned ? width : 0);
            window.__wr_chars.push({
                viewport_y: viewportY,
                text_left: textLeft,
                text_right: textLeft + width,
                text_align: align,
                t: text,
                x: Math.round(x * 10) / 10,
                y: Math.round(y * 10) / 10,
                f: String(this.font),
                side: side,
                seq: window.__wr_seq
            });
        }
        return origFill.apply(this, arguments);
    };
    window.__wr_drain = function() {
        var out = window.__wr_chars;
        window.__wr_chars = [];
        return out;
    };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""

IMGS_JS = """
() => {
    const H = window.innerHeight, W = window.innerWidth, out = [];
    document.querySelectorAll('img[class*="wr_readerImage"]').forEach(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        if (!src.includes('res.weread.qq.com/wrepub')) return;
        const r = i.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < H &&
            r.right > 0 && r.left < W &&
            getComputedStyle(i).visibility !== 'hidden' && getComputedStyle(i).display !== 'none')
            out.push({src, top: r.top, left: r.left, side: r.left + r.width / 2 >= W / 2 ? 1 : 0});
    });
    return out;
}
"""

SENT_END_RE = re.compile(r"[。！？；：…][”」）】》]*$")


def norm(s):
    return re.sub(r"[\s\u200b\ufeff\u3000\-_:：,，、.·]+", "", s or "")


def size_of(f):
    m = re.search(r"([\d.]+)px", f or "")
    return float(m.group(1)) if m else 0.0


def cls_of(f):
    s = size_of(f)
    if s >= 24.5:
        return "title"
    if s >= 21.0:
        return "yw"
    if "bold" in (f or ""):
        return "fang"
    return "js"


def split_pages(chars):
    """按 (seq, side) 以及 y 坐标回跳切分为独立单页"""
    real = []
    for c in chars:
        t = c["t"].replace("\u200b", "").replace("\ufeff", "")
        if not t:
            continue
        if t.startswith("abcdefg"):
            continue
        c = dict(c)
        c["t"] = t
        real.append(c)

    if not real:
        return []

    pages = []
    cur = []
    cur_key = None
    last_y = -1.0

    for c in real:
        key = (c["seq"], c["side"])
        if cur_key is None:
            cur_key = key
            cur = [c]
            last_y = c["y"]
            continue
        if key != cur_key or (len(c["t"]) == 1 and last_y > 250 and c["y"] < last_y - 80):
            if cur:
                pages.append((cur_key[1], cur))
            cur = [c]
            cur_key = key
            last_y = c["y"]
        else:
            cur.append(c)
            if len(c["t"]) == 1:
                last_y = c["y"]
    if cur:
        pages.append((cur_key[1], cur))
    # Canvas repaint order can differ from the reading order of the spread.
    return sorted(pages, key=lambda page: page[0])


def completes_heading_quote(title, continuation):
    return (
        title.count("“") == title.count("”") + 1
        and 1 < len(continuation) <= 8
        and continuation.endswith("”")
        and "“" not in continuation
    )


def heading_line_continues(previous, line):
    gap = line["y"] - previous["y"]
    if 0 < gap < 55:
        return True
    if not 0 < gap <= 1.7 * line["size"]:
        return False
    if abs(previous["x"] - line["x"]) > line["size"] / 2:
        return False

    left, right = previous["text"], line["text"]
    # A closing mark on the next line can complete an unfinished phrase.
    return any(
        left.count(opening) > left.count(closing)
        and right.count(closing) > right.count(opening)
        for opening, closing in (("“", "”"), ("‘", "’"), ("（", "）"), ("(", ")"), ("《", "》"), ("「", "」"))
    )


def mark_right_aligned_lines(lines):
    """Preserve isolated short lines anchored near the body column's right edge."""
    measured = [line for line in lines if line.get("has_bounds")]
    body = [line for line in measured if line["cls"] == "js" and len(line["text"]) >= 12]
    if len(body) < 2:
        return
    left_edge = min(line["left"] for line in body)
    right_edge = max(line["right"] for line in body)
    width = right_edge - left_edge
    if width <= 0:
        return
    full_lines = [line for line in body if line["right"] - line["left"] >= width * 0.7]
    if len(full_lines) < 2:
        return
    for index, line in enumerate(lines):
        if not line.get("has_bounds") or line["cls"] == "title":
            continue
        short = len(line["text"]) <= 24 and line["right"] - line["left"] <= width * 0.4
        anchored = (line["left"] >= left_edge + width * 0.55
                    and abs(right_edge - line["right"]) <= line["size"] * 1.5)
        neighbors = lines[max(0, index - 1):index] + lines[index + 1:index + 2]
        separated = all(
            abs(other.get("y", other["viewport_y"]) - line.get("y", line["viewport_y"])) >= line["size"] * 1.5
            for other in neighbors
        )
        if short and anchored and (separated or line.get("align") in ("right", "end")):
            line["alignment"] = "right"


def page_lines(chars):
    rows = {}
    for c in chars:
        rows.setdefault(round(c["y"] / 3) * 3, []).append(c)
    raw_lines = []
    for yk in sorted(rows):
        row = sorted(rows[yk], key=lambda c: c["x"])
        text = "".join(c["t"] for c in row).strip()
        if not text:
            continue
        f = collections.Counter(c["f"] for c in row).most_common(1)[0][0]
        raw_lines.append({
            "x": row[0]["x"], "y": yk, "text": text,
            "viewport_y": row[0].get("viewport_y", yk),
            "left": min(c.get("text_left", c["x"]) for c in row),
            "right": max(c.get("text_right", c["x"]) for c in row),
            "align": collections.Counter(c.get("text_align", "") for c in row).most_common(1)[0][0],
            "has_bounds": all("text_right" in c for c in row),
            "cls": cls_of(f), "size": round(size_of(f), 1),
        })

    out = []
    for ln in raw_lines:
        if (out and out[-1]["cls"] == "title" and ln["cls"] == "title"
                and out[-1]["size"] == ln["size"]
                and heading_line_continues(out[-1], ln)):
            out[-1]["text"] += wrapped_line_separator(out[-1]["text"], ln["text"]) + ln["text"]
            out[-1]["y"] = ln["y"]
        else:
            out.append(ln)
    mark_right_aligned_lines(out)
    return out


def copyright_layout(lines):
    """Require measured text bounds before interpreting alignment."""
    rows = [line for line in lines if line.get("has_bounds") and len(line["text"]) >= 4]
    if len(rows) < 3:
        return None
    tolerance = max(line["size"] for line in rows) * 0.6
    centers = [(line["left"] + line["right"]) / 2 for line in rows]
    starts = [line["left"] for line in rows]
    if (all(line["align"] == "center" for line in rows)
            or (max(centers) - min(centers) <= tolerance
                and max(starts) - min(starts) > tolerance * 2)):
        return "center"
    # Paragraphs may indent their first line. Look for repeated left edges
    # with varying line lengths, including short lines at paragraph ends.
    aligned = max(sum(abs(x - other) <= tolerance for other in starts) for x in starts)
    widths = [line["right"] - line["left"] for line in rows]
    if aligned >= 3 and max(widths) - min(widths) > tolerance * 2:
        return "left"
    return None


def copyright_body_line(lines, centered_copyright):
    """Find an aligned body after a measured, centered copyright prefix."""
    for index in range(len(lines)):
        prefix = lines[:index]
        following = lines[index:index + 6]
        if not (centered_copyright or copyright_layout(prefix) == "center"):
            continue
        if len(following) < 3 or copyright_layout(following) != "left":
            continue
        first_rows = following[:3]
        if not all(row.get("has_bounds") for row in first_rows):
            continue
        tolerance = max(row["size"] for row in first_rows) * 0.6
        starts = [row["left"] for row in first_rows]
        if max(starts) - min(starts) <= tolerance:
            return lines[index]
    return None


def untitled_front_boundary(lines, images, centered_copyright):
    if not centered_copyright or copyright_layout(lines) != "left" or not images:
        return None
    first_y = min(line["viewport_y"] for line in lines)
    preceding = [image["top"] for image in images if image["top"] < first_y]
    return min(preceding) if preceding else None


def ordered_page_content(lines, images):
    items = [(line["viewport_y"], False, line) for line in lines]
    items.extend((image["top"], True, image) for image in images)
    return [(is_image, item) for _, is_image, item in sorted(items, key=lambda item: item[0])]


def match_catalog_title(text, catalog, seen_titles):
    """
    基于官方目录精确反推章节标题（最高优先级）：
    对比正文行是否命中尚未出现过的目录条目，支持标题与段首相连时的自动剥离。
    返回: (匹配到的标题, 剥离标题后剩余的正文内容) 或 (None, text)
    """
    clean_text = norm(text)
    if not clean_text or len(clean_text) < 2:
        return None, text

    for cat in catalog:
        if cat in seen_titles:
            continue
        clean_cat = norm(cat)
        if not clean_cat or len(clean_cat) < 2:
            continue

        # 1. 独立一行完全匹配目录标题
        if clean_text == clean_cat:
            return cat, ""

        # 2. 标题在行首，且整行长度很短（<= 目录长度 + 4 个字）
        if clean_text.startswith(clean_cat) and len(clean_text) <= len(clean_cat) + 4:
            return cat, ""

        # 3. 标题与首段正文粘连（例如：'第2章 般若品第二次日，韦使君请益...'）
        if clean_text.startswith(clean_cat) and len(clean_cat) >= 3:
            # 找到原文本中标题结束的位置
            # 用原字符逐个匹配
            c_idx = 0
            t_idx = 0
            while c_idx < len(cat) and t_idx < len(text):
                if norm(text[t_idx]) == norm(cat[c_idx]):
                    c_idx += 1
                t_idx += 1
            if c_idx >= len(cat) * 0.8:
                remainder = text[t_idx:].lstrip("：:、.· 　")
                return cat, remainder

    return None, text


def wrapped_line_separator(left, right):
    """Restore a word separator at an English line wrap."""
    left = left.rstrip("*")
    right = right.lstrip("*")
    if re.search(r"[A-Za-z0-9][,.!?;:)]?$", left) and re.match(r"[A-Za-z0-9]", right):
        return " "
    return ""


def render_md(title, blocks, ch_idx):
    out = ["# %s\n" % title]
    buf, buf_cls = [], None

    def flush():
        nonlocal buf, buf_cls
        if not buf:
            buf_cls = None
            return
        if title.strip() == "版权信息":
            for line in buf:
                out.extend((line, ""))
        elif buf_cls == "js":
            merged = []
            for line in buf:
                if merged and merged[-1] and not SENT_END_RE.search(merged[-1]):
                    merged[-1] += wrapped_line_separator(merged[-1], line) + line
                else:
                    merged.append(line)
            for m in merged:
                if m.strip():
                    out.append(m.strip())
                    out.append("")
        elif buf_cls == "yw":
            joined = buf[0]
            for line in buf[1:]:
                joined += wrapped_line_separator(joined, line) + line
            out.append('<p class="yw">%s</p>' % joined.strip())
            out.append("")
        elif buf_cls == "fang":
            for line in buf:
                if line.strip():
                    out.append('<p class="fang">%s</p>' % line.strip())
                    out.append("")
        buf, buf_cls = [], None

    imgs = []
    for b in blocks:
        if b["type"] == "img":
            flush()
            ext = "jpg"
            m = re.search(r"\.(jpg|jpeg|png|gif|webp)", b["src"].lower())
            if m:
                ext = m.group(1).replace("jpeg", "jpg")
            digest = hashlib.sha256(b["src"].encode("utf-8")).hexdigest()
            name = f"img_{digest}.{ext}"
            out.append("![图](images/%s)\n" % name)
            imgs.append({"src": b["src"], "name": name})
            continue
        if b.get("alignment") == "right":
            flush()
            out.extend(('<p class="right">%s</p>' % escape(b["text"]), ""))
            continue
        if b["cls"] != buf_cls:
            flush()
            buf_cls = b["cls"]
        buf.append(b["text"])
    flush()
    return "\n".join(out).rstrip() + "\n", imgs


async def wait_stable_chars(page, timeout=6.0):
    t0 = time.time()
    last_n = 0
    stable_ticks = 0
    while time.time() - t0 < timeout:
        try:
            n = await page.evaluate("() => window.__wr_count()")
        except Exception:
            n = 0
        if n > 0:
            if n == last_n:
                stable_ticks += 1
                if stable_ticks >= 2:
                    return n
            else:
                last_n = n
                stable_ticks = 0
        await asyncio.sleep(0.18)
    return last_n


async def check_cookies_login(ctx):
    """基于真实鉴权凭证检查登录态，彻底避免访客目录误判"""
    try:
        cookies = await ctx.cookies()
        names = {c["name"] for c in cookies}
        return "wr_vid" in names and ("wr_skey" in names or "wr_rt" in names)
    except Exception:
        return False


async def launch_browser_context(p, user_data_dir, headless=False):
    """自动检测可用浏览器：优先系统自带 Edge/Chrome，免下载 200MB 浏览器"""
    custom_ch = os.environ.get("PLAYWRIGHT_CHANNEL")
    candidates = []
    if custom_ch:
        candidates.append({"name": f"自定义 {custom_ch}", "channel": custom_ch})
    candidates.extend([
        {"name": "系统 Microsoft Edge", "channel": "msedge"},
        {"name": "系统 Google Chrome", "channel": "chrome"},
        {"name": "Playwright Chromium", "channel": None},
    ])
    last_err = None
    for c in candidates:
        try:
            kwargs = {
                "headless": headless,
                "viewport": {"width": 1200, "height": 900},
                "args": ["--disable-blink-features=AutomationControlled"]
            }
            if c["channel"]:
                kwargs["channel"] = c["channel"]
            ctx = await p.chromium.launch_persistent_context(user_data_dir, **kwargs)
            return ctx
        except Exception as e:
            last_err = e
            err_msg = str(e)
            if "ProcessSingleton" in err_msg or "Lock file" in err_msg:
                raise RuntimeError("检测到浏览器配置目录被占用！请先关闭其他正在运行的微信读书导出窗口或浏览器后台进程后重试。")
            continue
    raise RuntimeError(f"未能启动浏览器 ({last_err})。请安装 Google Chrome 或 Microsoft Edge，或执行: playwright install chromium")


def conflicting_metadata(book_meta, output_dir):
    """Identify cover or introduction already associated with another cached title."""
    for name in os.listdir(output_dir):
        path = os.path.join(output_dir, name, "meta.json")
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            continue
        if not cached.get("title") or norm(cached["title"]) == norm(book_meta["title"]):
            continue
        if any(book_meta.get(key) and book_meta[key] == cached.get(key) for key in ("cover", "intro")):
            return cached["title"]
    return ""


async def read_front_page_metadata(page, title):
    """Read the visible title spread before the crawler navigates to the body."""
    if not title:
        return {}
    try:
        await page.click("button.readerControls_item.catalog", timeout=5000)
        entry = page.locator(".readerCatalog_list_item").filter(has_text=re.compile(r"^\s*扉页(?:\s*当前读到.*)?\s*$"))
        if await entry.count() != 1:
            await page.click("button.readerControls_item.catalog", timeout=3000)
            return {}
        await entry.click(timeout=5000)
        result = await page.wait_for_function("""title => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width && r.height && r.bottom > 0 && r.top < innerHeight
                    && getComputedStyle(el).visibility === 'visible';
            };
            const midpoint = innerWidth / 2;
            const heading = [...document.querySelectorAll('h1,h2,h3,p,div,span')].find(el =>
                !el.children.length && el.textContent.trim() === title && visible(el)
                && el.getBoundingClientRect().right < midpoint
                && el.getBoundingClientRect().top > 80);
            if (!heading) return null;
            const h = heading.getBoundingClientRect();
            const cover = [...document.images].find(img => {
                const r = img.getBoundingClientRect();
                return visible(img) && r.width >= 70 && r.height > r.width
                    && r.bottom <= h.top && r.left <= (h.left + h.right) / 2
                    && r.right >= (h.left + h.right) / 2 && img.complete && img.naturalWidth;
            });
            const candidates = [...document.querySelectorAll('p,div,section')].filter(el => {
                const r = el.getBoundingClientRect();
                const text = (el.innerText || '').trim();
                return r.left >= midpoint && r.width >= 150 && visible(el)
                    && text.length >= 50 && /[。！？]/.test(text)
                    && !el.querySelector('button,a,canvas,img');
            });
            const intro = candidates.find(el => !candidates.some(other => other.contains(el) && other !== el));
            return cover && intro ? {
                cover: cover.currentSrc || cover.src,
                intro: intro.innerText.trim(),
            } : null;
        }""", arg=title, timeout=7000)
        return await result.json_value()
    except PlaywrightTimeoutError:
        return {}
    finally:
        if await page.locator(".readerCatalog_list_item").first.is_visible():
            await page.keyboard.press("Escape")
            if await page.locator(".readerCatalog_list_item").first.is_visible():
                await page.click("button.readerControls_item.catalog", timeout=3000)


def advance_catalog(catalog, index, text, style):
    """Advance one entry in reading order, including repeated labels."""
    next_index = index + 1
    if next_index >= len(catalog):
        return index
    key = norm(text)
    if key == norm(catalog[next_index]) and (style == "title" or len(key) >= 2):
        return next_index
    return index


async def main(book_id_or_url, force=False, metadata_only=False):
    book_id = extract_book_id(book_id_or_url)
    if not book_id:
        print("❌ 错误: 未能解析出有效的微信读书书籍 ID 或链接")
        return False

    book_dir = os.path.join(BASE, "output", book_id + "_v2")
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")

    os.makedirs(md_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    state = get_crawl_state(book_dir)
    existing_mds = sorted([f for f in os.listdir(md_dir) if f.endswith(".md")])
    raw_pages = sorted([f for f in os.listdir(raw_dir) if f.startswith("page_") and f.endswith(".json")])

    # 1. 自愈检查：若存在 raw 页面落盘但 chapters 为空，自动重构
    if not existing_mds and raw_pages:
        print(f"🔄 检测到存在历史逐页落盘数据 ({len(raw_pages)} 页)，正在恢复章节数据...")
        rebuild_chapters_from_raw(book_id)
        existing_mds = sorted([f for f in os.listdir(md_dir) if f.endswith(".md")])
        state = get_crawl_state(book_dir)

    # 2. 全书已完成抓取且未指定 --force 时，直接复用已有数据
    if state.get("completed") and existing_mds and not force and not metadata_only:
        print(f"📖 检测到书籍《{book_id}》此前已完整抓取（共 {len(existing_mds)} 章）。")
        print("⏩ 自动跳过重复抓取，直接复用已有数据进入后续多格式导出流程。")
        print("💡 如需强制重新完整抓取，可在命令行传入 --force 参数。\n")
        download_all_images(raw_dir, img_dir)
        return True

    print(f"📖 目标书籍 ID: {book_id}")

    async with async_playwright() as p:
        ctx = await launch_browser_context(p, USER_DATA_DIR, headless=False)
        page = await ctx.new_page()
        if not metadata_only:
            await page.add_init_script(HOOK)

        target_url = f"https://weread.qq.com/web/reader/{book_id}"
        print(f"🌐 正在打开书籍阅读页: {target_url} ...", flush=True)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        # 登录判定：严格使用 Cookie 凭证
        if not await check_cookies_login(ctx):
            print("\n" + "="*55)
            print("👉 检测到未登录或登录已过期，请在浏览器中使用【微信扫码】（限时 300 秒）...")
            print("="*55 + "\n", flush=True)
            for sec in range(1, 151):
                await asyncio.sleep(2)
                if await check_cookies_login(ctx):
                    print("✅ 登录成功，继续执行抓取...", flush=True)
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                    break
            else:
                print("❌ 扫码登录超时（300秒），导出终止。请重新运行并在弹出的窗口中扫码！", flush=True)
                await ctx.close()
                return False

        # 抓取完整的书籍元数据与原版高清封面
        book_meta = await page.evaluate("""() => {
            let info = (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.reader && window.__INITIAL_STATE__.reader.bookInfo) || {};
            return {
                title: info.title || document.title.replace(' - 微信读书', '').trim(),
                author: info.author || '',
                cover: info.cover || '',
                intro: info.intro || '',
                publisher: info.publisher || ''
            };
        }""")
        # 确保处于双栏水平翻页模式
        if await page.locator("button.readerControls_item.isNormalReader").count() > 0:
            print("🔄 切换至双栏翻页模式...", flush=True)
            await page.click("button.readerControls_item.isNormalReader")
            await asyncio.sleep(3)

        front_page = await read_front_page_metadata(page, book_meta["title"])
        if front_page:
            book_meta.update(front_page)
            print("📖 已读取扉页显示的封面和简介。", flush=True)
        else:
            print("⚠️ 未能从扉页确认封面和简介，继续校验网页元数据。", flush=True)

        book_meta["book_id"] = book_id
        conflict = conflicting_metadata(book_meta, os.path.join(BASE, "output"))
        if conflict:
            print(f"⚠️ 封面或简介与另一册书《{conflict}》相同，本次省略这两项。", flush=True)
            book_meta.update(cover="", intro="")

        print(f"📚 书名: {book_meta.get('title')}")
        if book_meta.get("author"):
            print(f"✍️ 作者: {book_meta.get('author')}")

        cover_url = book_meta.get("cover")
        cover_saved = False
        if cover_url and not conflict:
            cover_path = os.path.join(book_dir, "cover.jpg")
            try:
                req = urllib.request.Request(cover_url, headers={"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=15).read()
                if len(raw) > 500:
                    with open(cover_path, "wb") as f:
                        f.write(raw)
                    cover_saved = True
                    print("🖼️ 原版高清封面下载完成")
            except Exception as e:
                print(f"⚠️ 封面下载跳过: {e}")

        book_meta["cover_available"] = cover_saved
        meta_file = os.path.join(book_dir, "meta.json")
        with open(meta_file + ".tmp", "w", encoding="utf-8") as f:
            json.dump(book_meta, f, ensure_ascii=False, indent=2)
        os.replace(meta_file + ".tmp", meta_file)

        if metadata_only:
            await ctx.close()
            if not cover_saved or not book_meta.get("intro"):
                print("⚠️ 刷新未取得完整封面和简介。")
                return False
            print("✅ 封面和简介已刷新，可以重新导出。")
            return True

        # 读取完整目录列表
        await page.click("button.readerControls_item.catalog", timeout=8000)
        await asyncio.sleep(1.5)
        items = page.locator(".readerCatalog_list_item")
        n = await items.count()
        catalog = []
        for i in range(n):
            txt = (await items.nth(i).inner_text()).strip().replace("\n", " ")
            txt = re.sub(r"\s*当前读到.*$", "", txt).strip()
            if txt:
                catalog.append(txt)
        with open(os.path.join(book_dir, "_catalog.json"), "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
        print(f"📑 目录共 {n} 项: {catalog[:3]} ... {catalog[-2:] if n > 3 else ''}", flush=True)

        # 断点续传智能计算：匹配已完成的章节
        existing_chap_files = sorted([f for f in os.listdir(raw_dir) if f.startswith("chap_") and f.endswith(".json")])
        completed_titles = []
        for cf in existing_chap_files:
            try:
                with open(os.path.join(raw_dir, cf), encoding="utf-8") as f:
                    cdata = json.load(f)
                    if cdata.get("title"):
                        completed_titles.append(cdata["title"])
            except Exception:
                pass

        if completed_titles and len({norm(label) for label in catalog}) != len(catalog):
            print("⚠️ 目录有同名条目，无法从旧章节标题安全定位。请先备份书籍缓存目录，再重新抓取。")
            await ctx.close()
            return False

        resume_cat_idx = 0
        if completed_titles and catalog:
            last_done_norm = norm(completed_titles[-1])
            for ci, cat in enumerate(catalog):
                if norm(cat) == last_done_norm or last_done_norm.startswith(norm(cat)):
                    resume_cat_idx = ci + 1
                    break
            if resume_cat_idx >= n:
                print(f"🎉 目录所有 {n} 个章节此前均已全部抓取完成！")
                save_crawl_state(book_dir, {
                    "book_id": book_id,
                    "completed": True,
                    "saved_chapter_count": len(completed_titles),
                    "total_words": state.get("total_words", 0)
                })
                await ctx.close()
                download_all_images(raw_dir, img_dir)
                return True

        if resume_cat_idx > 0 and n > 0:
            print("\n" + "="*55)
            print(f"🔄 检测到未完成进度，执行【真·断点续传】：")
            print(f"   - 历史已完成: {len(completed_titles)} 卷/章 (已落盘至 chapters/ 目录)")
            print(f"   - 续跑跳转点: 目录第 {resume_cat_idx + 1}/{n} 项《{catalog[resume_cat_idx]}》")
            print("="*55 + "\n", flush=True)
            nav_idx = min(resume_cat_idx, n - 1)
        else:
            nav_idx = 0

        # 平稳导航至目标章节（首章或续跑断点）
        if n > 0:
            it = items.nth(nav_idx)
            await it.scroll_into_view_if_needed()
            await asyncio.sleep(0.3)
            await page.evaluate("() => window.__wr_drain()")
            await it.click(timeout=5000)
            await asyncio.sleep(1.0)
            try:
                if await page.locator(".readerCatalog_list_item").first.is_visible():
                    await page.click("button.readerControls_item.catalog", timeout=2000)
            except Exception:
                await page.keyboard.press("Escape")
            await asyncio.sleep(1.0)

        # 等待首屏渲染稳定后再清空/重置缓冲区，确保首页 100% 捕获
        await wait_stable_chars(page, timeout=5.0)

        cur_title = catalog[nav_idx] if catalog and nav_idx < len(catalog) else "扉页"
        catalog_keys = {norm(title) for title in catalog}
        seen_titles = set(completed_titles + [cur_title])
        saved_idx = len(completed_titles)
        buf = []
        captured_heading = False
        stale = 0
        catalog_index = nav_idx
        crawl_completed = False
        seen_page_sigs = set()
        previous_spread = None
        centered_copyright = False

        raw_pages = sorted([f for f in os.listdir(raw_dir) if f.startswith("page_") and f.endswith(".json")])
        start_step = len(raw_pages) if resume_cat_idx > 0 else 0
        total_words_accum = state.get("total_words", 0) if resume_cat_idx > 0 else 0
        t_start = time.time()
        long_pause_at = next_long_pause_at(start_step)

        print(f"\n🚀 开始遍历抓取页面 (从第 {start_step} 页开始)...", flush=True)

        for step in range(start_step, start_step + 5000):
            if step > start_step:
                await page.mouse.click(600, 450)
                await page.keyboard.press("ArrowRight")
                delay = random.uniform(*PAGE_DELAY_RANGE)
                await asyncio.sleep(delay)
                if step >= long_pause_at:
                    pause = random.uniform(*LONG_PAUSE_RANGE)
                    print(f"  ⏸ 已抓取 {step} 页，触发温和长停顿 {pause:.0f} 秒...", flush=True)
                    await asyncio.sleep(pause)
                    long_pause_at = next_long_pause_at(step)

            ok = await wait_stable_chars(page, timeout=2.2 if step > start_step else 5.0)
            chars = await page.evaluate("() => window.__wr_drain()") if ok else []
            imgs = await page.evaluate(IMGS_JS)

            pages = split_pages(chars)
            got_any = False
            step_words = 0
            page_blocks = []
            lines_by_side = {0: [], 1: []}

            text_signatures = []
            for side, pchars in pages:
                lines = page_lines(pchars)
                if not lines:
                    continue
                sig = norm("".join(ln["text"] for ln in lines))
                text_signatures.append((side, sig))
                if len(sig) > 8 and sig in seen_page_sigs:
                    continue
                if len(sig) > 8:
                    seen_page_sigs.add(sig)
                lines_by_side[side].extend(lines)

            images_at_positions = {
                (im["side"], round(im["left"]), round(im["top"]), im["src"]): im for im in imgs
            }
            spread = (tuple(dict.fromkeys(text_signatures)), frozenset(images_at_positions))
            # A repeated rendering of the same spread does not add new images.
            if spread == previous_spread or (not text_signatures and previous_spread
                                             and spread[1] == previous_spread[1]):
                images_at_positions.clear()
            else:
                previous_spread = spread

            for side, lines in lines_by_side.items():
                side_images = [im for im in images_at_positions.values() if im["side"] == side]
                content = ordered_page_content(lines, side_images)
                # Inspect all lines before consuming the spread: the copyright
                # heading itself can occur later in this same batch.
                first_body = copyright_body_line(lines, centered_copyright)
                centered_copyright = centered_copyright or copyright_layout(lines) == "center"
                boundary = untitled_front_boundary(lines, side_images, centered_copyright)
                copyright_entries = [i for i, label in enumerate(catalog) if label == "版权信息"]
                if ((first_body is not None or boundary is not None)
                        and len(copyright_entries) == 1
                        and copyright_entries[0] + 1 < len(catalog)):
                    next_title = catalog[copyright_entries[0] + 1]
                    insertion = 0
                    if first_body is not None:
                        insertion = next(
                            i for i, (is_image, item) in enumerate(content)
                            if not is_image and item is first_body
                        )
                    content.insert(insertion, (False, {
                        "text": next_title, "cls": "title",
                        "copyright_boundary": copyright_entries[0] + 1,
                    }))
                for is_image, ln in content:
                    if is_image:
                        img_blk = {"type": "img", "src": ln["src"]}
                        buf.append(img_blk)
                        page_blocks.append(img_blk)
                        got_any = True
                        continue

                    if "copyright_boundary" in ln:
                        if cur_title != "版权信息":
                            continue
                        catalog_index = ln["copyright_boundary"] - 1
                        print(f"📐 检测到版权页结束，切换到目录项《{ln['text']}》。", flush=True)

                    got_any = True
                    txt = ln["text"].strip()
                    step_words += len(txt)

                    next_index = advance_catalog(catalog, catalog_index, txt, ln["cls"])
                    if next_index != catalog_index:
                        catalog_index = next_index
                        matched_title, remainder = catalog[catalog_index], ""
                    else:
                        matched_title, remainder = match_catalog_title(txt, catalog, seen_titles)
                        if (matched_title and catalog_index + 1 < len(catalog)
                                and matched_title == catalog[catalog_index + 1]):
                            catalog_index += 1

                    # A heading may span separate Canvas batches. Join it
                    # before the chapter boundary commits either fragment.
                    if (
                        captured_heading and not buf and ln["cls"] == "title"
                        and not matched_title
                        and norm(cur_title) not in catalog_keys
                        and norm(txt) not in catalog_keys
                        and completes_heading_quote(cur_title, txt)
                    ):
                        seen_titles.discard(cur_title)
                        cur_title += wrapped_line_separator(cur_title, txt) + txt
                        seen_titles.add(cur_title)
                        continue

                    is_new_chapter = False
                    new_title = ""
                    if matched_title and norm(matched_title) != norm(cur_title):
                        is_new_chapter = True
                        new_title = matched_title
                    elif ln["cls"] == "title" and norm(txt) != norm(cur_title) and len(txt) <= 40:
                        is_new_chapter = True
                        new_title = txt
                        # This whole line became the heading. A failed catalog
                        # match returns the original line as its remainder.
                        remainder = ""

                    if is_new_chapter:
                        if buf or captured_heading:
                            # 实时落盘刚完成的章节
                            saved_idx += 1
                            body, im_list = render_md(cur_title, buf, saved_idx)
                            txt_len = len(re.sub(r"<[^>]+>", "", body))
                            with open(os.path.join(md_dir, "%04d.md" % saved_idx), "w", encoding="utf-8") as f:
                                f.write(body)
                            with open(os.path.join(raw_dir, "chap_%04d.json" % saved_idx), "w", encoding="utf-8") as f:
                                json.dump({"title": cur_title, "images": im_list, "text_len": txt_len}, f, ensure_ascii=False)
                            print(f"\n  💾 [章节实时落盘] [{saved_idx:02d}] 《{cur_title}》已保存至 chapters/{saved_idx:04d}.md ({txt_len} 字)", flush=True)
                            save_crawl_state(book_dir, {
                                "book_id": book_id,
                                "completed": False,
                                "last_step": step,
                                "saved_chapter_count": saved_idx,
                                "last_title": cur_title,
                                "seen_titles": list(seen_titles),
                                "total_words": total_words_accum,
                            })
                        buf = []
                        cur_title = new_title
                        captured_heading = True
                        seen_titles.add(cur_title)
                        print(f"\n  📑 [第 {step:03d} 页] 识别新章节: 《{cur_title}》", flush=True)

                        if remainder:
                            rem_blk = {"type": "line", "cls": "js", "text": remainder}
                            buf.append(rem_blk)
                            page_blocks.append(rem_blk)
                        continue

                    if buf and buf[-1]["type"] == "line" and buf[-1]["text"] == txt and buf[-1]["cls"] == ln["cls"]:
                        continue

                    line_blk = {"type": "line", "cls": ln["cls"], "text": txt,
                                "layout": {key: ln[key] for key in
                                           ("left", "right", "viewport_y", "size", "align")
                                           if key in ln}, "side": side, "alignment": ln.get("alignment")}
                    buf.append(line_blk)
                    page_blocks.append(line_blk)

            total_words_accum += step_words

            # 🌟 逐页完整结构化落盘（真·断点保护）
            if got_any:
                page_file = os.path.join(raw_dir, f"page_{step:04d}.json")
                with open(page_file, "w", encoding="utf-8") as pf:
                    json.dump({
                        "step": step,
                        "chapter": cur_title,
                        "words": step_words,
                        "blocks": page_blocks,
                        "lines": [b["text"] for b in page_blocks if b.get("type") == "line"],
                    }, pf, ensure_ascii=False)

            # 实时可见的进度输出
            elapsed_sec = int(time.time() - t_start)
            elapsed_str = f"{elapsed_sec // 60}分{elapsed_sec % 60}秒"
            if got_any:
                stale = 0
                print(f"  [进度 第 {step:03d} 页] 本页 +{step_words:3d} 字 | 累计 {total_words_accum:6d} 字 | 所在: 《{cur_title[:12]}》 | 已用时 {elapsed_str}", flush=True)
            else:
                stale += 1
                if stale >= 8:
                    crawl_completed = bool(catalog) and catalog_index == len(catalog) - 1
                    if not crawl_completed:
                        print(f"⚠️ 连续 {stale} 次无新内容，尚未确认到达目录末项。", flush=True)
                    break

        if cur_title and (buf or captured_heading):
            saved_idx += 1
            body, im_list = render_md(cur_title, buf, saved_idx)
            txt_len = len(re.sub(r"<[^>]+>", "", body))
            with open(os.path.join(md_dir, "%04d.md" % saved_idx), "w", encoding="utf-8") as f:
                f.write(body)
            with open(os.path.join(raw_dir, "chap_%04d.json" % saved_idx), "w", encoding="utf-8") as f:
                json.dump({"title": cur_title, "images": im_list, "text_len": txt_len}, f, ensure_ascii=False)
            print(f"\n  💾 [末章落盘] [{saved_idx:02d}] 《{cur_title}》已写入 chapters/{saved_idx:04d}.md ({txt_len} 字)", flush=True)

        save_crawl_state(book_dir, {
            "book_id": book_id,
            "completed": crawl_completed,
            "last_step": step,
            "saved_chapter_count": saved_idx,
            "last_title": cur_title,
            "seen_titles": list(seen_titles),
            "total_words": total_words_accum,
        })

        print(f"\n" + "="*50)
        status = "抓取完成" if crawl_completed else "抓取未完成，已保留本地进度"
        print(f"{status}，已保存 {saved_idx} 个章节，累计 {total_words_accum} 字")
        print("="*50)
        await ctx.close()

    # 统一补齐配图下载
    download_all_images(raw_dir, img_dir)
    return crawl_completed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python crawl_weread.py <微信读书链接或ID> [--force] [--rebuild] [--metadata-only]")
        sys.exit(1)

    force_flag = "--force" in sys.argv or "-f" in sys.argv
    rebuild_flag = "--rebuild" in sys.argv

    bid = [a for a in sys.argv[1:] if not a.startswith("-")][0]
    extracted = extract_book_id(bid)

    if rebuild_flag:
        cnt = rebuild_chapters_from_raw(extracted)
        print(f"🎉 已成功从 raw/ 页面落盘数据恢复 {cnt} 个章节！")
    else:
        ok = asyncio.run(main(extracted, force=force_flag, metadata_only="--metadata-only" in sys.argv))
        sys.exit(0 if ok else 1)
