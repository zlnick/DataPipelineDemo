"""药品术语向量化：读取 terms-inbox TSV → 调本地 embedding 服务 → 写入 Terminology_Drug.Vec + 检索验证。

用法: python3 tools/drug_vectorize.py [--limit N] [--query 药名]
"""
import argparse
import csv
import json
import os
import time
import urllib.request

import iris.dbapi

EMBED_URL = "http://127.0.0.1:8001/v1/embeddings"
HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
BOX = os.path.join(os.path.dirname(__file__), "..", "data", "terms-inbox")


def embed_batch(texts):
    body = json.dumps({"input": texts}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(EMBED_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    vecs = [d["embedding"] for d in sorted(out["data"], key=lambda x: x["index"])]
    return vecs


def vec_str(vec):
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def load_rows(limit=0):
    rows = []
    for cs in ("nrdl", "cbih"):
        path = os.path.join(BOX, f"{cs}.tsv")
        with open(path, encoding="utf-8") as fh:
            r = csv.reader(fh, delimiter="\t")
            next(r, None)
            for row in r:
                if len(row) >= 3 and row[1] and row[2]:
                    rows.append((row[0], row[1], row[2]))
                    if limit and len(rows) >= limit:
                        return rows
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--query", default="阿基仑赛注射液")
    args = ap.parse_args()

    rows = load_rows(args.limit)
    print("待向量化条目:", len(rows))
    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute("DELETE FROM Terminology_Drug.Vec")
    conn.commit()

    B = 200
    inserted = 0
    t0 = time.time()
    for i in range(0, len(rows), B):
        chunk = rows[i:i + B]
        vecs = embed_batch([c[2] for c in chunk])
        for (cs, code, disp), v in zip(chunk, vecs):
            cur.execute(
                "INSERT INTO Terminology_Drug.Vec (CsId, Code, Display, V) "
                "VALUES (?, ?, ?, TO_VECTOR(?, DOUBLE))",
                (cs, code, disp, vec_str(v)))
        inserted += len(chunk)
        print(f"  已写入 {inserted}/{len(rows)} ({time.time() - t0:.0f}s)", flush=True)
    conn.commit()
    print("全部写入完成")

    if args.query:
        qv = vec_str(embed_batch([args.query])[0])
        cur.execute(
            "SELECT TOP 5 CsId, Code, Display, VECTOR_DOT_PRODUCT(V, TO_VECTOR(?, DOUBLE)) AS sim "
            "FROM Terminology_Drug.Vec ORDER BY sim DESC", (qv,))
        print("检索:", args.query)
        for r in cur.fetchall():
            print(f"  [{r[0]}] {r[2]} (sim={r[3]:.3f})")
    conn.close()


if __name__ == "__main__":
    main()
