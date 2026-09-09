#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.render — 把 translated_final.json 渲染为中文 DOCX / EPUB。

DOCX 用标准库 zipfile+XML 生成，无需 python-docx；EPUB 用 ebooklib。
"""
import json
import os
import re
import html
import zipfile
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _heading_zh_map(data):
    """很轻量的常见章节标题中译（缺失则回退原文）。可自行扩充。"""
    return {
        "abstract": "摘要", "introduction": "绪论", "background": "背景",
        "conclusions": "结论", "conclusion": "结论", "results": "结果",
        "methods": "方法", "materials and methods": "材料与方法",
        "discussion": "讨论", "acknowledgements": "致谢", "acknowledgments": "致谢",
        "declarations": "声明", "funding": "基金资助", "references": "参考文献",
        "author details": "作者信息", "competing interests": "利益冲突",
    }


def _safe_basename(title, suffix, ext, limit=60):
    """生成安全、短而可辨的渲染文件名（避免超长中文/路径导致 Word 打不开）。

    去掉非法字符后截取 base ≈ limit 字符，再拼 后缀+扩展名。
    """
    base = re.sub(r'[\\/:*?"<>|\u0000-\u001f]+', "_", str(title or "").strip())
    base = base.strip("._ ") or "译文"
    return (base[:limit].rstrip().rstrip("._ ") + suffix + ext)


def _ch_title_zh(title, data):
    low = title.strip().lower()
    m = _heading_zh_map(data)
    if low in m:
        return m[low]
    return title


# ---------------- DOCX ----------------

# 各级标题的(加粗, 字号半磅, 是否居中/对齐)。level 语义: 0 书名/顶部, 1 章, 2 节, 3+ 小节。
_HEADING_STYLE = {
    0: ("<w:b/><w:sz w:val=\"36\"/>", "center"),   # 书名
    1: ("<w:b/><w:sz w:val=\"32\"/>", "center"),   # 章
    2: ("<w:b/><w:sz w:val=\"28\"/>", None),       # 节
    3: ("<w:b/><w:sz w:val=\"26\"/>", None),       # 小节
}
_HEADING_STYLE_LAST = ("<w:b/><w:sz w:val=\"24\"/>", None)


def _heading_docx_style(level):
    return _HEADING_STYLE.get(int(level) if level is not None else 2, _HEADING_STYLE_LAST)


def _para_docx(text, style=None, align=None):
    props = f"<w:rPr>{style}</w:rPr>" if style else ""
    jc = f'<w:jc w:val="{align}"/>' if align else ""
    ppr = f"<w:pPr>{jc}</w:pPr>" if align else ""
    return (f'<w:p>{ppr}<w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')


# —— 图片插入相关常量 ——
A_NAME = "http://schemas.openxmlformats.org/drawingml/2006/main"
A_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
A_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
EMU_PER_PX = 9525  # 1 px ~ 9525 EMU (96 dpi)


def _image_para_xml(rel_id, w_emu, h_emu):
    """构造一张居中、内联图片的 <w:p> XML。"""
    extent = f'cx="{w_emu}" cy="{h_emu}"'
    return (
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
        f'<wp:inline distT="0" distB="0" distL="114300" distR="114300" xmlns:wp="{A_WP}" xmlns:a="{A_NAME}">'
        f'<wp:extent {extent}/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        '<wp:docPr id="100" name="Picture 1"/>'
        f'<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic xmlns:a="{A_NAME}"><a:graphicData uri="{A_PIC}">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:nvPicPr><pic:cNvPr id="0" name="autotrans-figure"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="RID" xmlns:r="RELSURI"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext {extent}/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    ).replace('RID', rel_id).replace('RELSURI', A_REL)


def _fit_emu(img_w_px, img_h_px, max_w_emu, max_h_emu=None):
    """把图片像素等比例缩放进 EMU 上限（页宽自适应）。返回 (w,h) EMU。"""
    w = int(img_w_px) * EMU_PER_PX
    h = int(img_h_px) * EMU_PER_PX
    if max_w_emu and w > max_w_emu:
        h = int(h * max_w_emu / w)
        w = max_w_emu
    if max_h_emu and h > max_h_emu:
        w = int(w * max_h_emu / h)
        h = max_h_emu
    return max(int(w), 1), max(int(h), 1)


def _bilingual_enabled(cfg):
    return str(cfg.get("render", {}).get("mode", "cn")).lower() in ("bilingual", "en-zh", "en_zh", "enzh")


def _content_specs(p, bilingual):
    """把一个段落(有 en + zh)切成需要渲染的内容条目列表。

    纯中文(bilingual=False)：每段只输出一行（zh 译文，若无则回退英文并标记）。
    对照(bilingual=True)：每段先英文原文、再中文译文 —— 最终一段英文一段中文交替。
    返回 list[dict:{text, kind, level, lang?, untranslated}]
    """
    zh = (p.get("zh") or "").strip()
    en = (p.get("text") or "").strip()
    kind = p.get("kind", "body")
    level = p.get("level")

    def zh_body(t, heading=False):
        return {"text": t, "kind": ("heading" if heading else "body"), "level": level,
                "untranslated": False}

    if not bilingual:
        if zh:
            return [zh_body(zh, heading=(kind == "heading"))]
        if en:
            return [{"text": en, "kind": kind, "level": level, "untranslated": True}]
        return []

    out = []
    if en:
        out.append({"text": en, "kind": kind, "level": level, "untranslated": False, "lang": "en"})
    if zh:
        out.append(zh_body(zh, heading=(kind == "heading")))
    elif en:
        out.append({"text": en, "kind": kind, "level": level, "untranslated": True})
    return out


def render_docx(cfg, data):
    paras = data["chapters"][0]["paragraphs"]
    title = data.get("title", "") or "译文"

    # —— 读入待插图（按 pdf 页码标记）——
    media = []  # 顺序字段: rel, fpath, ext
    used_imgs = []
    for im in data.get("images", []) or []:
        rel = (im.get("rel") or f"images/{im.get('file') or ''}").replace("\\", "/")
        fpath = os.path.join(cfg["out_dir"], *rel.split("/"))
        if not os.path.exists(fpath):
            continue
        ext = os.path.splitext(fpath)[1].lstrip(".").lower() or "png"
        if ext not in ("png", "jpeg", "jpg", "gif", "bmp"):
            ext = "png"
        used_imgs.append({"page": int(im.get("page") or 1), "rel": rel,
                          "fpath": fpath, "ext": ext,
                          "w": int(im.get("w") or 0), "h": int(im.get("h") or 0)})
    used_imgs.sort(key=lambda x: (x["page"], x["rel"]))

    # rel id 分配（媒体从 rId2 起，rId1 留 styles）
    for i, u in enumerate(used_imgs, start=2):
        u["rel_id"] = f"rId{i}"
        u["w_emu"], u["h_emu"] = _fit_emu(u["w"], u["h"], max_w_emu=int(5.5 * 914400))

    # —— 组正文 ——
    xml_parts = []
    tail_imgs = list(reversed(used_imgs))
    media_files = []  # (rel, fpath, ext, rel_id)
    untranslated = 0

    def queue_media(u):
        media_files.append((u["rel"], u["fpath"], u["ext"], u["rel_id"]))

    def emit_title():
        xml_parts.append(_para_docx(title, _HEADING_STYLE[0][0], _HEADING_STYLE[0][1]))
        if data.get("author"):
            xml_parts.append(_para_docx(data["author"], None, "center"))
        xml_parts.append(_para_docx("（中文翻译 · 由 AutoTrans 自动生成）", "<w:sz w:val=\"18\"/>", "center"))
        xml_parts.append(_para_docx(""))

    def flush_before(page):
        while tail_imgs and tail_imgs[-1]["page"] <= int(page):
            u = tail_imgs.pop()
            queue_media(u)
            xml_parts.append(_image_para_xml(u["rel_id"], u["w_emu"], u["h_emu"]))
            xml_parts.append(_para_docx(""))

    emit_title()
    bilingual = _bilingual_enabled(cfg)

    def append_spec(spec_):
        """把一个 content spec 渲染成 docx 行。返回 False 若空。"""
        text = (spec_.get("text") or "").strip()
        if not text:
            return True  # 空行忽略
        is_head = spec_.get("kind") == "heading"
        if spec_.get("lang") == "en":
            # 对照模式：英文原文当正文输出（标题也先出英文文本再中文标题）
            if is_head:
                style, align = _heading_docx_style(spec_.get("level"))
                xml_parts.append(_para_docx(text, style, align))
            else:
                # 英文正文：稍小、斜体弱化以区分中文——但保持可读正常体
                xml_parts.append(_para_docx(text, "<w:sz w:val=\"21\"/>", None))
            return True
        # 中文/译文
        if spec_.get("untranslated"):
            xml_parts.append(_para_docx("[未翻译] " + text, "<w:i/>", None))
            return True
        if is_head:
            style, align = _heading_docx_style(spec_.get("level"))
            xml_parts.append(_para_docx(_ch_title_zh(text, data), style, align))
        else:
            xml_parts.append(_para_docx(text))
        return True

    for p in paras:
        pg = int(p.get("print_page") or p.get("pdf_page") or 1)
        flush_before(pg)
        specs = _content_specs(p, bilingual)
        # 判定是否“本段译文缺失”以统计
        if not (p.get("zh") or "").strip():
            untranslated += 1
        for s in specs:
            append_spec(s)

    # 剩余图（末段 page 之前都跑不到）置尾
    while tail_imgs:
        u = tail_imgs.pop()
        queue_media(u)
        xml_parts.append(_image_para_xml(u["rel_id"], u["w_emu"], u["h_emu"]))
        xml_parts.append(_para_docx(""))
    if untranslated:
        print(f"警告：{untranslated} 段未翻译，已回退英文原文。")

    docxml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              '<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>'
              ) % (W, "\n".join(xml_parts))
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
              '<w:styles xmlns:w="%s">'
              '<w:docDefaults><w:rPrDefault><w:rPr>'
              '<w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体" w:hAnsi="Times New Roman"/>'
              '<w:sz w:val="24"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>') % W

    # —— content types (含媒体) ——
    default_ct = {
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "xml": "application/xml",
        "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg",
        "gif": "image/gif", "bmp": "image/bmp",
    }
    _media_ext = sorted({m[2] for m in media_files}) or ["png"]
    ct_parts = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                '<Default Extension="xml" ContentType="application/xml"/>']
    for e in sorted(_media_ext):
        ct_parts.append(f'<Default Extension="{e}" ContentType="{default_ct.get(e, "application/octet-stream")}"/>')
    ct_parts.append('<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>')
    ct_parts.append('<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     + "".join(ct_parts) + '</Types>')

    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    rel_part = ['<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                'Target="styles.xml"/>']
    for i, m in enumerate(media_files, start=2):
        _, _, _, rid = m
        # target 写成 media/name（basename 即可）
        base = os.path.basename(m[1])
        rel_part.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{base}"/>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(rel_part) + '</Relationships>')

    suffix = "_中英对照" if _bilingual_enabled(cfg) else "_中文翻译"
    name = cfg["render"].get("docx_name") or _safe_basename(title, suffix, ".docx")
    out = os.path.join(cfg["out_dir"], name)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", docxml)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", styles)
        for i, m in enumerate(media_files, start=1):
            rel_media, fpath_media, ext_media, _ = m
            z.write(fpath_media, f"word/media/{os.path.basename(fpath_media)}")
    print("DOCX ->", out, f"(含 {len(media_files)} 图)")
    return out


# ---------------- EPUB ----------------

CSS = """
body { font-family: "Noto Serif SC","Source Han Serif SC","Songti SC","SimSun",serif;
       line-height: 1.85; }
