#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoTrans — 英文文献 / 专业书籍 → 中文 的自动化翻译工具链。

流程：extract（PDF→结构化文本）→ translate（DeepSeek→中文）→ render（→ DOCX/EPUB）。
全程断点续传；重跑只会补缺失部分，不重复计费。

用法（standalone）：
    python autotrans.py <pdf> [config.json]
    python autotrans.py all <pdf> [--glossary biomedical] [--format docx]
    python autotrans.py extract  <pdf>
    python autotrans.py translate <pdf>
    python autotrans.py render   <pdf>

也可被 dsh-autotrans 插件作为子进程调用；此时末尾会输出一行
`AUTOTRANS_RESULT={...}` JSON 摘要，供插件解析。
"""
import argparse
import json
import os
import sys


def _force_utf8_stdio():
    """确保 stdout/stderr 输出 UTF-8（跨平台、管道/重定向下均一致）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _bootstrap():
    # 可选：项目内 venv 的 site-packages，便于在不激活 conda 时也能加载依赖
    venv_sp = os.path.join(HERE, ".venv", "Lib", "site-packages")
    if os.path.isdir(venv_sp) and venv_sp not in sys.path:
        sys.path.insert(0, venv_sp)


_bootstrap()

from core.config import load_config, resolve_glossary  # noqa: E402
from core import extract, translate, render  # noqa: E402

ACTIONS = ("all", "extract", "translate", "render")
RESULT_PREFIX = "AUTOTRANS_RESULT="


def _emit_result(payload):
    """输出机器可读摘要（供 dsh-autotrans 插件解析）。"""
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def build_parser():
    p = argparse.ArgumentParser(
        prog="autotrans",
        description="英文 PDF → 中文 自动化翻译（DeepSeek API，术语表 + 断点续传，输出 DOCX/EPUB）。",
    )
    p.add_argument("action", nargs="?", default="all",
                   help="all | extract | translate | render（默认 all）")
    p.add_argument("pdf", nargs="?", help="PDF 文件路径")
    p.add_argument("--config", dest="config_path", help="JSON 配置文件路径")
    p.add_argument("--out-dir", dest="out_dir", help="输出目录（默认 <pdf>_translated/）")
    p.add_argument("--glossary", help="术语表：内置名 general/biomedical/anthropology 或 TSV 绝对路径")
    p.add_argument("--model", help="DeepSeek 模型（默认 deepseek-chat）")
    p.add_argument("--concurrency", type=int, help="并发请求数（默认 4）")
    p.add_argument("--format", choices=["docx", "epub", "both"], help="输出格式（默认 docx）")
    p.add_argument("--mode", choices=["cn", "bilingual"], default=None,
                   help="渲染模式：cn（仅中文，默认）；bilingual=中英对照（一段英文一段中文）")
    p.add_argument("--api-base", dest="api_base", help="API 基地址（默认 https://api.deepseek.com）")
    p.add_argument("--api-key", dest="api_key", help="DeepSeek API Key（默认读 DEEPSEEK_API_KEY 环境变量）")
    p.add_argument("--domain", help="翻译领域（general/biomedical/anthropology/life-science）")
    return p


def _resolve_action_and_pdf(args):
    """兼容四种调用：`<pdf>`、`all <pdf>`、`extract <pdf>`、`extract` + AUTOTRANS_PDF。

    最后一种用于非 ASCII 路径：Windows 下冻结运行时的 argv 无法可靠承载
    CJK 文件名（会被按 ANSI 解码而变成乱码），而环境变量是正常传递的，
    所以调用方可以只传 action，把 PDF 路径放进 AUTOTRANS_PDF。
    """
    env_pdf = os.environ.get("AUTOTRANS_PDF", "").strip()
    if args.pdf is None:
        # 只给了一个参数：可能是 `<pdf>`、`<action>`，或 <action> + 环境变量路径
        if args.action and args.action.lower().endswith(".pdf"):
            return "all", args.action
        if env_pdf and os.path.exists(env_pdf):
            action = (args.action or "all").lower()
            if action not in ACTIONS:
                print(f"未知动作 {args.action!r}，可用：{', '.join(ACTIONS)}", file=sys.stderr)
                sys.exit(2)
            return action, env_pdf
        print("缺少 PDF 路径。用法：python autotrans.py [action] <pdf> [选项]", file=sys.stderr)
        sys.exit(2)
    action = args.action.lower()
    if action not in ACTIONS:
        print(f"未知动作 {args.action!r}，可用：{', '.join(ACTIONS)}", file=sys.stderr)
        sys.exit(2)
    return action, args.pdf


