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
    if not t or len(t) > 90:
        return False
    if _is_furniture(line):
        return False
    if t.endswith((".", ";", ",")):
        return False
    # 明显大于正文字号（如标题、章标题），或短且像标题
    if line["size"] >= body_median * 1.12:
        return True
    if len(t) <= 60 and not re.search(r"\d{2,}", t) and len(t.split()) <= 12:
        return True
    return False


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
            for para in _group_paragraphs(col_lines):
                if ext.get("skip_figure_captions") and _is_figure_caption(para[0]):
                    continue
                joined = _para_text(para)
                if not joined:
                    continue
                kind = "heading" if (len(para) == 1 and _looks_heading(para[0], body_median)) else "body"
                units.append({"kind": kind, "text": joined, "page": page1})

    doc.close()

    title = cfg.get("title") or os.path.splitext(os.path.basename(cfg["pdf_path"]))[0]
    data = {
        "title": title,
        "author": cfg.get("author", ""),
        "chapters": [{"title": title, "paragraphs": [
            {"print_page": u["page"], "pdf_page": u["page"], "text": u["text"], "kind": u["kind"]}
            for u in units]}],
        "notes": {"paragraphs": []},
        "meta": {"two_column": bool(two_col), "reference_page": ref_page,
                 "body_median": round(body_median, 2)},
    }

    os.makedirs(cfg["out_dir"], exist_ok=True)
    with open(os.path.join(cfg["out_dir"], "extracted.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    # preview
    prev = os.path.join(cfg["out_dir"], "preview")
    os.makedirs(prev, exist_ok=True)
    n_words = sum(len(u["text"].split()) for u in units)
    with open(os.path.join(prev, "article.txt"), "w", encoding="utf-8") as f:
        for u in units:
            f.write(("[H] " if u["kind"] == "heading" else "") + u["text"] + "\n\n")
    print(f"提取完成：{len(units)} 段 / ~{n_words} 词；双栏={two_col}；参考文献起始页={ref_page}")
    return data
