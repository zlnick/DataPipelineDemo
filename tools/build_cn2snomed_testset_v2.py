# -*- coding: utf-8 -*-
"""中文诊断测试集 v2：国标 ICD-10 ↔ US Core SNOMED（覆盖各系统 ~50 病种）。

输入: data/testsets/cn2snomed_seed50.tsv
  列: cn_kw | code_override(有则锁定 SNOMED 码) | filter(否则 tx expand 关键字) | want(display 匹配) | note(可含 excludeXXX 中文排除)
中文定位: 国标表名 LIKE kw，最短优先 + note 排除词。
SNOMED: override 码取自 uscore 样本(display 由样本/v1 提供)；filter 走 tx.fhir.org 匿名展开，
        display 先小写等值匹配 want，再包含匹配。
产物: data/testsets/cn2snomed_testset_v2.csv + 预览/告警 _build_v2.txt（不入 IRIS）
"""
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request

BASE = os.path.join(os.path.dirname(__file__), "..", "data", "testsets")
CN_CSV = os.path.join(BASE, "..", "terms-inbox", "icd10_main.csv")
SAMPLE = os.path.join(BASE, "uscore_condition_snomed_sample.csv")
SEED = os.path.join(BASE, "cn2snomed_seed50.tsv")
OUT = os.path.join(BASE, "cn2snomed_testset_v2.csv")
REPORT = os.path.join(BASE, "_build_v2.txt")
VS = "http://hl7.org/fhir/us/core/ValueSet/us-core-condition-code"
EXPAND = "https://tx.fhir.org/r4/ValueSet/$expand"

_cache: dict[str, list] = {}
CACHE_FILE = os.path.join(BASE, "_f_cache.json")
if os.path.exists(CACHE_FILE):
    try:
        _cache.update(json.load(open(CACHE_FILE, encoding="utf-8")))
    except Exception:
        pass


def snomed_filter(filter_word: str) -> list:
    """tx expand filter 抓取（缓存 + 重试一次）。"""
    if filter_word in _cache:
        return _cache[filter_word]
    url = (f"{EXPAND}?url={urllib.parse.quote(VS, safe='')}"
           f"&filter={urllib.parse.quote(filter_word)}&count=300&_format=json")
    req = urllib.request.Request(url, headers={"User-Agent": "terminology-demo/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        time.sleep(2)
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode("utf-8"))
    items = [x for x in d.get("expansion", {}).get("contains", [])
             if x.get("system", "").endswith("/sct")]
    _cache[filter_word] = items
    json.dump(_cache, open(CACHE_FILE, "w", encoding="utf-8"))
    time.sleep(0.3)
    return items


def pick_display(items, want: str):
    """返回 (code, display)；先等值；包含匹配优先无修饰限定的最短项。"""
    w = want.lower()
    for x in items:
        if (x.get("display") or "").lower() == w:
            return x["code"], x["display"]
    # 修饰限定词：优先排除这些（表示子型/伴随情形，演示测试集要主诊断级）
    dirty = ("without ", "caused by ", "due to ", "following ", "secondary to ",
             "complicating ", " in mother", " in pregnancy", "suspected ", "neonatal ", "alkaline ", "cystine ", "with obstruction", "sequelae ")
    best = None
    for x in items:
        d = (x.get("display") or "").lower()
        if w not in d:
            continue
        if any(k in d for k in dirty):
            continue
        if best is None or len(d) < len(best[1]):
            best = (x["code"], x.get("display"))
    if best:
        return best
    for x in items:
        if w in (x.get("display") or "").lower():
            return x["code"], x["display"]
    return None, None


def resolve_snomed(override: str, f: str, want: str, sample_map, v1_map):
    """返回 (code, display, source, warn)。"""
    if override and override != "-":
        disp = sample_map.get(override) or v1_map.get(override)
        if disp:
            return override, disp, "sample", ""
        return override, want, "override", "override 码不在样本/v1，display 用 want 占位"
    if f and f != "-":
        try:
            code, disp = pick_display(snomed_filter(f), want)
        except Exception as exc:
            return None, None, "filter", f"filter 抓取失败: {exc}"
        if code:
            return code, disp, "filter", ""
        return None, None, "filter", f"filter={f} 无 want={want} 匹配"
    return None, None, "-", "无 override 也无 filter"


def resolve_cn(kw: str, note: str):
    excludes = re.findall(r"exclude(\S+)", note)
    best, count = None, 0
    with open(CN_CSV, encoding="utf-8-sig") as fh:
        for code, name in csv.reader(fh):
            if kw not in name:
                continue
            if any(e in name for e in excludes):
                continue
            count += 1
            if best is None or len(name) < len(best[1]):
                best = (code, name)
    return best, count


def main():
    sample_map = {}
    with open(SAMPLE, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            sample_map[r["code"]] = r["display"]
    v1_map = {}
    try:
        with open(os.path.join(BASE, "cn2snomed_testset_v1.csv"), encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                v1_map[r["snomed_code"]] = r["snomed_display"]
    except FileNotFoundError:
        pass

    seed = list(csv.DictReader(open(SEED, encoding="utf-8"), delimiter="\t"))
    rows, warns = [], []
    for i, s in enumerate(seed, 1):
        kw = s["cn_kw"].strip()
        code, disp, src, warn = resolve_snomed(
            s["code_override"].strip(), s["filter"].strip(), s["want"].strip(),
            sample_map, v1_map)
        cn, cn_n = resolve_cn(kw, s["note"])
        rows.append({
            "cn_code": cn[0] if cn else "", "cn_name": cn[1] if cn else kw,
            "snomed_code": code or "", "snomed_display": disp or "",
            "source": src, "note": s["note"].strip(),
            "cn_candidates": cn_n if cn else 0,
        })
        line = (f"[{i:02d}] {kw}: CN={cn[0] if cn else '?·未命中'} | "
                f"SCT={code or '?·未命中'} {disp or ''} ({src})")
        if not cn:
            warns.append("中文未命中: " + kw)
        if warn:
            warns.append(f"[{i:02d}] {kw}: {warn}")
        print(line)

    os.makedirs(BASE, exist_ok=True)
    cols = ["cn_code", "cn_name", "snomed_code", "snomed_display", "source", "note", "cn_candidates"]
    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(f"写入 {len(rows)} 条 -> {OUT}\n")
        for line in warns:
            fh.write("WARN: " + line + "\n")
    print(f"\n写入 {len(rows)} 条 -> {OUT}")


if __name__ == "__main__":
    main()
