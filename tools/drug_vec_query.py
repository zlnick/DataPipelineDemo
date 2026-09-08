"""药品向量检索查询（不写入）：--query 中文/英文均可。"""
import argparse
import json
import urllib.request

import iris.dbapi

EMBED_URL = "http://127.0.0.1:8001/v1/embeddings"


def embed(texts):
    body = json.dumps({"input": texts}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(EMBED_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return [d["embedding"] for d in sorted(out["data"], key=lambda x: x["index"])]


def vec_str(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="盐酸二甲双胍")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--cs", default="")
    args = ap.parse_args()
    conn = iris.dbapi.connect(hostname="127.0.0.1", port=51774, namespace="TERMINOLOGY",
                              username="superuser", password="SYS")
    cur = conn.cursor()
    qv = vec_str(embed([args.query])[0])
    sql = ("SELECT TOP ? CsId, Code, Display, "
           "VECTOR_DOT_PRODUCT(V, TO_VECTOR(?, DOUBLE)) AS sim "
           "FROM Terminology_Drug.Vec ")
    params = [args.top, qv]
    if args.cs:
        sql += "WHERE CsId = ? "
        params.append(args.cs)
    sql += "ORDER BY sim DESC"
    cur.execute(sql, tuple(params))
    print("向量检索:", args.query, "| cs:", args.cs or "*")
    for r in cur.fetchall():
        print(f"  [{r[0]}] {r[2]} (sim={r[3]:.3f})")
    conn.close()


if __name__ == "__main__":
    main()