def _apply_overrides(cfg, args):
    if args.out_dir:
        cfg["out_dir"] = args.out_dir
    if args.glossary:
        cfg["glossary"] = args.glossary
    if args.model:
        cfg["translation"]["model"] = args.model
    if args.concurrency:
        cfg["translation"]["concurrency"] = args.concurrency
    if args.format:
        cfg["render"]["format"] = args.format
    if args.mode:
        cfg["render"]["mode"] = args.mode
    if args.api_base:
        cfg["translation"]["api_base"] = args.api_base
    if args.domain:
        cfg["translation"]["domain"] = args.domain


def _list_outputs(out_dir):
    files = []
    for name in ("extracted.json", "translated.json", "translated_final.json"):
        p = os.path.join(out_dir, name)
        if os.path.exists(p):
            files.append(p)
    for name in sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else ():
        if name.lower().endswith((".docx", ".epub")):
            files.append(os.path.join(out_dir, name))
    return files


def main(argv=None):
    args = build_parser().parse_args(argv)
    action, pdf = _resolve_action_and_pdf(args)
    pdf_abs = os.path.abspath(pdf)
    if not os.path.exists(pdf_abs):
        print(f"PDF 不存在：{pdf_abs}", file=sys.stderr)
        _emit_result({"ok": False, "message": f"PDF 不存在：{pdf_abs}", "out_dir": "", "files": []})
        sys.exit(4)

    cfg = load_config(args.config_path)
    cfg["pdf_path"] = pdf_abs
    if not cfg.get("out_dir"):
        cfg["out_dir"] = os.path.splitext(pdf_abs)[0] + "_translated"
    _apply_overrides(cfg, args)
    cfg["glossary"] = resolve_glossary(cfg.get("glossary", ""))

    messages = []
    ok = True

    try:
        if action in ("extract", "all"):
            data = extract.run(cfg)
            n_units = sum(len(ch.get("paragraphs", [])) for ch in data.get("chapters", []))
            messages.append(f"提取完成：{n_units} 段，preview 见 {os.path.join(cfg['out_dir'], 'preview', 'article.txt')}")
            if action == "extract":
                _emit_result({"ok": True, "message": "；".join(messages),
                              "out_dir": cfg["out_dir"], "files": _list_outputs(cfg["out_dir"])})
                return 0

        if action in ("translate", "all"):
            api_key = (args.api_key or "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
            if not api_key:
                msg = ("未检测到 DEEPSEEK_API_KEY，跳过翻译。请设置环境变量 "
                       "DEEPSEEK_API_KEY 或传入 --api-key。")
                print(msg, file=sys.stderr)
                if action == "translate":
                    _emit_result({"ok": False, "message": msg, "out_dir": cfg["out_dir"],
                                  "files": _list_outputs(cfg["out_dir"])})
                    return 3
                messages.append(msg)
                ok = False
            else:
                translate.run(cfg, api_key)
                messages.append("翻译完成")

        if action in ("render", "all"):
            files = render.run(cfg)
            if files:
                messages.append("渲染完成：" + "、".join(os.path.basename(f) for f in files))
            else:
                messages.append("渲染跳过（未找到 translated_final.json）")

        _emit_result({"ok": ok, "message": "；".join(messages),
                      "out_dir": cfg["out_dir"], "files": _list_outputs(cfg["out_dir"])})
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001 - 转为结构化错误返回给插件
        print(f"错误：{exc}", file=sys.stderr)
        _emit_result({"ok": False, "message": f"错误：{exc}", "out_dir": cfg["out_dir"],
                      "files": _list_outputs(cfg["out_dir"])})
        return 1


if __name__ == "__main__":
    sys.exit(main())
