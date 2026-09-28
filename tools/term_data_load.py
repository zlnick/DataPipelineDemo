# -*- coding: utf-8 -*-
"""把随仓库分发的术语素材灌入术语服务器（幂等 upsert，不删已有数据）。

素材（data/terms-inbox/，随仓库分发）：
- icd10_main.csv（国标 ICD-10，表头 code,name，约 2 万条）  -> Terminology_Icd10.Concept(Code, Name, Dagger)
- nrdl.tsv / cbih.tsv（中文药品目录，9 列 TSV：CodeSystemId,Code,Display,Form,Note,Category,Section,Extras,SrcPage）
                                                            -> Terminology_Drug.Code(...)

为什么必须走 DB-API：术语服务器（裁剪版 iris-terminology）只提供 GET 读路由，
没有写入/导入 REST 端点，故只能直连写表（与上游 tools/drug_import_db.py / icd10_import.py 同法）。

用法（backend 容器内；容器内可解析主机名 iris-terminology）：
    python /tmp/term_data_load.py [--dir /tmp/terms-inbox] [--wait 120]

幂等：`INSERT OR UPDATE`，重复执行不会产生重复行，也不会抹掉库里已有的其它术语。
"""
import argparse
import csv
import os
import sys
import time

import iris.dbapi

ICD_TABLE = "Terminology_Icd10.Concept"
DRUG_TABLE = "Terminology_Drug.Code"


def connect(host: str, wait: int = 0):
    """连接 TERMINOLOGY 命名空间；wait>0 时按秒重试（等术语服务器就绪）。"""
    t0 = time.time()
    while True:
        try:
            return iris.dbapi.connect(hostname=host, port=1972, namespace="TERMINOLOGY",
                                      username="superuser", password="SYS")
        except Exception:  # noqa: BLE001 - 就绪前连接失败属正常
            if time.time() - t0 >= wait:
                raise
            time.sleep(5)


def nn(v):
    """空串转 None（避免 IRIS 把空串读回 \\u0000）。"""
    c = str(v or "").replace("\x00", "").strip()
    return c if c else None


def clip(v, n):
    """按目标列长度截断，避免超长导入失败。"""
    c = nn(v)
    return c[:n] if c else None


def load_icd10(cur, path: str) -> int:
    """导入 ICD-10 诊断（code,name）-> Terminology_Icd10.Concept。"""
    if not os.path.isfile(path):
        print("  [skip] 缺文件:", path)
        return 0
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        rd = csv.reader(fh)
        next(rd, None)  # 表头
        for rec in rd:
            if len(rec) < 2:
                continue
            code, name = nn(rec[0]), nn(rec[1])
            if code and name:
                rows.append((code[:60], name[:200], 0))
    for i in range(0, len(rows), 2000):
        batch = rows[i:i + 2000]
        cur.executemany(
            "INSERT OR UPDATE INTO %s (Code, Name, Dagger) VALUES (?, ?, ?)" % ICD_TABLE,
            batch)
    print("  ICD-10: %d 条" % len(rows))
    return len(rows)


def load_drug(cur, path: str) -> int:
    """导入中文药品目录（9 列 TSV）-> Terminology_Drug.Code。"""
    if not os.path.isfile(path):
        print("  [skip] 缺文件:", path)
        return 0
    rows = []
    with open(path, encoding="utf-8") as fh:
        rd = csv.reader(fh, delimiter="\t")
        next(rd, None)  # 表头：CodeSystemId Code Display Form Note Category Section Extras SrcPage
        for rec in rd:
            if len(rec) < 3:
                continue
            sys_id, code, disp = nn(rec[0]), nn(rec[1]), nn(rec[2])
            if not (sys_id and code and disp):
                continue
            form = clip(rec[3], 110) if len(rec) > 3 else None
            note = clip(rec[4], 400) if len(rec) > 4 else None
            cate = clip(rec[5], 70) if len(rec) > 5 else None
            sect = clip(rec[6], 35) if len(rec) > 6 else None
            extr = clip(rec[7], 1500) if len(rec) > 7 else None
            page = None
            if len(rec) > 8:
                try:
                    page = int((rec[8] or "0").strip() or 0)
                except ValueError:
                    page = None
            rows.append((sys_id[:30], code[:60], disp[:150], form, note, cate, sect, extr, page))
    for i in range(0, len(rows), 2000):
        batch = rows[i:i + 2000]
        cur.executemany(
            "INSERT OR UPDATE INTO %s (CodeSystemId, Code, Display, Form, Note, "
            "Category, SectionName, Extras, SrcPage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)" % DRUG_TABLE,
            batch)
    print("  药品(%s): %d 条" % (os.path.basename(path), len(rows)))
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/tmp/terms-inbox", help="素材目录（容器内）")
    ap.add_argument("--host", default=os.getenv("TERM_IRIS_HOST", "iris-terminology"))
    ap.add_argument("--wait", type=int, default=120, help="等术语服务器就绪的秒数")
    a = ap.parse_args()

    conn = connect(a.host, a.wait)
    cur = conn.cursor()
    total = 0
    total += load_icd10(cur, os.path.join(a.dir, "icd10_main.csv"))
    total += load_drug(cur, os.path.join(a.dir, "nrdl.tsv"))
    total += load_drug(cur, os.path.join(a.dir, "cbih.tsv"))
    conn.commit()

    for tbl in (ICD_TABLE, DRUG_TABLE):
        cur.execute("SELECT COUNT(*) FROM %s" % tbl)
        print("  %s 现有行数: %s" % (tbl, cur.fetchone()[0]))
    conn.close()
    print("术语概念导入完成：本次处理 %d 条（幂等）" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