h1.book { text-align:center; font-size:1.7em; margin:2em 0 0.5em; }
h2.chapter { text-align:center; font-size:1.4em; margin:1.5em 0 1em; }
h3.section { font-size:1.1em; margin:1.1em 0 0.5em; }
p { text-indent:2em; margin:0.6em 0; text-align:justify; }
p.en { font-family: Georgia,"Times New Roman",serif; color:#333;
       text-indent:0; font-size:0.95em; margin:0.9em 0 0.2em; }
p.fallback { color:#a00; }
"""


def render_epub(cfg, data):
    try:
        from ebooklib import epub
    except Exception as exc:  # pragma: no cover - dependency diagnostics
        raise RuntimeError(
            "缺少 EPUB 依赖 ebooklib：请运行  pip install ebooklib  （或将 render.format 设为 docx）"
        ) from exc

    book = epub.EpubBook()
    title = data.get("title", "") or "译文"
    book.set_identifier("autotrans-" + re.sub(r"[^a-zA-Z0-9]+", "-", title))
    book.set_title(title + "（中文版）")
    book.set_language("zh-CN")
    if data.get("author"):
        book.add_author(data["author"])

    css_item = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css",
                             content=CSS.encode("utf-8"))
    book.add_item(css_item)

    spine = ["nav"]
    toc = []
    cover = epub.EpubHtml(title="书名页", file_name="cover.xhtml", lang="zh-CN")
    cover.content = (f'<div style="text-align:center;margin-top:25%">'
                     f'<h1 class="book">{html.escape(title)}</h1>'
                     f'<p>{html.escape(data.get("author", ""))}</p><p>中文版</p></div>')
    cover.add_item(css_item)
    book.add_item(cover)
    spine.append(cover)
    toc.append(epub.Link("cover.xhtml", "书名页", "cover"))

    bilingual = _bilingual_enabled(cfg)
    for ci, ch in enumerate(data["chapters"]):
        ctitle = ch["title"]
        parts = [f'<h2 class="chapter">{html.escape(ctitle)}</h2>']
        for p in ch["paragraphs"]:
            for spec in _content_specs(p, bilingual):
                text = (spec.get("text") or "").strip()
                if not text:
                    continue
                if spec.get("lang") == "en":
                    tag = "h3" if spec.get("kind") == "heading" else "p.en"
                    cls = ' class="section"' if tag == "h3" else ' class="en"'
                    parts.append(f'<{tag}{cls}>{html.escape(text)}</{tag}>')
                    continue
                if spec.get("untranslated"):
                    parts.append(f'<p class="en fallback">[未翻译] {html.escape(text)}</p>')
                    continue
                tag = "h3" if spec.get("kind") == "heading" else "p"
                cls = ' class="section"' if tag == "h3" else ""
                parts.append(f'<{tag}{cls}>{html.escape(text)}</{tag}>')
        body = "\n".join(parts)
        item = epub.EpubHtml(title=ctitle, file_name=f"ch{ci:02d}.xhtml", lang="zh-CN")
        item.content = (f'<html><head><link rel="stylesheet" href="style.css"/></head>'
                        f'<body>{body}</body></html>')
        item.add_item(css_item)
        book.add_item(item)
        spine.append(item)
        toc.append((epub.Section(ctitle), (item,)))

    book.spine = spine
    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    name = cfg["render"].get("epub_name") or _safe_basename(
        title, "_中英对照" if _bilingual_enabled(cfg) else "_中文版", ".epub")
    out = os.path.join(cfg["out_dir"], name)
    epub.write_epub(out, book, {})
    print("EPUB ->", out)
    return out


def run(cfg):
    out_dir = cfg["out_dir"]
    final_path = os.path.join(out_dir, "translated_final.json")
    if not os.path.exists(final_path):
        print(f"未找到 {final_path}，请先运行 translate。")
        return []
    data = json.load(open(final_path, encoding="utf-8-sig"))
    fmt = cfg["render"].get("format", "docx").lower()
    files = []
    if fmt in ("docx", "both"):
        files.append(render_docx(cfg, data))
    if fmt in ("epub", "both"):
        files.append(render_epub(cfg, data))
    return files
