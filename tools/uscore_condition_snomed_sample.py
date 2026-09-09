# -*- coding: utf-8 -*-
"""抓取 US Core Condition Codes 值集的 SNOMED CT 展开样本（tx.fhir.org 公开匿名）。

产物: data/testsets/uscore_condition_snomed_sample.csv（code,display）
说明: 完整 is-a Clinical finding 展开极大（需 SNOMED license/术语服务器），
      本工具取公开可拿的子集样本，仅用于演示测试集构建，不做生产断言。
用法: python3 tools/uscore_condition_snomed_sample.py [--count 5000]
"""
import argparse
import csv
import json
import os
import urllib.parse
import urllib.request

VS = "http://hl7.org/fhir/us/core/ValueSet/us-core-condition-code"
EXPAND = "https://tx.fhir.org/r4/ValueSet/$expand"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "testsets",
                   "uscore_condition_snomed_sample.csv")


def fetch(count: int, offset: int) -> list:
    url = (f"{EXPAND}?url={urllib.parse.quote(VS, safe='')}"
           f"&count={count}&offset={offset}&_format=json")
    req = urllib.request.Request(url, headers={"User-Agent": "terminology-demo/1.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d.get("expansion", {}).get("contains", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=2000)
    ap.add_argument("--pages", type=int, default=1)
    args = ap.parse_args()

    rows, seen = [], set()
    for p in range(args.pages):
        try:
            batch = fetch(args.count, p * args.count)
        except Exception as exc:
            print(f"第 {p+1} 页抓取失败: {exc}")
            break
        for x in batch:
            if x.get("system", "").endswith("/sct") and x.get("code") not in seen:
                seen.add(x["code"])
                rows.append((x["code"], x.get("display", "")))
        print(f"页 {p+1}: +{len(batch)} 行，累计 {len(rows)}", flush=True)
        if len(batch) < args.count:
            break

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "display"])
        w.writerows(rows)
    print(f"样本落盘: {len(rows)} 行 -> {OUT}")


if __name__ == "__main__":
    main()
