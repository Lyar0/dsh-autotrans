#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.translate — 调用 DeepSeek API 批量翻译 extracted.json。

特性：段落级/块级断点续传（原子写盘）、长段按句边界拆块、术语表约束、
可选并发、健壮的 <seg> 解析与单段兜底。

输入：out_dir/extracted.json；输出：out_dir/translated.json（扁平续传）、
     out_dir/translated_final.json（段落级中英对照，供渲染）。
"""
import json
import os
import re
import sys
import time
import threading
import queue
import urllib.request
import urllib.error
from collections import defaultdict

DEFAULT_GLOSSARY_SYSTEM = """你是术语抽取助手。阅读给定的英文论文/文献选段，识别其中【该领域专业术语、专有名词、关键概念】及其中文规范译名。

要求：
1. 只输出技术/领域术语（含专有缩写、工具/平台/方法名若中文语境需译出时给出；像 Seurat、Scanpy、MERFISH、10x Genomics 这类保留英文原样的，也列出但中文译名用原文）。基因/蛋白名（如 PD-L1、WNT）无需列出。
2. 中文译名要准确、常用、统一；同词多译取最通用者。
3. 每条尽量独立成对：英文术语（可含括号缩写）\t中文译名
4. 只输出以制表符分隔的两列，每行一对，不要任何解释、编号或额外文字。没有就不输出任何内容。
5. 数量控制在最有价值的 20~60 条，覆盖全文反复出现且影响整体一致性的术语。"""


def _auto_glossary_system():
    return DEFAULT_GLOSSARY_SYSTEM


DEFAULT_SYSTEM = """你是一位专业的{domain}中英译者。请将用户给出的英文段落翻译成地道、专业、忠实、通顺的中文学术中文（科技/学术论文语体，客观严谨）。

翻译要求：
1. 严格遵循以下术语对照表（如提供）：凡出现表中英文词，一律采用表中指定译名，保持全篇统一。
{glossary_block}
2. 技术平台/公司/软件/工具名（如 Visium、Seurat、Scanpy、Giotto、MERFISH、seqFISH、10x Genomics 等）与基因/蛋白名（如 PD-L1、WNT、NOTCH）原则上保留英文原样；首次出现的缩写给出中文全称并括注缩写（如 单细胞RNA测序（scRNA-seq））。
3. 正文中的 [数字] 与 [a, b–c] 是文献引用/脚注序号，必须原样保留在译文对应位置，不要翻译、删除或改动。
4. 输入第 i 段的编号就是它的 id。保持段落数、顺序与输入完全一致，不要合并或拆分。
5. 技术名词、剂量、单位按科技惯例处理（μm、mm²、kb、bp 等照原样）。
6. 不要添加解释、评论、译者注或原文以外内容；忠实传达原意。

