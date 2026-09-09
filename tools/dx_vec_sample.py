# -*- coding: utf-8 -*-
"""诊断向量化取样验证：中文国标 ICD-10（取样 N 条）→ SNOMED US Core 样本池（全 2042）语义检索。

仅向量化：
  A. SNOMED 样本池 2042（作候选/目标池，规模小）；
  B. 中文 ICD-10 取样 N 条（默认 10，来自 cn2snomed_testset_v2.csv 真值）。
评估：每条中文诊断向量检索 SNOMED 池 Top5，报告正确目标（v2 真值 SNOMED code）命中位次与分数。
验证后输出 data/testsets/dx_vec_verify_report.txt。不触发全量向量化。
"""
import csv
import json
import math
import os
import time
import urllib.request

import iris.dbapi

EMBED = "http://127.0.0.1:8001/v1/embeddings"
HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
BASE = os.path.join(os.path.dirname(__file__), "..", "data", "testsets")
V2 = os.path.join(BASE, "cn2snomed_testset_v2.csv")
REPORT = os.path.join(BASE, "dx_vec_verify_report.txt")
ICD_URI = "urn:cn-nhsa:icd10-gbt2016"   # 中文诊断（取样）
SNOMED_URI = "urn:uscore:condition-sample"  # SNOMED 样本池
MODEL = "Qwen3-Embedding-0.6B"


def embed_batch(texts):
    body = json.dumps({"input": texts}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(EMBED, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return [norm(v) for v in [d["embedding"] for d in sorted(out["data"], key=lambda x: x["index"])]]


def norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def vs(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def main(n: int = 10):
    # 1) 测试集真值（跨系统取前 n 行）
    truth = []
    with open(V2, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r.get("cn_code") and r.get("snomed_code"):
                truth.append(r)
    sample = truth[:n]
    report = [f"取样中文诊断 {len(sample)} 条；SNOMED 池：全量读取概念表\n"]

    # 2) SNOMED 池：直接从概念表拉（与 /terminology/uscore-condition 一致）
    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute("SELECT Code, Display FROM Terminology_SnomedUs.Concept")
    pool = cur.fetchall()
    report.append(f"SNOMED 池规模: {len(pool)}\n")

    # 3) 预热（中英混合，防冷启动首个中文不稳）
    embed_batch(["metformin", "二甲双胍"])

    # ---- SNOMED 池向量化（英文）----
    _vec_buf = {}
    B = 100
    t0 = time.time()
    texts = [p[1][:240] for p in pool]
    done = 0
    cur.execute("DELETE FROM Terminology_Vector.TermEmbedding WHERE SystemUri = ?", (SNOMED_URI,))
    conn.commit()
    for i in range(0, len(texts), B):
        chunk = texts[i:i + B]
        vecs = embed_batch(chunk)
        for p, v in zip(pool[i:i + B], vecs):
            cur.execute(
                "INSERT INTO Terminology_Vector.TermEmbedding "
                "(ReleaseId, SystemUri, Code, Lang, Text, Model, Embedding) "
                "VALUES (?, ?, ?, 'en', ?, ?, TO_VECTOR(?, DECIMAL))",
                ("dx-sample", SNOMED_URI, p[0], p[1][:240], MODEL, vs(v)))
        done += len(chunk)
        print(f"  SNOMED 池 {done}/{len(texts)} ({time.time()-t0:.0f}s)", flush=True)
    conn.commit()

    # ---- 中文取样向量化 ----
    cur.execute("DELETE FROM Terminology_Vector.TermEmbedding WHERE SystemUri = ?", (ICD_URI,))
    conn.commit()
    zh = [r["cn_name"] for r in sample]
    zh_vecs = dict(zip(zh, embed_batch(zh)))
    for r in sample:
        cur.execute(
            "INSERT INTO Terminology_Vector.TermEmbedding "
            "(ReleaseId, SystemUri, Code, Lang, Text, Model, Embedding) "
            "VALUES (?, ?, ?, 'zh', ?, ?, TO_VECTOR(?, DECIMAL))",
            ("dx-sample", ICD_URI, r["cn_code"], r["cn_name"], MODEL, vs(zh_vecs[r["cn_name"]])))
    conn.commit()
    print("中文取样向量化完成")

    # 5) 逐条检索评估
    hit1 = hit3 = hit5 = 0
    report.append(f"\n{'中文诊断':<18}{'目标 SNOMED':<46}命中@Top rank@score")
    for r in sample:
        q = r["cn_name"]
        target = r["snomed_code"]
        qv = vs(zh_vecs[q])
        cur.execute(
            "SELECT TOP 5 Code, Text, VECTOR_DOT_PRODUCT(Embedding, TO_VECTOR(?, DECIMAL)) AS S "
            "FROM Terminology_Vector.TermEmbedding WHERE SystemUri = ? ORDER BY S DESC",
            (qv, SNOMED_URI))
        cand = cur.fetchall()
        rank = next((i + 1 for i, (c, _, _) in enumerate(cand) if c == target), None)
        line = f"{q:<16}{r['cn_code']}->{target:<12}"
        if rank:
            hit5 += 1
            if rank <= 3:
                hit3 += 1
            if rank == 1:
                hit1 += 1
            line += f"  HIT Top{rank}  score={float(cand[rank-1][2]):.3f}"
        else:
            tops = ", ".join(f"{c}({float(s):.2f})" for c, _, s in cand[:3])
            line += f"  MISS   top: {tops}"
        report.append(line)
    report.append(f"\n==== 结果 ====\nTop1 命中: {hit1}/{len(sample)}  Top3: {hit3}/{len(sample)}  Top5: {hit5}/{len(sample)}")

    open(REPORT, "w", encoding="utf-8").write("\n".join(report))
    print("\n".join(report))
    conn.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    main(a.n)
