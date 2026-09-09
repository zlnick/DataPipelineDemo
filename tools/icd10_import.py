# -*- coding: utf-8 -*-
"""国标 ICD-10 诊断术语集导入：icd10_main.csv + dagger 标志 -> Terminology_Icd10.Concept。

幂等：先 DELETE 全表再 executemany 插入（约 2 万条，秒级）。
用法: python3 tools/icd10_import.py
"""
import csv
import os

from openpyxl import load_workbook
import iris.dbapi

HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "terms-inbox", "icd10_main.csv")
SRC_XLSX = os.path.expanduser("~/Downloads/《疾病分类与代码》（GBT 14396-2016）国标版.xlsx")
TABLE = "Terminology_Icd10.Concept"


def dagger_codes() -> set:
    """Excel A 列带 † 的码集合（用于 Dagger 标志）。"""
    wb = load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    out = set()
    for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
        a = (row[0] or "").strip()
        if a.rstrip("†*").strip() != a:
            out.add(a.rstrip("†*").strip())
    return out


def main(exclude_z=False):
    dagger = dagger_codes()
    print("dagger 码数:", len(dagger))

    rows = []
    with open(CSV_PATH, encoding="utf-8-sig") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for code, name in rd:
            if exclude_z and code.strip().startswith("Z"):
                continue
            rows.append((code.strip(), name.strip(), 1 if code.strip() in dagger else 0))
    print("CSV 行:", len(rows))

    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {TABLE}")
    conn.commit()
    batch = []
    for i in range(0, len(rows), 5000):
        batch = rows[i:i + 5000]
        cur.executemany(
            f"INSERT INTO {TABLE} (Code, Name, Dagger) VALUES (?, ?, ?)", batch)
        conn.commit()
        print(f"  已导入 {i + len(batch)}/{len(rows)}", flush=True)
    print("导入完成：", len(rows), "条")
    conn.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--exclude-z", action="store_true", help="跳过 Z 章(健康状态/保健因素)条目")
    a = ap.parse_args()
    main(a.exclude_z)
