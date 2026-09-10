#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.config — 配置加载、默认值合并与术语表解析。"""
import json
import os
import sys


def base_dir():
    """项目根：源码运行时为仓库 python/ 目录；PyInstaller 冻结时为解包数据目录。"""
    if getattr(sys, "frozen", False):
        # onedir/onefile：附加的数据文件（glossaries）在 _MEIPASS 下
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


HERE = base_dir()


def user_glossary_path():
    """持久“用户词表库”位置（跨文档累积、跨使用不断变厚）。

    优先级：
      1) 环境变量 AUTOTRANS_USER_GLOSSARY（由插件传入，避免各自猜测）
      2) $DSH_HOME 或 ~/.dsh 下的 autotrans/user_glossary.tsv
    这个文件存放用户自行编辑的译名 + 每次自动生成并入度高的词对；会被并入后续所有翻译。
    """
    env = os.environ.get("AUTOTRANS_USER_GLOSSARY", "").strip()
    if env:
        return os.path.abspath(env)
    dsh_home = os.environ.get("DSH_HOME", "").strip()
    if not dsh_home:
        dsh_home = os.path.join(os.path.expanduser("~"), ".dsh")
    return os.path.join(dsh_home, "autotrans", "user_glossary.tsv")


def user_glossary_exists():
    return os.path.exists(user_glossary_path())

DEFAULTS = {
    "pdf_path": "",
    "out_dir": "",                    # 留空则自动 <pdf>_translated/
    "glossary": "",                   # 留空=不用术语表；可用内置名或绝对路径
    "title": "",                      # 文档标题（留空则从 PDF 文件名推断）
    "author": "",                     # 作者（可选）

    "extract": {
        "body_top": 80,               # 页眉过滤线（y 小于此值视为页眉）
        "body_bottom": 730,           # 页码/版权过滤线（y 大于此值视为页脚）
        "col_split": 300.0,           # 双栏分栏 x 中线；自动模式下由版面估计
        "min_font": 8.9,              # 正文最小字号（低于此值视为图注/参考文献）
        "tail_min_font": 7.0,         # 末页声明区保留的更低字号
        "skip_references": True,       # 跳过参考文献列表
        "skip_figure_captions": True,  # 跳过图注
        "auto_layout": True,           # 自动识别单/双栏与分栏位置
        "full_width_lines": False,     # 双栏页中把跨栏通栏行(标题/摘要横幅)单独成组；默认关闭（优先保证内容完整）
        "dedupe_page": False,          # 去除同一页重复文本层带来的整段重复
    },

    "translation": {
        "api_base": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "batch_chars": 2800,
        "max_chunk": 900,
        "max_tokens": 8192,
        "concurrency": 4,
        "temperature": 0.3,
        "domain": "general",          # general | biomedical | anthropology | life-science
        "system_prompt_file": "",      # 可选：自定义系统提示词文件（utf-8）
        "auto_glossary": True,         # 翻译前自动用 LLM 从本文扫出领域术语表并并入
    },

    "render": {
        "format": "docx",             # docx | epub | both
        "mode": "cn",                 # cn（仅中文，默认）| bilingual / en-zh（逐段中英对照：一段英文+一段中文）
        "docx_name": "",              # 留空自动 <书名>_中文翻译.docx
        "epub_name": "",              # 留空自动 <书名>_中文版.epub
    },
}

GLOSSARY_PRESETS = {
    "biomedical": os.path.join(HERE, "glossaries", "biomedical.tsv"),
    "anthropology": os.path.join(HERE, "glossaries", "anthropology.tsv"),
    "general": os.path.join(HERE, "glossaries", "general.tsv"),
    "": "",
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_glossary(spec):
    """把术语表 spec 解析为真实 TSV 路径；找不到返回空串。"""
    if not spec:
        return ""
    if spec in GLOSSARY_PRESETS:
        p = GLOSSARY_PRESETS[spec]
        return p if os.path.exists(p) else ""
    if os.path.exists(spec):
        return spec
    return ""


def load_config(path=None):
    cfg = _deep_merge(DEFAULTS, {})
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg = _deep_merge(cfg, json.load(f))
    cfg["glossary"] = resolve_glossary(cfg.get("glossary", ""))
    return cfg


def build_default_config(pdf_path, out_dir=None):
    cfg = _deep_merge(DEFAULTS, {})
    cfg["pdf_path"] = os.path.abspath(pdf_path)
    cfg["out_dir"] = out_dir or (os.path.splitext(os.path.abspath(pdf_path))[0] + "_translated")
    return cfg
