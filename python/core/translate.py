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
    if len(text) <= max_chunk:
        return [text]
    parts = re.split(r"(?<=[.!?;])\s+", text)
    chunks, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > max_chunk:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + " " + p) if cur else p
    if cur:
        chunks.append(cur)
    final = []
    for c in chunks:
        while len(c) > max_chunk:
            final.append(c[:max_chunk])
            c = c[max_chunk:]
        final.append(c)
    return final


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


def run(cfg, api_key):
    t = cfg["translation"]
    out_dir = cfg["out_dir"]
    data = json.load(open(os.path.join(out_dir, "extracted.json"), encoding="utf-8-sig"))
    glossary = _load_glossary(cfg.get("glossary", ""))
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
