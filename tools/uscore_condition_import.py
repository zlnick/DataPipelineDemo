# -*- coding: utf-8 -*-
"""US Core Condition 值集 SNOMED 术语集（样本）入库 Terminology_SnomedUs.Concept。

数据源合并：
  1) data/testsets/uscore_condition_snomed_sample.csv（tx.fhir.org 公开展开样本）
  2) data/testsets/cn2snomed_testset_v2.csv（测试集真值 SNOMED 码/display，确保 lookup/validate 全覆盖）

幂等：DELETE 全表 + executemany 插入。
用法: python3 tools/uscore_condition_import.py
"""
import csv
import os

import iris.dbapi

HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
BASE = os.path.join(os.path.dirname(__file__), "..", "data", "testsets")
SAMPLE = os.path.join(BASE, "uscore_condition_snomed_sample.csv")
V2 = os.path.join(BASE, "cn2snomed_testset_v2.csv")
TABLE = "Terminology_SnomedUs.Concept"


def main():
    by_code: dict[str, str] = {}
    with open(SAMPLE, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            c = (r.get("code") or "").strip()
            d = (r.get("display") or "").strip()
            if c and d:
                by_code.setdefault(c, d)
    try:
        with open(V2, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                c = (r.get("snomed_code") or "").strip()
                d = (r.get("snomed_display") or "").strip()
                if c and d:
                    by_code.setdefault(c, d)
    except FileNotFoundError:
        pass
    rows = sorted(by_code.items())
    print("合计概念:", len(rows))

    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {TABLE}")
    conn.commit()
    for i in range(0, len(rows), 5000):
        cur.executemany(f"INSERT INTO {TABLE} (Code, Display) VALUES (?, ?)",
                        [(c, d) for c, d in rows[i:i + 5000]])
        conn.commit()
    print("导入完成:", len(rows), "条")
    conn.close()


if __name__ == "__main__":
    main()
