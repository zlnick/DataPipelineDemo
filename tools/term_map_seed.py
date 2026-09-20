# -*- coding: utf-8 -*-
"""把术语服务器上**已有的转换素材**预生成为"术语转换映射"（Terminology_Mapping.CodeMap）。

为什么：平台生成数据转换时要求"术语服务器必须先有现成转换"（无则中止生成并要求补齐）。
本脚本把服务器上既有的两类素材固化成映射条目（只读既有资料 + 幂等 upsert，不做任何语义判定）：

  ① 中文诊断：`Terminology_SnomedUs.ChineseAlias`（US Core Condition 双语词表，52 条人工核对真值）
     → urn:cn-nhsa:icd10-gbt2016  →  http://snomed.info/sct            （method=manual）
  ② 中文药品：`Terminology_Drug.Code`（NRDL/CBIH 目录码 → 中文名）
     × `Terminology_Rxnorm.ChineseAlias`（中文名 → RXCUI，IN 层）**精确同名**匹配
     → urn:cn-nhsa:drug-nrdl|drug-cbih  →  RxNorm                      （method=derived）

未匹配到的编码**不写负缓存**（"无映射"属判定结论，由判定 Agent / 人工补录决定）。

用法（宿主直接跑；术语服务器端口 51774）：
    python3 tools/term_map_seed.py            # 灌数（幂等）
    python3 tools/term_map_seed.py --dry      # 只预览将要写入的条目
    python3 tools/term_map_seed.py --check    # 只读：打印服务器目录与覆盖率
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"
TERM_BASE = os.environ.get("TERMSRV_BASE_HOST", "http://localhost:52774/terminology")

ICD10_SYS = "urn:cn-nhsa:icd10-gbt2016"
SNOMED_SYS = "http://snomed.info/sct"
NRDL_SYS = "urn:cn-nhsa:drug-nrdl"
CBIH_SYS = "urn:cn-nhsa:drug-cbih"
RX_SYS = "http://www.nlm.nih.gov/research/umls/rxnorm"
DRUG_SYS = {"nrdl": NRDL_SYS, "cbih": CBIH_SYS}


def _auth_header() -> dict:
    """Basic Auth 头（术语服务器与 demo 同口径 superuser/SYS）。"""
    return {"Authorization": "Basic " + base64.b64encode(b"superuser:SYS").decode()}


def _get(path: str) -> dict:
    req = urllib.request.Request(TERM_BASE + path, headers=_auth_header())
    with urllib.request.urlopen(req, timeout=30) as rsp:
        return json.loads(rsp.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(TERM_BASE + path, data=data, method="POST",
                                 headers={**_auth_header(),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as rsp:
        return json.loads(rsp.read().decode("utf-8"))


def _connect():
    import iris.dbapi
    return iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")


def build_icd_entries(conn) -> tuple[list[dict], list[str]]:
    """① 国标 ICD-10 → SNOMED：来自双语词表（人工核对真值）。"""
    cur = conn.cursor()
    cur.execute("SELECT CnCode, Code, Display, ZhName FROM Terminology_SnomedUs.ChineseAlias")
    entries, skipped = [], []
    for cn_code, code, display, zh in cur.fetchall():
        cn_code = (cn_code or "").strip()
        code = (code or "").strip()
        if not cn_code or not code:
            skipped.append(str(zh))
            continue
        entries.append({
            "sourceSystem": ICD10_SYS, "sourceCode": cn_code, "sourceDisplay": zh or "",
            "targetSystem": SNOMED_SYS, "targetCode": code, "targetDisplay": display or "",
            "method": "manual", "status": "active",
            "evidence": "US Core Condition 中文双语词表（人工核对真值，源自 tools/uscore_zhmap.py）",
            "note": "预生成：来自术语服务器既有词表",
        })
    return entries, skipped




def build_drug_entries(conn) -> tuple[list[dict], dict]:
    """② 药品目录码 → RxNorm：目录码解析中文名 × RxNorm 中文别名（精确同名）。"""
    cur = conn.cursor()
    cur.execute("SELECT ZhName, Rxcui, EnName, Tty FROM Terminology_Rxnorm.ChineseAlias")
    by_name = {}
    for zh, rxcui, en, tty in cur.fetchall():
        zh = (zh or "").strip()
        if zh and (rxcui or "").strip():
            by_name.setdefault(zh, (str(rxcui).strip(), en or "", tty or ""))

    cur.execute("SELECT Code, CodeSystemId, Display FROM Terminology_Drug.Code")
    entries, stat = [], {"total": 0, "matched": 0, "by_system": {}}
    for code, cs, display in cur.fetchall():
        code = (code or "").strip()
        cs = (cs or "").strip().lower()
        name = (display or "").strip()
        if not code or cs not in DRUG_SYS:
            continue
        stat["total"] += 1
        hit = by_name.get(name)
        if not hit:
            continue
        rxcui, en, tty = hit
        entries.append({
            "sourceSystem": DRUG_SYS[cs], "sourceCode": code, "sourceDisplay": name,
            "targetSystem": RX_SYS, "targetCode": rxcui, "targetDisplay": en,
            "method": "derived", "status": "active", "confidence": 0.9,
            "evidence": f"术语服务器推导：{cs.upper()} 目录名『{name}』精确命中 RxNorm 中文别名（{tty or 'IN'}）",
            "note": "预生成：目录码 → 中文名 → RxNorm 中文别名（未做语义判定）",
        })
        stat["matched"] += 1
        stat["by_system"][DRUG_SYS[cs]] = stat["by_system"].get(DRUG_SYS[cs], 0) + 1
    return entries, stat


def chunked(items: list[dict], size: int = 200):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main() -> int:
    ap = argparse.ArgumentParser(description="预生成术语转换映射到术语服务器")
    ap.add_argument("--dry", action="store_true", help="只预览，不写入")
    ap.add_argument("--check", action="store_true", help="只读取服务器目录与覆盖率")
    args = ap.parse_args()

    try:
        print("== 服务器目录（/mapping/systems）:")
        print(json.dumps(_get("/mapping/systems"), ensure_ascii=False, indent=2)[:1200])
    except urllib.error.URLError as exc:
        print(f"!! 无法访问术语服务器 {TERM_BASE}: {exc}", file=sys.stderr)
        return 2

    if args.check:
        for sys_uri, tgt in ((ICD10_SYS, SNOMED_SYS), (NRDL_SYS, RX_SYS)):
            q = (f"/mapping/coverage?sourceSystem={urllib.parse.quote(sys_uri)}"
                 f"&targetSystem={urllib.parse.quote(tgt)}")
            try:
                print(f"== 覆盖率 {sys_uri} → {tgt}: {json.dumps(_get(q), ensure_ascii=False)}")
            except urllib.error.URLError as exc:
                print(f"!! coverage: {exc}")
        return 0

    conn = _connect()
    try:
        icd_entries, skipped = build_icd_entries(conn)
        drug_entries, drug_stat = build_drug_entries(conn)
    finally:
        conn.close()

    print(f"== ① ICD-10→SNOMED 预生成条目 {len(icd_entries)} 条"
          f"（跳过无国标码 {len(skipped)} 条: {skipped[:5]}）")
    print(f"== ② 药品→RxNorm 推导条目 {len(drug_entries)} 条"
          f"（目录码 {drug_stat['total']} → 命中 {drug_stat['matched']}，按体系 {drug_stat['by_system']}）")

    if args.dry:
        for e in (icd_entries[:2] + drug_entries[:4]):
            print("   ", json.dumps(e, ensure_ascii=False))
        print("DRY-RUN（未写入）")
        return 0

    total = {"inserted": 0, "updated": 0, "failed": 0}
    for part in chunked(icd_entries + drug_entries):
        res = _post("/mapping/entries", {"items": part})
        total["inserted"] += int(res.get("inserted") or 0)
        total["updated"] += int(res.get("updated") or 0)
        total["failed"] += len(res.get("failed") or [])
        if res.get("failed"):
            print("!! 失败项:", json.dumps(res["failed"][:3], ensure_ascii=False))
    print(f"== 写入完成：inserted={total['inserted']} updated={total['updated']} failed={total['failed']}")
    print("== 写入后目录:", json.dumps(_get("/mapping/systems"), ensure_ascii=False))
    return 0 if total["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

