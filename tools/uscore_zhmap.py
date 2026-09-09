# -*- coding: utf-8 -*-
"""中文诊断 → SNOMED 双语词表灌数：从 cn2snomed_testset_v2.csv（52 条人工核对真值）写入
Terminology_SnomedUs.ChineseAlias（近义词先决，修复原发性高血压等被英文异体挤序问题）。
幂等：DELETE 全表重建。用法: python3 tools/uscore_zhmap.py
"""
import csv
import os

import iris.dbapi

HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
V2 = os.path.join(os.path.dirname(__file__), "..", "data", "testsets", "cn2snomed_testset_v2.csv")
TABLE = "Terminology_SnomedUs.ChineseAlias"


def main():
    rows = []
    with open(V2, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r.get("cn_code") and r.get("snomed_code") and r.get("cn_name"):
                rows.append((r["cn_name"], r["cn_code"], r["snomed_code"],
                             r.get("snomed_display", "")))
    print("词条:", len(rows))
    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {TABLE}")
    conn.commit()
    for i in range(0, len(rows), 2000):
        cur.executemany(
            f"INSERT INTO {TABLE} (ZhName, CnCode, Code, Display) VALUES (?, ?, ?, ?)",
            rows[i:i + 2000])
        conn.commit()
    print("写入完成:", len(rows), "条")
    conn.close()


if __name__ == "__main__":
    main()