输出格式（严格遵守）：
对输入第 i 段输出一行：<seg id="i">该段的中文翻译</seg>
除这些 <seg> 标签外，不要输出任何其他文字、说明或注释。"""

DOMAIN_ZH = {
    "biomedical": "生物医学与生物信息学",
    "anthropology": "人类学与民族志",
    "life-science": "生命科学",
    "general": "学术",
}


def _load_glossary(path):
    out = []
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                out.append((parts[0].strip(), parts[1].strip()))
    return out


def _split_para(text, max_chunk):
    """按【句子边界】把超长段拆成 ≤ max_chunk 的块，避免在一句话中间硬截断。

    策略：先按句末标点(.!?;)拆成“句子单元”（跨缩写/小数点不完美，但不会切断词/句内部），
    再贪心组块直到接近 max_chunk。只有极少数“单句就超长”的句子会整体保留（宁可长度超一点，
    也绝不在句中断成两半分别翻译导致译意割裂）。绝对不做事先按 max_chunk 的中部硬切。
    """
    if len(text) <= max_chunk:
        return [text]
    # 句单元拆分：保留句末标点+空格
    pieces = re.findall(r"[^.!?;]*[.!?;]+(?:\s+|$)|[^.!?;]+$", text)
    pieces = [p.strip() for p in pieces if p and p.strip()]

    def push(chunks, cur):
        cur = cur.strip()
        if cur:
            chunks.append(cur)
        return ""

    chunks, cur = [], ""
    for piece in pieces:
        # 保留 split 时去掉的空格，避免粘连
        sep = " " if cur else ""
        cand = (cur + sep + piece).strip()
        if len(cand) <= max_chunk:
            cur = cand
            continue
        # 当前块已满：先冲掉已积累的句子
        if cur:
            chunks.append(cur.strip())
            cur = piece
            # 若连当前单句仍超长（罕见超长句），整体放入，绝不砍句
            while len(cur) > max_chunk:
                chunks.append(cur.strip())
                cur = ""
        else:
            cur = piece
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    for _ in range(3):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
            return
        except OSError:
            time.sleep(0.3)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _call(api_base, key, model, system, user, temp, max_tokens, timeout=300, retries=5):
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {"model": model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "temperature": temp, "max_tokens": max_tokens, "stream": False}
    body = json.dumps(payload).encode("utf-8")
    for a in range(retries):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            c = data["choices"][0]
            return c["message"]["content"], c.get("finish_reason")
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (429, 500, 502, 503):
                wait = 6 * (a + 1)
                print(f"   HTTP {code}，{wait}s 后重试...", flush=True)
                time.sleep(wait)
            else:
                raise RuntimeError(f"API HTTP {code}: {e.read().decode('utf-8')[:300]}")
        except urllib.error.URLError as e:
            print(f"   网络错误 {e}，重试...", flush=True)
            time.sleep(6 * (a + 1))
    raise RuntimeError("重试次数用尽")


def _parse_segs(txt):
    out = {}
    if not txt:
        return out
    parts = re.split(r'<seg\s+id="(\d+)"\s*>', txt)
    for i in range(1, len(parts), 2):
        try:
            sid = int(parts[i])
        except ValueError:
            continue
        content = parts[i + 1] if i + 1 < len(parts) else ""
        content = re.sub(r"</seg>.*$", "", content, flags=re.S).strip()
        if content:
            out[sid] = content
    return out


def _build_units(data, max_chunk):
    units = []

    def emit(sec, ci, pi, text):
        for k, c in enumerate(_split_para(text, max_chunk)):
            units.append({"sec": sec, "ci": ci, "pi": pi, "chunk_id": k, "text": c})

    for ci, ch in enumerate(data["chapters"]):
        for pi, p in enumerate(ch["paragraphs"]):
            emit("c", ci, pi, p["text"])
    for pi, p in enumerate(data["notes"].get("paragraphs", [])):
        emit("n", -1, pi, p["text"])
    return units


def _build_user(units, idx_list):
    lines = [f"请翻译以下 {len(idx_list)} 段英文。每段前数字就是它的 id："]
    for idx in idx_list:
        lines.append(f"{idx + 1}. {units[idx]['text']}")
    return "\n".join(lines)


def _load_system(cfg):
    f = cfg["translation"].get("system_prompt_file")
    if f and os.path.exists(f):
        tpl = open(f, encoding="utf-8-sig").read()
    else:
        tpl = DEFAULT_SYSTEM
    domain = cfg["translation"].get("domain", "general")
    domain_zh = DOMAIN_ZH.get(domain, domain)
    return tpl.replace("{domain}", domain_zh)


# ---------------- 自动 / 合并 术语表 ----------------

def _build_doc_sample(data, limit_urls=6000):
    """拼一段够 LLM 识别领域术语的文档代表文本：标题 + 前部正文片段。"""
    parts = []
    seen = set()
    cap = 0
    for ch in data.get("chapters", []):
        for p in ch.get("paragraphs", []):
            txt = (p.get("text") or "").strip()
            if not txt:
                continue
            if txt in seen:
                continue
            seen.add(txt)
            parts.append(txt)
            cap += len(txt)
            if cap >= limit_urls and len(parts) >= 8:
                break
        if cap >= limit_urls and len(parts) >= 8:
            break
    return "\n".join(parts)


def generate_auto_glossary(cfg, api_key, data):
    """用 LLM 从本文扫出该领域术语表（换域自动适配）。

    返回 (pairs, cache_path)。结果缓存到 out_dir/auto_glossary.tsv，可断点续用：
    已存在且健在则直接读取不重复调用 API。调用失败则告警并返回空（不影响主流程）。
    """
    t = cfg["translation"]
    out_dir = cfg["out_dir"]
    cache = os.path.join(out_dir, "auto_glossary.tsv")

    existing = _load_glossary(cache)
    if existing:
        print(f"复用自动术语表（{len(existing)} 条）：{cache}")
        return existing, cache

    if not api_key:
        return [], cache

    sample = _build_doc_sample(data)
    if not sample:
        return [], cache
    # 剪到约 6000 字符，避免超出上下文
    sample = sample[:6000]

    system = _auto_glossary_system()
    user = ("请阅读以下论文内容，抽取该领域专业术语并给出中文规范译名。\n"
            "严格按 英文术语\\t中文译名 每行一对输出，不要解释或编号：\n\n" + sample)

    try:
        raw, _ = _call(t["api_base"], api_key, t.get("model", "deepseek-chat"),
                       system, user, t.get("temperature", 0.0), 4096)
    except Exception as e:
        print(f"警告：自动术语表生成失败（{e}），继续用现有个/空白词表。")
        return [], cache

    pairs = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            pairs.append((parts[0].strip(), parts[1].strip()))
    # 去重（按英文词，保留首个中文译名）
    uniq, seen_en = [], set()
    for en, zh in pairs:
        if en.lower() in seen_en:
            continue
        seen_en.add(en.lower())
        uniq.append((en, zh))

    if uniq:
        os.makedirs(out_dir, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            f.write("# 自动生成的领域术语表（可用手写表覆盖/合并；删了会重新生成）\n")
            for en, zh in uniq:
                f.write(f"{en}\t{zh}\n")
        print(f"已生成自动术语表：{len(uniq)} 条 -> {cache}")
    return uniq, cache


def merge_glossaries(manual_pairs, auto_pairs):
    """自动表在后，不覆盖手写表中同英文词的译名（手写优先级更高）。"""
    merged = list(manual_pairs)
    seen_en = set(en.lower() for en, _ in manual_pairs)
    for en, zh in auto_pairs:
        if en.lower() not in seen_en:
            merged.append((en, zh))
            seen_en.add(en.lower())
    return merged


def _persist_user_glossary_pairs(pairs, path):
    """把【新增、不冲突】的词对持久化进用户词表库（append-only，会跨次累积）。

    规则：只新增英文键尚不存在于库中的词对；不覆盖已存在的任何译名。
    返回本轮实际新增的英文键集合（供打日志）。原子写法防中断。
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = {en.lower(): zh for en, zh in _load_glossary(path)}
        with open(path, "a", encoding="utf-8") as f:
            added = []
            for en, zh in pairs:
                if en.lower() not in existing:
                    f.write(f"{en}\t{zh}\n")
                    existing[en.lower()] = zh
                    added.append(en)
            f.flush()
        return added
    except Exception as e:
        print(f"警告：持久化用户词表失败（{e}）")
        return []


