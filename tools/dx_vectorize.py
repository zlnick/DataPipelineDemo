# -*- coding: utf-8 -*-
"""诊断术语集全量向量化：中文国标 ICD-10（Terminology_Icd10.Concept 全量）→ TermEmbedding。

同时重建 SNOMED US Core 池（同 uri 幂等重灌，统一 release）。
用法:
  python3 tools/dx_vectorize.py --zh        # 中文 ICD-10 全量（~2 万）
  python3 tools/dx_vectorize.py --snomed    # SNOMED 池重建（2042）
默认两者都跑。输出 data/rxnorm_vec 无关，日志自行重定向。
"""
import argparse
import json
import math
import time
import urllib.request

import iris.dbapi

EMBED = "http://127.0.0.1:8001/v1/embeddings"
HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
ICD_URI = "urn:cn-nhsa:icd10-gbt2016"
SNOMED_URI = "urn:uscore:condition-sample"
MODEL = "Qwen3-Embedding-0.6B"


def embed_batch(texts, retries=4):
    body = json.dumps({"input": texts}, ensure_ascii=False).encode("utf-8")
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(EMBED, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.loads(r.read().decode("utf-8"))
            vecs = [d["embedding"] for d in sorted(out["data"], key=lambda x: x["index"])]
            return [norm(v) for v in vecs]
        except Exception as e:
            last = e
            print(f"  embed 重试 {a+1}/{retries}: {e}", flush=True)
            time.sleep(3 + a * 3)
    raise last


def norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def vs(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def load_rows(cur, table, lang):
    cur.execute(f"SELECT Code, Text FROM Terminology_Vector.TermEmbedding WHERE 1=0")  # no-op
    cur.execute(f"SELECT Code, Name FROM {table}")
    return [(str(r[0]), str(r[1])) for r in cur.fetchall() if r[0] and r[1]]


def vectorize(conn, cur, uri, release, rows, lang, batch=200):
    cur.execute("DELETE FROM Terminology_Vector.TermEmbedding WHERE SystemUri = ?", (uri,))
    conn.commit()
    n = len(rows)
    t0 = time.time()
    done = 0
    for i in range(0, n, batch):
        chunk = rows[i:i + batch]
        vecs = embed_batch([r[1][:240] for r in chunk])
        for (code, text), v in zip(chunk, vecs):
            cur.execute(
                "INSERT INTO Terminology_Vector.TermEmbedding "
                "(ReleaseId, SystemUri, Code, Lang, Text, Model, Embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, TO_VECTOR(?, DECIMAL))",
                (release, uri, code, lang, text[:240], MODEL, vs(v)))
        done += len(chunk)
        print(f"  {uri} {done}/{n} ({time.time()-t0:.0f}s)", flush=True)
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zh", action="store_true")
    ap.add_argument("--snomed", action="store_true")
    args = ap.parse_args()
    do_zh = args.zh or not (args.zh or args.snomed)
    do_sn = args.snomed or not (args.zh or args.snomed)

    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    # 预热（中英混合）
    embed_batch(["metformin", "阿莫西林胶囊"])

    if do_sn:
        rows = []
        cur.execute("SELECT Code, Display FROM Terminology_SnomedUs.Concept")
        rows = [(str(r[0]), str(r[1])) for r in cur.fetchall() if r[0] and r[1]]
        print("SNOMED 池:", len(rows))
        vectorize(conn, cur, SNOMED_URI, "uscore-cond-sample", rows, "en")
    if do_zh:
        rows = load_rows(cur, "Terminology_Icd10.Concept", "zh")
        print("中文 ICD-10:", len(rows))
        vectorize(conn, cur, ICD_URI, "gbt2016", rows, "zh")
    print("全部完成")
    conn.close()


if __name__ == "__main__":
    main()
