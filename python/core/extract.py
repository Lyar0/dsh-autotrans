#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.extract — 从 PDF 提取正文，自动识别单/双栏排版、正文区域、章节标题，
过滤页眉页脚/图注/参考文献，输出结构化 JSON 与纯文本预览。

产物：
  out_dir/extracted.json   {title, author, chapters:[{title, paragraphs:[{text,kind}]}], notes}
  out_dir/preview/*.txt    分章纯文本预览（检查提取质量用）
"""
import json
import os
import re
import statistics


def _load_pymupdf():
    """延迟导入 pymupdf，让 --help / translate / render 在未装 PDF 依赖时也能运行。"""
    try:
        import pymupdf
        return pymupdf
    except Exception:
        try:
            import fitz as pymupdf
            return pymupdf
        except Exception as exc:  # pragma: no cover - dependency diagnostics
            raise RuntimeError(
                "缺少 PDF 提取依赖 pymupdf：请运行  pip install pymupdf  （若失败可改用 pip install PyMuPDF）"
            ) from exc


def _norm(txt):
    txt = txt.replace("\u00ad", "").replace("\u00ac", "")
    txt = txt.replace("\u00a0", " ").replace("\u2009", " ").replace("\u2007", " ")
    txt = txt.replace("\u2011", "-")
    return re.sub(r"[ \t]+", " ", txt).strip()


def _line_items(doc, pno, body_top, body_bottom):
    """返回页面文本行：{x0,x1,y0,y1,size,text}，已过滤页眉页脚。"""
    d = doc[pno].get_text("dict")
    out = []
    for blk in d["blocks"]:
        if blk["type"] != 0:
            continue
        for l in blk["lines"]:
            x0, y0, x1, y1 = l["bbox"]
            if y1 < body_top or y0 > body_bottom:
                continue
            spans = l["spans"]
            if not spans:
                continue
            # 行字号取字符数加权的主导字号
            best = max(spans, key=lambda s: len(s.get("text", "")) or 1)
            size = best["size"]
            txt = _norm("".join(s["text"] for s in spans))
            if not txt:
                continue
            out.append({"x0": x0, "x1": x1, "y0": y0, "y1": y1, "size": size, "text": txt})
    return out


def _estimate_columns(doc, pages, body_top, body_bottom):
    """估计分栏：统计各行水平中心在 x<350 与 x>=350 的分布。若明显双峰则分栏。"""
    left = right = 0
    for pno in pages:
        for it in _line_items(doc, pno, body_top, body_bottom):
            if it["size"] < 8:
                continue
            mid = (it["x0"] + it["x1"]) / 2.0
            if mid < 350:
                left += 1
            else:
                right += 1
    return bool(left and right)


def _median_body_size(doc, pages, body_top, body_bottom):
    sizes = []
    for pno in pages:
        for it in _line_items(doc, pno, body_top, body_bottom):
            if 8.5 <= it["size"] <= 16 and it["size"] < 13:
                sizes.append(it["size"])
    return statistics.median(sizes) if sizes else 10.0


FURNITURE = {"review", "open access", "original article", "research article", "editorial",
             "research", "article", "letter", "perspective", "protocol", "correction"}


def _is_furniture(line):
    return line["text"].strip().lower() in FURNITURE


def _looks_heading(line, body_median):
    t = line["text"]
    if not t:
        return False
    if _is_furniture(line):
        return False
    if t.strip().endswith((".", ";", ",")):
        return False
    # 带章节号的长标题即便超过 90 字也判为标题
    numbered = bool(re.match(r"^\s*\d{1,2}(?:\.\d{1,2}){0,2}\s+[A-Za-z\u4e00-\u9fff]", t))
    if numbered and len(t) <= 200:
        return True
    if len(t) > 90:
        return False
    low = _strip_punct_lower(t)
    # 独立成段的常见大写章节头 / 顶层关键字
    if low in _TOP_LEVEL_KEYWORDS:
        return True
    words = t.split()
    if words and t.isupper() and 1 <= len(words) <= 12 and not re.search(r"\d{2,}", t):
        return True
    # 字号明显大于正文的（章/节/标题），通常是独立短行
    if line["size"] >= body_median * 1.45:
        return True
    return False


def _strip_punct_lower(text):
    # 去掉末尾的标点/计数修饰后规整为小写，便于与关键字/全大写匹配
    return text.strip().strip(":.\u2013\u2014-_ ").lower()


# 常见顶层章节（映射为 level 0/1 的强关键字），供标题分级参考
_TOP_LEVEL_KEYWORDS = {
    "abstract", "introduction", "background", "methods", "materials and methods",
    "results", "discussion", "conclusions", "conclusion", "references",
    "acknowledgements", "acknowledgments", "bibliography", "literature cited",
    "declarations", "supplementary information", "author details",
    "definitions", "data availability", "funding",
}


def _heading_level(text, size, body_median, ref_size=None):
    """启发式给一个 heading 文本估层级（0=书名级,1=章,2=节,3=小节）。"""
    t = text.strip()
    low = t.lower().rstrip(".:; ")
    if low in _TOP_LEVEL_KEYWORDS:
        return 1
    # 序号结构：1 / 1.2 / 2.3.1 … 点数越多层级越深
    m = re.match(r"^(\d{1,2})(?:\.(\d{1,2}))?(?:\.(\d{1,2}))?", t)
    if m:
        dots = sum(1 for g in m.groups()[1:] if g)
        return 1 + min(dots, 2)  # "1"→1, "1.2"→2, "1.2.3"→3
    if t.isupper() or low.startswith(("chapter ", "part ")):
        return 1
    # 相对字号：明显放大归 1（章），否则归 2（节）
    base = ref_size if ref_size and ref_size > 0 else body_median
    return 1 if size >= base * 1.45 else 2


def _looks_caption_start(text):
    t = text.lower()
    return bool(re.match(r"^(fig(ure)?\.?|table|supplementary)\s*[0-9ivxlc]+", t)) or t.startswith("fig.")


_TERM_RE = re.compile(r"[.!?;:]$")


def _looks_sentence_end(text):
    """去掉尾部常见引用/编号闭括号后，判断文本是否以句号等结尾。

    覆盖像 `[ 31 ].`、` (c).`、`SC)`… 末尾其实已闭合的正文；也避免把
    "等等。"之后又跟闭括号误判为未完继续。
    """
    t = (text or "").strip()
    if not t:
        return True  # 空不合并
    # 去掉末尾成对的闭括号/可能跟的引用编号
    m = re.search(r"([.!?;:])[\s]*[\])]*\s*$", t)
    if m:
        return True
    return False


def _merge_overflow(units):
    """把正文中被 栏/页 误切成多片的同段续接起来。

    跨双栏 PDF 的常见病：一个自然段在左栏结尾没画句号就断了（栏末/页末），
    extract 将其当独立段 → 中句被拆成“各自完整但其实是孤句”令翻译失真。
    这里以“上一 body 段不以句末标点收尾、且下一项也是 body（非标题）”为判据，
    把下段并入上一段，把同一个自然段完整接回去。标题永远独立。
    """
    out = []
    for u in units:
        if not out:
            out.append(dict(u))
            continue
        last = out[-1]
        if (last.get("kind") == "body" and u.get("kind") == "body"
                and not _looks_sentence_end(last.get("text"))):
            # 续接
            lt = last.get("text") or ""
            sep = "" if (lt.endswith("-") or lt.endswith("\u2010")) else " "
            last["text"] = _norm(lt + sep + (u.get("text") or "")).strip()
            continue
        out.append(dict(u))
    # 段尾孤句(整段仍不以句点结束)不改；已是信息最小残留
    return out


def _segment_column(lines, body_median, page, ext):
    """把某栏 lines 切成 {kind,text,level,page} 单元。

    采用“行级优先级”识别标题，兼容多种排版：
      a) 标题独立行（可能带大字）;
      b) 标题号被拆成一行（'2.1'）紧接着标题词行（'HiST Model Structure'）;
      c) 章节号+标题+正文被 pymupdf 拆到多行但粘在同一段。
    流程：先把 lines 按 y 排序；
      ① 标记“副标题边界轮”：任何位于非正文开头的孤立短数字号行 + 紧随的短标题词行综合判定；
      ② 用 _group_paragraphs 分粗段，段内再做行级标题切分。
    """
    if not lines:
        return []

    def is_caption(line):
        return _looks_caption_start(line.get("text", ""))

    def heading_start(line):
        # 单行判定：带章节号、上层关键字、短全大写、或字号远大于正文
        return _looks_heading(line, body_median)

    def lone_num(line):
        return bool(re.match(r"^\s*\d{1,2}(?:\.\d{1,2}){0,2}\s*$", line.get("text", "").strip()))

    def title_word(line):
        t = line.get("text", "").strip()
        if not t or len(t) > 160 or t.endswith((".", ";")):
            return False
        return _looks_caption_start(t) is False and (
            _looks_heading(line, body_median) or heading_looks_title(t)
        )

    raw = sorted(lines, key=lambda l: l["y0"])
    out = []
    groups = _group_paragraphs(raw)
    for para in groups:
        if ext.get("skip_figure_captions") and is_caption(para[0]):
            continue
        # 行级标题收集
        sub = _extract_heading_lines(para, heading_start, lone_num, title_word, body_median)
        if sub is None:
            joined = _para_text(para)
            if joined:
                out.append({"kind": "body", "text": joined, "page": page})
            continue
        head_lines, rest = sub
        head_text = _para_text(head_lines)
        if head_text:
            lvl = _heading_level(head_text, head_lines[0]["size"], body_median)
            out.append({"kind": "heading", "level": lvl, "text": head_text, "page": page})
        if rest:
            out.extend(_as_body_units(rest, page))
    return out


def heading_looks_title(t):
    # 用于标题词行补充：不含句点且以大写短词开头的行判标题，降低把“正文续行”当标题的误判
    words = t.split()
    if not words or len(words) > 14 or len(t) > 180:
        return False
    if not words[0][0].isupper():
        return False
    if re.search(r"[.!?]\s*$", t):
        return False
    return True


def _extract_heading_lines(para, heading_start, lone_num, title_word, body_median):
    """从一个已分组段落中抽取“标题行前缀”。

    返回 (head_lines, rest_lines) 或 None（本段不含行级标题）。覆盖：
      - [文头孤立行] 本身即标题（如独立短标题行）
      - [孤立数字号行][标题词行] 相邻组合（'2.1' + 'HiST Model Structure'）
      - [孤立数字号行] 后面接很长正文时不算（避免把编号列表当标题）
    """
    n = len(para)
    i = 0
    # 允许前导孤立数字号行
    if n and lone_num(para[0]):
        hdr = [para[0]]
        i = 1
        # 若紧跟标题词行，则并入
        if i < n and title_word(para[i]) and not lone_num(para[i]):
            hdr.append(para[i]); i += 1
            # 可能标题词被拆多行（都满足 title_word 且较短）
            while i < n and title_word(para[i]) and len(para[i]["text"]) <= 100:
                hdr.append(para[i]); i += 1
        # 无标题词跟随时：孤立数字可能只是列表项，不判为标题；除非它本身就是带完整标题的一个段（段仅此行）
        if len(hdr) == 1 and n == 1:
            return hdr, []
        if len(hdr) == 1:
            return None
        return hdr[:i], para[i:]

    # 无孤立号：若首行即标题
    if heading_start(para[0]):
        hdr = [para[0]]
        j = 1
        while j < n and title_word(para[j]) and lone_num(para[j]) is False and len(para[j]["text"]) <= 100:
            hdr.append(para[j]); j += 1
        return hdr, para[j:]
    # 首行是普通正文
    return None


def _as_body_units(raw_lines, page):
    """把非标题行按行距再分组成 body 单元（标题拆开后余文可能跨段）。"""
    result = []
    for para in _group_paragraphs(raw_lines):
        t = _para_text(para)
        if t:
            result.append({"kind": "body", "text": t, "page": page})
    return result


def _group_paragraphs(lines):
    """按行间距把同栏 lines 分组为段落；返回 list[list[line]]。"""
    if not lines:
        return []
    lines = sorted(lines, key=lambda l: l["y0"])
    ys = sorted(l["y0"] for l in lines)
    deltas = [ys[i + 1] - ys[i] for i in range(len(ys) - 1) if 0 < ys[i + 1] - ys[i] < 40]
    line_h = statistics.median(deltas) if deltas else 13.0
    thr = line_h * 1.75
    paras = [[lines[0]]]
    for l in lines[1:]:
        gap = l["y0"] - paras[-1][-1]["y0"]
        if gap > thr:
            paras.append([l])
        else:
            paras[-1].append(l)
    return paras


def _para_text(para):
    raw = [_norm(l["text"]) for l in para]
    raw = [r for r in raw if r]
    return " ".join(raw)


def _is_figure_caption(line):
    t = line["text"].lower()
    return bool(re.match(r"^(fig(ure)?\.?|table)\s*[0-9ivxlc]+", t)) or t.startswith("fig.")


def _is_reference_start(line):
    t = line["text"].strip().lower()
    return t in ("references", "bibliography", "reference", "literature cited")


def _detect_reference_page(doc, body_top, body_bottom):
    """定位 References/Bibliography 起始页（1-based）及其 y 坐标；找不到返回 (None, None)。"""
    for pno in range(doc.page_count):
        for it in _line_items(doc, pno, body_top, body_bottom):
            if _is_reference_start(it):
                return pno + 1, it["y0"]
    return None, None


def run(cfg):
    pymupdf = _load_pymupdf()
    ext = cfg["extract"]
    body_top, body_bottom = ext.get("body_top", 80), ext.get("body_bottom", 730)
    doc = pymupdf.open(cfg["pdf_path"])
    total = doc.page_count

    ref_page, ref_y = (_detect_reference_page(doc, body_top, body_bottom)
                       if ext.get("skip_references") else (None, None))
    last_page = ref_page if ref_page else total

    # 自动布局
    scan = list(range(min(total, 8)))
    two_col = ext.get("auto_layout", True) and _estimate_columns(doc, scan, body_top, body_bottom)
    body_median = _median_body_size(doc, scan, body_top, body_bottom)
    col_split = ext.get("col_split", 300.0)

    units = []  # {kind,text,page}
    for pno in range(total):
        page1 = pno + 1
        if page1 > last_page:
            break
        is_ref_page = (page1 == ref_page)
        min_font = ext.get("tail_min_font", 7.0) if is_ref_page else ext.get("min_font", 8.9)
        items = [it for it in _line_items(doc, pno, body_top, body_bottom)
                 if min_font <= it["size"] <= 28.0 and not _is_furniture(it)]
        if not items:
            continue
        # 分配列
        for it in items:
            it["col"] = 0 if ((it["x0"] + it["x1"]) / 2.0) < col_split else 1
        if not two_col:
            for it in items:
                it["col"] = 0

        # 参考文献页：丢弃 References 标题及其后的内容（含右栏参考文献列）
        if is_ref_page:
            items = [it for it in items if not _is_reference_start(it) and it["y0"] < ref_y]
            if two_col:
                # 参考文献通常独占右栏；为稳妥，仅保留左栏内容
                items = [it for it in items if it["col"] == 0]

        for col in sorted(set(it["col"] for it in items)):
            col_lines = [it for it in items if it["col"] == col]
            units.extend(_segment_column(col_lines, body_median, page1, ext))

    # ---- 收集并落盘页面大图（供 Word 插图）；需在 doc 关闭前完成 ----
    images_dir = os.path.join(cfg["out_dir"], "images")
    os.makedirs(images_dir, exist_ok=True)
    image_list = []
    for entry in _harvest_page_images(doc, images_dir, body_top, body_bottom, last_page, config=ext):
        image_list.append(entry)

    doc.close()

    title = cfg.get("title") or os.path.splitext(os.path.basename(cfg["pdf_path"]))[0]
    os.makedirs(images_dir, exist_ok=True)

    paras_out = []
    # 合并“跨栏/跨页被截断”的同段正文：以一个未以句点结尾的 body 段为信号，
    # 把紧随的下一个 body 续接进来，修复“也表现出较高水平”这类句中被拆开各自翻译的问题。
    units = _merge_overflow(units)
    for u in units:
        rec = {"print_page": u["page"], "pdf_page": u["page"], "text": u["text"], "kind": u["kind"]}
        if u.get("level") is not None:
            rec["level"] = u["level"]
        paras_out.append(rec)
    data = {
        "title": title,
        "author": cfg.get("author", ""),
        "chapters": [{"title": title, "paragraphs": paras_out}],
        "images": image_list,
        "notes": {"paragraphs": []},
        "meta": {"two_column": bool(two_col), "reference_page": ref_page,
                 "body_median": round(body_median, 2)},
    }

    # preview
    prev = os.path.join(cfg["out_dir"], "preview")
    os.makedirs(prev, exist_ok=True)
    n_words = sum(len(u["text"].split()) for u in units)
    with open(os.path.join(prev, "article.txt"), "w", encoding="utf-8") as f:
        for u in units:
            tag = "body"
            if u["kind"] == "heading":
                tag = "H%d" % u.get("level", 2)
            f.write(f"[{tag}] " + u["text"] + "\n\n")

    with open(os.path.join(cfg["out_dir"], "extracted.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print(f"提取完成：{len(units)} 段 / ~{n_words} 词；图片={len(image_list)}；双栏={two_col}；参考文献起始页={ref_page}")
    return data


def _harvest_page_images(doc, images_dir, body_top, body_bottom, last_page, config=None, max_per_page=6, min_px=40000):
    """逐页收集并落盘“版面大图”（去除 logo/小图标），返回 [{page,file,w,h}]。

    只认矩形位于正文上/中区、面积够大的直接图片；同一 xref 去重（一页可能放多个同 xref）。
    图片写入 out_dir/images/page_NN_idx.ext，供 DOCX 插图阶段引用。
    """
    out = []
    done = set()  # (page) 本函数产出以页+顺序即够；xref 去重按 (page,xref)
    seen_xref = set()
    for pno in range(min(last_page or doc.page_count, doc.page_count or 0)):
        page = doc[pno]
        pn = pno + 1
        imgs = page.get_images(full=True)
        if not imgs:
            continue
        placed = []
        for item in imgs:
            try:
                xref = item[0]
                rects = page.get_image_rects(xref)
            except Exception:
                continue
            if not rects:
                continue
            info = doc.extract_image(xref)
            if not info:
                continue
            w, h = info.get("width", 0), info.get("height", 0)
            ext = (info.get("ext") or "png").lower()
            # SKIP icon-like (small)
            if w * h < min_px:
                continue
            for r in rects:
                rw, rh = r.width, r.height
                # 要求矩形落在正文纵向带且非极小（图标徽标之类）
                if rw <= 20 and rh <= 20:
                    continue
                placed.append((xref, w, h, ext, r))
        # 过滤多余同 xref
        picked, px = [], []
        for entry in placed:
            xref, w, h, ext, r = entry
            if (pn, xref) in seen_xref:
                continue
            seen_xref.add((pn, xref))
            picked.append(entry)
        if not picked:
            continue
        for idx, (xref, w, h, ext, r) in enumerate(picked[:max_per_page]):
            img = doc.extract_image(xref)
            fname = f"page_{pn:02d}_{idx}.{ext}"
            fpath = os.path.join(images_dir, fname)
            with open(fpath, "wb") as fd:
                fd.write(img["image"])
            out.append({"page": pn, "file": fname,
                        "rel": os.path.join("images", fname).replace("\\", "/"),
                        "w": w, "h": h})
        if len(picked) > max_per_page:
            print(f"  警告：第 {pn} 页图片过多，仅收集前 {max_per_page} 张")
    return out
