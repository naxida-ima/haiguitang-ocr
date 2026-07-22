#!/usr/bin/env python3
"""
Build Markdown + OCR Coherence Report from scanned Chinese PDF using PaddleOCR.
Usage:
    python3 build_md.py --pdf input.pdf --dpi 300 --out-dir .
Dependencies: paddlepaddle, paddleocr, pillow, jieba, poppler-utils (pdftoppm)
"""
import re, os, sys, glob, argparse, subprocess

# ========== CONSTANTS ==========
COVER = "许二木海龟汤合集\n作者/许二木 编者/长安\n"
OUT_MD = "许二木海龟汤合集.md"
OUT_DIAG = "OCR_不连贯标注.md"
SEASON_SIZES = [(1, 23), (2, 37), (3, 79), (4, 1)]

re_q = re.compile(r"[《«(]([^》»)]{1,30})[》»)]")
re_code = re.compile(
    r"(?:S[0-9]?\s*[1lI]?\s*|[0-9]\s*|规则怪谈\s*|灵之残响\s*|残响\s*)E\s*[0-9A-Za-z]{1,3}"
)
SOLF = re.compile(r"\b(do|re|mi|fa|sol|la|si|ti)\b", re.I)

def cjkc(s):
    return len(re.findall(r"[一-鿿]", s))

def is_footer(L):
    return bool(re.search(r"第\s*\d+\s*页", L))

def is_solfege(s):
    return bool(SOLF.search(s))

def is_gibberish(L):
    s = L.strip()
    if not s or not re.search(r"[A-Za-z]", s):
        return False
    if cjkc(s) >= 2:
        return False
    if is_solfege(s):
        return False
    alnum = re.sub(r"[^A-Za-z0-9]", "", s)
    if len(alnum) < 8:
        return False
    dens = len(re.findall(r"[scectSCECT]", alnum)) / len(alnum)
    return dens > 0.35 or len(alnum) > 18

def strip_edges(L):
    s = L
    m0 = re.search(r"[一-鿿]", s)
    if m0 and m0.start() > 0:
        lead = s[:m0.start()]
        if re.search(r"[A-Za-z]", lead) and len(lead.strip()) >= 2:
            s = s[m0.start():]
    mc = list(re.finditer(r"[一-鿿]", s))
    if mc:
        i1 = mc[-1].start()
        tail = s[i1+1:]
        if (re.search(r"[A-Za-z]", tail) and len(tail.strip()) >= 2
                and not re.search(r"[一-鿿]", tail)):
            s = s[:i1+1]
    return s

def clean_line(L):
    if is_footer(L):
        return ""
    if is_gibberish(L):
        return ""
    return strip_edges(L)

def assign_code(n):
    m = n
    for s, size in SEASON_SIZES:
        if m < size:
            return (str(s), f"{m + 1:02d}")
        m -= size
    return ("4", "01")

# ========== EASY OCR ==========
def ocr_all(pdf, work_dir, dpi):
    os.makedirs(work_dir, exist_ok=True)
    prefix = os.path.join(work_dir, "p")
    print(f"Rendering PDF to PNG at {dpi} DPI...")
    subprocess.run(
        ["pdftoppm", "-png", "-r", str(dpi), pdf, prefix],
        check=True
    )
    print("Importing EasyOCR (first run downloads models)...")
    import easyocr
    reader = easyocr.Reader(['ch_sim'], gpu=False)
    pngs = sorted(glob.glob(os.path.join(work_dir, "p-*.png")))
    pages = {}
    print(f"OCRing {len(pngs)} pages...")
    for idx, fpath in enumerate(pngs):
        n = int(re.search(r"(\d+)", os.path.basename(fpath)).group(1))
        result = reader.readtext(fpath)
        pages[n] = extract_page_text(result)
        if (idx + 1) % 20 == 0:
            print(f"  ... {idx+1}/{len(pngs)} done")
    return pages

def extract_page_text(blocks):
    """Convert EasyOCR blocks (sorted by position) into page text."""
    if not blocks:
        return ""
    items = []
    for bbox, text, conf in blocks:
        ys = [p[1] for p in bbox]
        xs = [p[0] for p in bbox]
        y_avg = sum(ys) / 4
        x_avg = sum(xs) / 4
        height = max(ys) - min(ys)
        items.append((y_avg, x_avg, height, text))
    items.sort(key=lambda x: (x[0], x[1]))
    lines = []
    cur_line = []
    prev_y = None
    for y, x, h, t in items:
        if prev_y is not None and abs(y - prev_y) > h * 0.6:
            if cur_line:
                cur_line.sort(key=lambda c: c[0])
                lines.append("".join(c[1] for c in cur_line))
                cur_line = []
        cur_line.append((x, t))
        prev_y = y
    if cur_line:
        cur_line.sort(key=lambda c: c[0])
        lines.append("".join(c[1] for c in cur_line))
    return "\n".join(lines)