def load_user_glossary():
    """读取持久用户词表库（若已存在），供并入每次翻译。"""
    try:
        from core.config import user_glossary_path
        path = user_glossary_path()
        pairs = _load_glossary(path)
        return pairs, path
    except Exception:
        return [], ""


def run(cfg, api_key):
    t = cfg["translation"]
    out_dir = cfg["out_dir"]
    data = json.load(open(os.path.join(out_dir, "extracted.json"), encoding="utf-8-sig"))

    # 词表来源层级（前高后低 + 只增不改）：
    #   1) 显式 glossary(cfg)：内置/手动最高
    #   2) 持久用户词表库 user_glossary.tsv：地方累计的译名/审定，次高
    #   3) 本轮自动领域词表 auto_glossary.tsv：最低，仅填补空缺
    glossary = _load_glossary(cfg.get("glossary", ""))
    user_lib, user_lib_path = load_user_glossary()
    if user_lib:
        glossary = merge_glossaries(glossary, user_lib)

    # 自动领域词表（换域即用）；并存进用户词表库供跨次累积
    auto_pairs = []
    if t.get("auto_glossary", True):
        auto_generated, _ = generate_auto_glossary(cfg, api_key, data)
        auto_pairs = auto_generated
        if auto_generated and user_lib_path:
            newly = _persist_user_glossary_pairs(auto_generated, user_lib_path)
            if newly:
                print(f"已将 {len(newly)} 个新词并入用户词表库（累计可复用）: {user_lib_path}")
    glossary = merge_glossaries(glossary, auto_pairs)

    gl_block = "\n".join(f"{en}\t{zh}" for en, zh in glossary) if glossary else "（无）"
    system = _load_system(cfg).replace("{glossary_block}", gl_block)

    batch_chars = t.get("batch_chars", 2800)
    max_chunk = t.get("max_chunk", 900)
    max_tokens = t.get("max_tokens", 8192)

    # 复用已译段落（来自 translated_final.json）
    prior = {}
    final_path = os.path.join(out_dir, "translated_final.json")
    if os.path.exists(final_path):
        try:
            d = json.load(open(final_path, encoding="utf-8-sig"))
            for ci, ch in enumerate(d.get("chapters", [])):
                for pi, p in enumerate(ch.get("paragraphs", [])):
                    if (p.get("zh") or "").strip():
                        prior[("c", ci, pi)] = p["zh"].strip()
            for pi, p in enumerate(d.get("notes", {}).get("paragraphs", [])):
                if (p.get("zh") or "").strip():
                    prior[("n", -1, pi)] = p["zh"].strip()
        except Exception:
            prior = {}

    units = _build_units(data, max_chunk)
    out_path = os.path.join(out_dir, "translated.json")
    results = {}
    if os.path.exists(out_path):
        try:
            raw = json.load(open(out_path, encoding="utf-8-sig"))
            if raw and all(isinstance(v, str) for v in raw.values()):
                results = {int(k): v for k, v in raw.items()}
        except Exception:
            results = {}

    nchunk = defaultdict(int)
    for u in units:
        nchunk[(u["sec"], u["ci"], u["pi"])] += 1
    reused, pending = 0, []
    for idx, u in enumerate(units):
        keyp = (u["sec"], u["ci"], u["pi"])
        if nchunk[keyp] == 1 and prior.get(keyp):
            results[idx] = prior[keyp]
            reused += 1
            continue
        cur = results.get(idx, "")
        if not isinstance(cur, str) or not cur.strip():
            pending.append(idx)

    print(f"总块数 {len(units)}，复用 {reused}，待译 {len(pending)}")
    if pending:
        batches, cur, cc = [], [], 0
        for idx in pending:
            s = len(units[idx]["text"]) + 40
            if cur and cc + s > batch_chars:
                batches.append(cur)
                cur, cc = [], 0
            cur.append(idx)
            cc += s
        if cur:
            batches.append(cur)
        concurrency = max(1, t.get("concurrency", 4))
        print(f"{len(batches)} 批；模型={t['model']} 并发={concurrency} 术语={len(glossary)} 条")

        q = queue.Queue()
        for b in batches:
            q.put(b)
        lock = threading.Lock()
        fails, rechecks, done = {}, {}, 0

        def worker():
            nonlocal done
            while True:
                try:
                    idx_list = q.get_nowait()
                except queue.Empty:
                    return
                bk = tuple(idx_list)
                try:
                    user = _build_user(units, idx_list)
                    raw, _ = _call(t["api_base"], api_key, t["model"], system, user,
                                   t.get("temperature", 0.3), max_tokens)
                    segs = _parse_segs(raw)
                    if len(idx_list) == 1:
                        got = segs.get(idx_list[0] + 1, "")
                        if not got:
                            cleaned = re.sub(r"```.*?```", "", raw, flags=re.S).strip()
                            if cleaned:
                                segs = {idx_list[0] + 1: cleaned}
                    with lock:
                        still = []
                        for i, idx in enumerate(idx_list):
                            zh = segs.get(idx + 1, "")
                            if zh and zh.strip():
                                results[idx] = zh
                                done += 1
                            else:
                                still.append(idx)
                        _atomic_write(out_path, results)
                        if not still:
                            fails.pop(bk, None)
                            rechecks.pop(bk, None)
                            print(f"  批完成，累计 {done}/{len(pending)}", flush=True)
                        else:
                            rechecks[bk] = rechecks.get(bk, 0) + 1
                            if rechecks[bk] <= 2:
                                q.put(still)
                            else:
                                print(f"  {len(still)} 块仍缺，留给下次续传", flush=True)
                except Exception as e:
                    fails[bk] = fails.get(bk, 0) + 1
                    if fails[bk] < 3:
                        q.put(idx_list)
                    else:
                        print(f"  批失败 3 次：{e}", flush=True)
                finally:
                    q.task_done()

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        missing = sum(1 for idx in pending if not (results.get(idx, "") or "").strip())
        print(f"翻译结束：完成 {len(pending) - missing}/{len(pending)}（若未满可重跑续传）")

    # 组装回段落级
    final = json.loads(json.dumps(data))
    pmap = {}
    for idx, u in enumerate(units):
        pmap.setdefault((u["sec"], u["ci"], u["pi"]), {})[u["chunk_id"]] = results.get(idx, "")
    for ci, ch in enumerate(final["chapters"]):
        for pi, p in enumerate(ch["paragraphs"]):
            chs = pmap.get(("c", ci, pi), {})
            p["zh"] = "".join(chs[k] for k in sorted(chs) if chs[k]).strip()
    for pi, p in enumerate(final["notes"].get("paragraphs", [])):
        chs = pmap.get(("n", -1, pi), {})
        p["zh"] = "".join(chs[k] for k in sorted(chs) if chs[k]).strip()
    _atomic_write(final_path, final)
    print("结构化译文 ->", final_path)
    return final
