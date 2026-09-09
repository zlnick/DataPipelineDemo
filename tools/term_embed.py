"""通用术语向量化：任意术语集 TSV -> 本地 embedding 服务 -> 上游共享向量层 Terminology_Vector.TermEmbedding。

对齐上游 iris-terminology-server 做法：所有术语集共用一张 TermEmbedding 表，按 SystemUri/ReleaseId/Lang 区分。
入库前 L2 归一化（与 Utils.EncodeText 的归一化一致，点积 = 余弦相似度）。

用法:
  python3 tools/term_embed.py --all                    # 登记的所有术语集入库（幂等）
  python3 tools/term_embed.py --cs nrdl --limit 300    # 单术语集 + 限量冒烟
  python3 tools/term_embed.py --query 阿基仑赛          # 向量检索（等价 REST GET /terminology/vector/search）
  python3 tools/term_embed.py --query "Metformin" --cs nrdl --top 5
"""
import argparse
import csv
import json
import math
import os
import time
import urllib.request

import iris.dbapi

EMBED_URL = "http://127.0.0.1:8001/v1/embeddings"
HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
BOX = os.path.join(os.path.dirname(__file__), "..", "data", "terms-inbox")
MODEL = "Qwen3-Embedding-0.6B"
RELEASE = "2025"  # 医保/商保目录版次占位（同一术语集多版本并存时可区分）

# 术语集注册表：新增术语集只在此登记即可接入向量层
SYSTEMS = {
    "nrdl": dict(file="nrdl.tsv", uri="urn:cn-nhsa:drug-nrdl",
                 desc="国家医保药品目录"),
    "cbih": dict(file="cbih.tsv", uri="urn:cn-nhsa:drug-cbih",
                 desc="商业健康险创新药目录", brand_alias=True),
}