# ========== TOC PAGE DETECTION ==========
def toc_line_count(t):
    cnt = 0
    for ln in t.split("\n"):
        s = ln.strip()
        if (re.search(r"S?\d*\s*E\s*\d+", s) and
            (re.search(r"[.．·…]{2,}", s) or re.search(r"\d{1,3}\s*$", s))):
            cnt += 1
    return cnt

# ========== TITLE DETECTION ==========
def has_tangmian(lines, i, k=6):
    N = len(lines)
    for j in range(i + 1, min(i + k + 1, N)):
        if "汤面" in lines[j]:
            return True
    return False

def looks_like_title(L):
    q = re_q.search(L)
    if not q:
        return False
    t = q.group(1)  # title content between 《》
    if cjkc(t) < 1 and len(re.sub(r"[^A-Za-z0-9]", "", t)) < 2:
        return False
    return bool(re_code.search(L))

# ========== COHERENCE ANALYSIS ==========
def analyze_coherence(puzzles, body):
    """Flag suspicious passages (likely OCR errors) in body text."""
    import jieba
    _ = jieba.lcut("初始化")  # warm up

    known_words = set(jieba.dt.FREQ.keys())
    dict_chars = set()
    for w in jieba.dt.FREQ:
        for c in w:
            if '\u4e00' <= c <= '\u9fff':
                dict_chars.add(c)

    # Common single-character function words - do NOT flag these
    common_ones = set(
        "的了是我在一不是有个人们这他她它那和就也都可对了"
        "会给于被把让向从到去来在以没上中下大小学年日时"
        "分秒月后前先次又再还已正只与或但而因所如为做"
        "能会要该应可必得看听说写读走跑飞等老"
        "点面边里外内间口手头身脚眼耳鼻舌牙"
        "色香味声光电气水火山石土木花草鸟鱼虫"
        "东南西北红黄蓝绿白黑金木"
    )

    # Build per-soup body text dictionary
    body_parts = {}
    puzz_idxs = sorted(p["idx"] for p in puzzles)
    for idx, p in enumerate(puzzles):
        start = p["idx"]
        end = puzz_idxs[idx + 1] if idx + 1 < len(puzz_idxs) else len(body)
        body_parts[idx] = body[start:end]

    # Build character frequency across all body text
    char_freq = {}
    for idx in range(len(puzzles)):
        for ln in body_parts[idx]:
            for c in ln:
                if '\u4e00' <= c <= '\u9fff':
                    char_freq[c] = char_freq.get(c, 0) + 1

    results = []
    for idx, p in enumerate(puzzles):
        flags = []
        for ln_rel, ln in enumerate(body_parts[idx]):
            s = ln.strip()
            if not s or cjkc(s) < 3:
                continue

            tokens = jieba.lcut(s)
            line_flags = []
            for t in tokens:
                t = t.strip()
                if not t:
                    continue

                if len(t) >= 2 and t not in known_words:
                    # Multi-char unknown token via HMM. Check if it contains
                    # characters that form unusual pairs
                    suspect = False
                    for i in range(len(t) - 1):
                        c1, c2 = t[i], t[i + 1]
                        if ('\u4e00' <= c1 <= '\u9fff' and
                            '\u4e00' <= c2 <= '\u9fff'):
                            # Check if this character pair appears in a known word
                            pair_in_any = False
                            for w in known_words:
                                if c1 + c2 in w:
                                    pair_in_any = True
                                    break
                            if not pair_in_any:
                                suspect = True
                                break
                    if suspect:
                        line_flags.append(t)

                elif len(t) == 1 and '\u4e00' <= t <= '\u9fff':
                    is_rare = t not in dict_chars
                    is_very_rare_in_doc = char_freq.get(t, 0) <= 2 and t not in common_ones
                    if is_rare or is_very_rare_in_doc:
                        line_flags.append(t)

            if line_flags and ln_rel > 0:  # skip header line
                flags.append((s[:120], line_flags))

        if flags:
            results.append({
                "season": p["season"],
                "ep": p["ep"],
                "title": p["title"],
                "flags": flags
            })
    return results

