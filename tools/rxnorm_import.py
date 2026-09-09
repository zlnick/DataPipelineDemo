"""RxNorm RRF 导入：概念表 + 共享向量层（基础版）。

用法（按序）:
  1) 概念表:   python3 tools/rxnorm_import.py --import
               （RXNCONSO.RRF 的 SAB=RXNORM 全行 -> Terminology_Rxnorm.Concept，幂等）
  2) 向量导出: python3 tools/rxnorm_import.py --save-vec --ttys SCD,SBD,IN [--limit N]
               （首选名子集 -> embedding 服务 -> JSONL，只调 embedding 不连 IRIS）
  3) 向量灌库: python3 tools/term_embed.py --load <jsonl>
               （复用共享 TermEmbedding 通用 loader）

内存提示：步骤 2 是大流量 embedding，建议先 docker compose stop iris-terminology 腾内存（同药品目录经验）。
"""
import argparse
import csv
import json
import math
import os
import time
import urllib.request

import iris.dbapi

RX_DIR = "/Users/lzhu/Downloads/RxNorm_full_prescribe_current/rrf"
CONSO = os.path.join(RX_DIR, "RXNCONSO.RRF")
RX_URI = "http://www.nlm.nih.gov/research/umls/rxnorm"
RELEASE = "full_prescribe"
MODEL = "Qwen3-Embedding-0.6B"
EMBED_URL = "http://127.0.0.1:8001/v1/embeddings"
HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
CONCEPT_TABLE = "Terminology_Rxnorm.Concept"
# 输出放项目 data/ 目录（/tmp 在宿主重启/清空时会丢）
VEC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
# 向量化首选 TTY（按临床可处方层级；SY/PSN/TMSY 同义词不进向量，避免相近名刷屏）
DEFAULT_TTYS = ["SCD", "SBD", "IN"]


def iter_rxnorm_rows():
    """流式解析 RXNCONSO，产出 SAB=RXNORM 的 (rxcui, rxaUI, tty, str)。"""
    with open(CONSO, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            c = line.rstrip("|\n").split("|")
            if len(c) < 15 or c[11] != "RXNORM":
                continue
            yield c[0], c[7], c[12], c[14]


def import_concepts():
    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {CONCEPT_TABLE}")
    conn.commit()
    t0 = time.time()
    n = 0
    batch = []
    for rxcui, rxaui, tty, s in iter_rxnorm_rows():
        batch.append((rxcui, rxaui, tty, s[:900], "ENG"))
        n += 1
        if len(batch) >= 5000:
            _insert_batch(cur, conn, batch)
            batch.clear()
            print(f"  已导入 {n} 行 ({time.time()-t0:.0f}s)", flush=True)
    if batch:
        _insert_batch(cur, conn, batch)
    print(f"概念表完成：{n} 行 ({time.time()-t0:.0f}s)")
    conn.close()


def _insert_batch(cur, conn, batch):
    cur.executemany(
        f"INSERT INTO {CONCEPT_TABLE} (Rxcui, RxaUI, Tty, Str, Lang) VALUES (?, ?, ?, ?, ?)",
        batch)
    conn.commit()


def save_vec(ttys, limit=0, batch=200, resume=False):
    """抽首选名子集 -> embedding -> JSONL。

    resume=True 时按已写行数断点续传（append，不删旧文件），配合 wrapper 崩溃自动拉起。
    """
    ttys = [t.strip().upper() for t in ttys]
    names = {}          # rxcui -> str（保留首次出现；名称截断 240 防超长 pad 拖慢批量推理）
    order = []
    for rxcui, _rxaui, tty, s in iter_rxnorm_rows():
        if tty in ttys and rxcui not in names and s:
            names[rxcui] = s[:128]   # 名称截断 128（RxNorm 名基本 <100；防超长名拖慢批内 padding）
            order.append(rxcui)
            if limit and len(order) >= limit:
                break
    # 按名称长度排序：短名先跑（快），超长名集中到最后少量慢批 —— 避免长名 padding 拖慢全批
    order.sort(key=lambda r: len(names[r]))
    out_path = os.path.join(VEC_DIR, f"rxnorm_vec_{'_'.join(ttys)}.jsonl")

    skip = 0
    if resume and os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as fh:
            skip = sum(1 for _ in fh)
        if skip >= len(order):
            print(f"已全部完成（{skip} 条），无需继续", flush=True)
            return
    elif os.path.exists(out_path):
        os.remove(out_path)

    total = len(order)
    print(f"待向量化: {total} 条 (ttys={ttys})，resume 跳过 {skip} 条", flush=True)
    t0 = time.time()
    for i in range(skip, total, batch):
        chunk = order[i:i + batch]
        vecs = embed_batch([names[c] for c in chunk])
        with open(out_path, "a", encoding="utf-8") as fh:
            for rxcui, v in zip(chunk, vecs):
                fh.write(json.dumps({
                    "release": RELEASE, "uri": RX_URI, "code": rxcui,
                    "lang": "en", "text": names[rxcui], "model": MODEL,
                    "vec": [round(x, 6) for x in v]}, ensure_ascii=False) + "\n")
        print(f"  saved {i + len(chunk)}/{total} ({time.time()-t0:.0f}s)", flush=True)
    print("向量导出完成:", out_path)
    print("下一步: python3 tools/term_embed.py --load", out_path)


def embed_batch(texts, retries=4):
    body = json.dumps({"input": texts}, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(EMBED_URL, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            vecs = [d["embedding"] for d in sorted(out["data"], key=lambda x: x["index"])]
            return [normalize(v) for v in vecs]
        except Exception as e:
            last_err = e
            print(f"  embed 重试 {attempt+1}/{retries}: {e}", flush=True)
            time.sleep(3 + attempt * 3)
    raise last_err


def normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--import", action="store_true", dest="do_import",
                    help="RXNCONSO -> 概念表（全部 RXNORM 行）")
    ap.add_argument("--save-vec", action="store_true", dest="save_vec")
    ap.add_argument("--ttys", default=",".join(DEFAULT_TTYS), help="向量 TTY 子集，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="save-vec 限量冒烟")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--resume", action="store_true", help="save-vec 断点续传（按已有 JSONL 行数跳过）")
    args = ap.parse_args()

    if args.do_import:
        import_concepts()
    elif args.save_vec:
        save_vec([t for t in args.ttys.split(",") if t], args.limit, args.batch, args.resume)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