def embed_batch(texts, retries=4):
    body = json.dumps({"input": texts}, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(EMBED_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            vecs = [d["embedding"] for d in sorted(out["data"], key=lambda x: x["index"])]
            return [normalize(v) for v in vecs]
        except Exception as e:  # 连接断开/服务重启时重试
            last_err = e
            print(f"  embed 重试 {attempt + 1}/{retries}: {e}", flush=True)
            time.sleep(3 + attempt * 3)
    raise last_err


def normalize(vec):
    """L2 归一化，使点积 = 余弦相似度（与 IRIS 端 Utils.EncodeText 一致）。"""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def vec_str(vec):
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def load_cs(cs, limit=0):
    """读术语集 TSV -> [(system_uri, code, text)]；cbih 附加 Extras.brand 作为别名行。"""
    cfg = SYSTEMS[cs]
    path = os.path.join(BOX, cfg["file"])
    rows = []
    with open(path, encoding="utf-8") as fh:
        r = csv.reader(fh, delimiter="\t")
        next(r, None)  # header
        for row in r:
            if len(row) < 3 or not row[1] or not row[2]:
                continue
            rows.append((cfg["uri"], row[1], row[2]))
            if cfg.get("brand_alias") and len(row) > 7:
                try:
                    meta = json.loads(row[7] or "{}")
                except ValueError:
                    meta = {}
                if meta.get("brand"):
                    rows.append((cfg["uri"], row[1], meta["brand"]))
            if limit and len(rows) >= limit:
                return rows
    return rows


def upsert(conn, cur, cs, rows, batch=100):
    cfg = SYSTEMS[cs]
    cur.execute("DELETE FROM Terminology_Vector.TermEmbedding WHERE SystemUri = ? AND ReleaseId = ?",
                (cfg["uri"], RELEASE))
    conn.commit()
    inserted = 0
    t0 = time.time()
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        vecs = embed_batch([r[2] for r in chunk])
        for (uri, code, text), v in zip(chunk, vecs):
            cur.execute(
                "INSERT INTO Terminology_Vector.TermEmbedding "
                "(ReleaseId, SystemUri, Code, Lang, Text, Model, Embedding) "
                "VALUES (?, ?, ?, 'zh', ?, ?, TO_VECTOR(?, DECIMAL))",
                (RELEASE, uri, code, text, MODEL, vec_str(v)))
        inserted += len(chunk)
        print(f"  [{cs}] 已写入 {inserted}/{len(rows)} ({time.time() - t0:.0f}s)", flush=True)
    conn.commit()


def search(cur, query, cs="", top=5):
    qv = vec_str(embed_batch([query])[0])
    sql = ("SELECT TOP ? SystemUri, ReleaseId, Code, Lang, Text, Model, "
           "VECTOR_DOT_PRODUCT(Embedding, TO_VECTOR(?, DECIMAL)) AS Score "
           "FROM Terminology_Vector.TermEmbedding ")
    params = [top, qv]
    if cs:
        sql += "WHERE SystemUri = ? "
        params.append(SYSTEMS[cs]["uri"])
    sql += "ORDER BY Score DESC"
    cur.execute(sql, tuple(params))
    out = []
    for r in cur.fetchall():
        out.append((r[0], r[2], r[4], r[5], r[6]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cs", choices=list(SYSTEMS), default=None)
    ap.add_argument("--all", action="store_true", help="全部登记术语集入库")
    ap.add_argument("--limit", type=int, default=0, help="每术语集限量（冒烟）")
    ap.add_argument("--batch", type=int, default=100, help="embedding 批大小（内存紧张时可调小）")
    ap.add_argument("--query", default=None, help="向量检索模式（不写库）")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--save", default=None, help="仅向量化并写出 JSONL（不连 IRIS；先停 iris-terminology 腾内存跑）")
    ap.add_argument("--load", default=None, help="从 JSONL 灌入共享向量表（需 iris-terminology 在线；不调 embedding）")
    ap.add_argument("--no-delete", action="store_true", help="load 时不清除该 SystemUri 已有向量（追加子集）")
    args = ap.parse_args()

    if args.save:
        # 阶段1：只调 embedding 服务，写出向量文件（无需 IRIS）
        targets = list(SYSTEMS) if args.all else [args.cs] if args.cs else list(SYSTEMS)
        if os.path.exists(args.save):
            os.remove(args.save)
        total = 0
        for cs in targets:
            rows = load_cs(cs, args.limit)
            print(f"[{cs}] {SYSTEMS[cs]['desc']}: 向量化 {len(rows)} 条", flush=True)
            for i in range(0, len(rows), args.batch):
                chunk = rows[i:i + args.batch]
                vecs = embed_batch([r[2] for r in chunk])
                with open(args.save, "a", encoding="utf-8") as fh:
                    for (uri, code, text), v in zip(chunk, vecs):
                        fh.write(json.dumps({"release": RELEASE, "uri": uri, "code": code,
                                             "lang": "zh", "text": text, "model": MODEL,
                                             "vec": [round(x, 6) for x in v]},
                                            ensure_ascii=False) + "\n")
                total += len(chunk)
                print(f"  saved {total} ({os.path.getsize(args.save) / 1e6:.1f}MB)", flush=True)
        print("阶段1完成:", args.save)
        return

    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()

    if args.load:
        # 阶段2：从 JSONL 批量写入共享向量表（无需 embedding 服务）
        entries = []
        uris = set()
        seen = set()  # 按 PK 去重（TSV 源可能有重复行）
        with open(args.load, encoding="utf-8") as fh:
            for line in fh:
                o = json.loads(line)
                k = (o["uri"], o["code"], o["lang"], o["text"])
                if k in seen:
                    continue
                seen.add(k)
                uris.add(o["uri"])
                entries.append(o)
        if not args.no_delete:
            for u in uris:
                cur.execute("DELETE FROM Terminology_Vector.TermEmbedding WHERE SystemUri = ?", (u,))
            conn.commit()
        t0 = time.time()
        for i in range(0, len(entries), 500):
            for o in entries[i:i + 500]:
                cur.execute(
                    "INSERT INTO Terminology_Vector.TermEmbedding "
                    "(ReleaseId, SystemUri, Code, Lang, Text, Model, Embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, TO_VECTOR(?, DECIMAL))",
                    (o["release"], o["uri"], o["code"], o["lang"], o["text"],
                     o["model"], vec_str(o["vec"])))
            conn.commit()
            print(f"  已灌入 {i + 500 if i + 500 <= len(entries) else len(entries)}/{len(entries)} "
                  f"({time.time() - t0:.0f}s)", flush=True)
        print("阶段2完成，共", len(entries), "条")
        conn.close()
        return

    if args.query:
        print("向量检索:", args.query, "| cs:", args.cs or "*")
        for uri, code, text, model, score in search(cur, args.query, args.cs, args.top):
            print(f"  [{uri.split(':')[-1]}] {text} ({code}) sim={score:.4f}")
        conn.close()
        return

    targets = list(SYSTEMS) if args.all else [args.cs] if args.cs else list(SYSTEMS)
    for cs in targets:
        rows = load_cs(cs, args.limit)
        print(f"待向量化 [{cs}] {SYSTEMS[cs]['desc']}: {len(rows)} 条")
        upsert(conn, cur, cs, rows, batch=args.batch)
    print("全部完成。REST 等价端点: GET http://localhost:52774/terminology/vector/search?q=...")
    conn.close()


if __name__ == "__main__":
    main()