def build_diag_md(results):
    parts = []
    parts.append("# OCR 不连贯标注\n")
    parts.append("> 以下标出了经 PaddleOCR 识别后，正文中**读起来不自然**的段落（可疑的 OCR 识别错误）。")
    parts.append("> 格式：原文 → `「可疑词」` 为标记出的片段，请对照原书手动矫正。\n")
    total = 0
    for r in results:
        parts.append(f"### S{r['season']}E{r['ep']}《{r['title']}》\n")
        for context, flags in r["flags"]:
            marked = context
            for ft in flags:
                marked = marked.replace(ft, f"**「{ft}」**", 1)
            parts.append(f"- {marked}")
            total += len(flags)
        parts.append("")
    parts.append(f"---\n共标注 **{total}** 处可疑位置，涉及 **{len(results)}** 道汤。")
    return "\n".join(parts)

# ========== MAIN ==========
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default="7.19虾滑汤.pdf")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--work-dir", default="ocr_work_new")
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    # ---- Step 1: OCR ----
    pages = ocr_all(args.pdf, args.work_dir, args.dpi)
    pages[1] = COVER
    nums = sorted(pages)

    # ---- Step 2: TOC drop ----
    DROP = set()
    for n in nums:
        t = pages[n]
        has_body = ("汤面" in t) or ("汤底" in t)
        if not has_body and toc_line_count(t) >= 4:
            DROP.add(n)
    print("TOC pages dropped:", sorted(DROP))

    # ---- Step 3: Line stream ----
    lines = []
    for n in nums:
        if n in DROP:
            continue
        for ln in pages[n].split("\n"):
            lines.append(ln)
    N = len(lines)

    # ---- Step 4: Detect titles ----
    raw = []
    for i in range(N):
        if not has_tangmian(lines, i):
            continue
        if not looks_like_title(lines[i]):
            continue
        q = re_q.search(lines[i])
        t = q.group(2)
        raw.append({"idx": i, "title": t.strip()})

    SEP_TITLES = {"保姆"}
    seen = set()
    puzzles = []
    for p in raw:
        t = p["title"]
        if t in seen and t not in SEP_TITLES:
            continue
        seen.add(t)
        puzzles.append(p)
    print("Detected puzzles:", len(puzzles))

    for n, p in enumerate(puzzles):
        s, e = assign_code(n)
        p["season"], p["ep"] = s, e

    # ---- Step 5: Build main md ----
    # TOC
    toc_parts = ["## 目录", ""]
    seasons = {}
    for p in puzzles:
        seasons.setdefault(p["season"], []).append(p)
    for s in sorted(seasons, key=int):
        items = seasons[s]
        p_s = f"### 第 {s} 赛季（共 {len(items)} 篇）"
        toc_parts.append(p_s)
        for k, p in enumerate(items, 1):
            toc_parts.append(f"{k}. S{p['season']}E{p['ep']} 《{p['title']}》")
        toc_parts.append("")

    # Body
    body = ["## 正文（按汤分节）", ""]
    puzz_idx = {p["idx"]: p for p in puzzles}
    keys = sorted(puzz_idx)
    ki = 0
    for i in range(N):
        if ki < len(keys) and i == keys[ki]:
            p = puzz_idx[i]
            body.append(f"### S{p['season']}E{p['ep']} 《{p['title']}》")
            ki += 1
        else:
            if looks_like_title(lines[i]) and i not in puzz_idx:
                continue
            cl = clean_line(lines[i])
            if cl.strip() == "":
                body.append("")
            else:
                body.append(cl)

    # Collapse blanks
    collapsed = []; blank = 0
    for ln in body:
        if ln.strip() == "":
            blank += 1
            if blank <= 1:
                collapsed.append(ln)
        else:
            blank = 0
            collapsed.append(ln)
    body = collapsed

    # Assemble
    md = [
        "# 许二木海龟汤合集",
        "",
        "> 作者：许二木 ｜ 编者：长安",
        "> 来源：141 页扫描版 PDF（无文字层），经 PaddleOCR 全文 AI 识别，按「汤」分节整理。",
        "",
    ]
    md.extend(toc_parts)
    md.append("")
    md.extend(body)

    out_path = os.path.join(args.out_dir, OUT_MD)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print(f"Written: {out_path}")
    print(f"  bytes: {os.path.getsize(out_path)}")
    print(f"  body chars (kept): {sum(len(l) for l in body)}")

    # ---- Step 6: Coherence analysis ----
    print("\nRunning coherence analysis...")
    diag_results = analyze_coherence(puzzles, body)
    diag_md = build_diag_md(diag_results)
    diag_path = os.path.join(args.out_dir, OUT_DIAG)
    with open(diag_path, "w", encoding="utf-8") as f:
        f.write(diag_md)
    print(f"Written: {diag_path}")
    print(f"  bytes: {os.path.getsize(diag_path)}")
    soups_with_issues = len(diag_results)
    total_flags = sum(len(r["flags"]) for r in diag_results)
    print(f"  flagged: {total_flags} spots across {soups_with_issues} soups")

if __name__ == "__main__":
    main()
