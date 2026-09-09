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


def render_docx(cfg, data):
    paras = data["chapters"][0]["paragraphs"]
    xml_parts = []
    title = data.get("title", "") or "译文"
    xml_parts.append(_para_docx(title, _HEADING_STYLE[0][0], _HEADING_STYLE[0][1]))
    if data.get("author"):
        xml_parts.append(_para_docx(data["author"], None, "center"))
    xml_parts.append(_para_docx("（中文翻译 · 由 AutoTrans 自动生成）", "<w:sz w:val=\"18\"/>", "center"))
    xml_parts.append(_para_docx(""))

    untranslated = 0
    for p in paras:
        zh = (p.get("zh") or "").strip()
        en = (p.get("text") or "").strip()
        if not zh:
            untranslated += 1
            xml_parts.append(_para_docx(f"[未翻译] {en}"))
            continue
        if p.get("kind") == "heading":
            style, align = _heading_docx_style(p.get("level"))
            xml_parts.append(_para_docx(_ch_title_zh(zh, data), style, align))
        else:
            xml_parts.append(_para_docx(zh))
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
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                     '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                '</Relationships>')

    name = cfg["render"].get("docx_name") or (re.sub(r'[\\/:*?"<>|]+', "_", title) + "_中文翻译.docx")
    out = os.path.join(cfg["out_dir"], name)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", docxml)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", styles)
    print("DOCX ->", out)
    return out


# ---------------- EPUB ----------------

CSS = """
body { font-family: "Noto Serif SC","Source Han Serif SC","Songti SC","SimSun",serif;
       line-height: 1.85; }
h1.book { text-align:center; font-size:1.7em; margin:2em 0 0.5em; }
h2.chapter { text-align:center; font-size:1.4em; margin:1.5em 0 1em; }
h3.section { font-size:1.1em; margin:1.1em 0 0.5em; }
p { text-indent:2em; margin:0.6em 0; text-align:justify; }
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

    for ci, ch in enumerate(data["chapters"]):
        ctitle = ch["title"]
        parts = [f'<h2 class="chapter">{html.escape(ctitle)}</h2>']
        for p in ch["paragraphs"]:
            text = (p.get("zh") or p.get("text") or "")
            tag = "h3" if p.get("kind") == "heading" else "p"
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

    name = cfg["render"].get("epub_name") or (re.sub(r'[\\/:*?"<>|]+', "_", title) + "_中文版.epub")
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
