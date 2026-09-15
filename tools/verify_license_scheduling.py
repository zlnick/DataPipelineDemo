# -*- coding: utf-8 -*-
"""验证「许可调度」：超许可上限的分组**生成但停用**（不再 500 失败）+ 一键切换自动让路。

用法：
    python3 tools/verify_license_scheduling.py           # 全流程（生成 + 断言 + 切换）
    python3 tools/verify_license_scheduling.py --check    # 只核对当前环境状态，不生成

背景：IRIS 社区版 KeyLicenseUnits=8，每个 Ens 业务主机（BS/BP/BO）常驻占 1 个，后端连接再占
1 → 同时最多 7 个业务主机。过去「一次提交多组超容量 → 直接 500 失败」，用户无法演示第二条管道。
现在改为**许可调度**：按提交顺序装箱，放不下的分组组件照旧生成、初始 Enabled=0（停用），
用户可在「数据管道」卡片一键切换（启用时自动为它让路）。

前提：已登记 1 个 SQL 源（多表资产）+ 1 个 FHIR 目标 + 1 个 SOAP 目标 + 两条已确认映射
      （一条 → FHIR、一条 → SOAP），且两组业务主机之和 **超过许可上限**（否则无需调度）。

断言：
  1) POST /pipelines/generate（pipelines=[FHIR 组, SOAP 组]）返回 code=0（不再 500）；
  2) license_budget：scheduled 非空、suspended 非空、over_capacity=true；
  3) Production 中 suspended 组的组件 Enabled=0（生成即停用，不占许可单元）；
  4) POST /pipelines/instances/<被停用管道>/enable → 自动让路（disabled_others 非空）并变 active。
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:5001/api"
FAILURES: list[str] = []


def call(method, path, body=None, timeout=1800):
    """调用后端 API（HTTP 错误时打印响应体，便于定位）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"[HTTP {exc.code}] {method} {path}\n{detail[:1500]}")


def data_of(resp):
    """取统一返回体 {code,data,message} 的 data（失败直接报错，不静默）。"""
    if resp.get("code") != 0:
        raise SystemExit(f"接口失败: {resp.get('message')}")
    return resp.get("data") or {}


def check(ok, label, detail=""):
    """记录并打印一条断言结果（失败累计，最后统一退出码）。"""
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(label)
    return ok


def prod_items():
    """Production 组件清单 → ({name: item}, units)。"""
    d = data_of(call("GET", "/pipelines/items"))
    return {str(i.get("name")): i for i in d.get("items") or []}, int(d.get("units") or 0)


def build_groups():
    """按「目标分组 + 数据源覆盖度」组装多管道组（与前端 / tools/gen_multi_pipeline.py 一致）。"""
    ds_list = data_of(call("GET", "/datasources")).get("items", [])
    tgs = data_of(call("GET", "/targets")).get("items", [])
    maps = data_of(call("GET", "/mappings")).get("items", [])
    asset_cands: dict[str, list[dict]] = {}
    for d in ds_list:
        assets = data_of(call("GET", f"/datasources/{d['id']}/assets")).get("items", [])
        for a in assets:
            asset_cands.setdefault(a.get("name"), []).append(
                {"dsId": d["id"], "dsType": d.get("type")})
    tgt_by_table: dict[str, dict] = {}
    for t in tgs:
        tgt_by_table.setdefault(t.get("table"), t)
    by_target: dict[str, list[dict]] = {}
    for m in maps:
        tg = tgt_by_table.get(m.get("target_table")) or {}
        by_target.setdefault(str(tg.get("target_id") or tg.get("id") or m.get("target_table")),
                             []).append(m)
    groups = []
    for ms in by_target.values():
        score: dict[str, int] = {}
        for m in ms:
            for c in asset_cands.get(m.get("source"), []):
                score[c["dsId"]] = score.get(c["dsId"], 0) + 1
        best = sorted(score.items(), key=lambda kv: -kv[1])[0] if score else ("", 0)
        ds_id = best[0]
        ds_type = next((c["dsType"] for c in asset_cands.get(ms[0].get("source"), [])
                        if c["dsId"] == ds_id), "")
        tg = tgt_by_table.get(ms[0].get("target_table")) or {}
        groups.append({
            "source_type": ds_type or "SQL", "source_id": ds_id or None,
            "target_type": ms[0].get("target_type")
            or ("SOAP" if tg.get("type") == "SOAP" else "DB"),
            "target_id": tg.get("target_id") or tg.get("id"),
            "mappings": ms,
        })
    return groups



def main():
    """主流程：生成（超容量多组）→ 断言调度结果 → 一键切换验证。"""
    only_check = "--check" in sys.argv
    items, units = prod_items()
    max_hosts = max(0, units - 1)
    print(f"许可单元 units={units}，业务主机上限 max_hosts={max_hosts}", flush=True)
    lb: dict = {}

    if not only_check:
        groups = build_groups()
        print("组装出的管道组:", flush=True)
        for g in groups:
            print("  源", g["source_id"], g["source_type"], "→ 目标", g["target_id"],
                  g["target_type"], "| 映射", [m.get("id") for m in g["mappings"]], flush=True)
        if len(groups) < 2:
            raise SystemExit(f"当前只有 {len(groups)} 组映射，无法验证『分组调度』："
                             "请先准备两条映射（一条 → FHIR、一条 → SOAP）")
        t0 = time.time()
        resp = call("POST", "/pipelines/generate", {"pipelines": groups})
        d = resp.get("data") or {}
        lb = d.get("license_budget") or {}
        print(f"generate code={resp.get('code')} 耗时={time.time() - t0:.1f}s "
              f"message={resp.get('message')}", flush=True)
        check(resp.get("code") == 0, "超容量多管道生成成功（不再 500 硬失败）",
              resp.get("message") or "")
        print("license_budget:", json.dumps(lb, ensure_ascii=False)[:600], flush=True)
        check(bool(lb.get("scheduled")), "有分组被启用 (scheduled)", str(lb.get("scheduled")))
        check(bool(lb.get("suspended")), "有分组因超上限被停用 (suspended)", str(lb.get("suspended")))
        check(bool(lb.get("over_capacity")), "over_capacity=true（调度而非失败）")

    # ---- 事实核对（以 Production 组件 Enabled 为准） ----
    items, units = prod_items()
    insts = data_of(call("GET", "/pipelines/instances")).get("items", [])
    print("管道实体:", [(i["id"], i["status"], i["category"]) for i in insts], flush=True)
    suspended_cats = {str(c) for c in (lb.get("suspended") or [])}
    if not only_check and suspended_cats:
        for i in insts:
            comps = [n for n in (i.get("component_names") or [])]
            en = {n: int((items.get(n) or {}).get("enabled") or 0) for n in comps}
            if str(i.get("category")) in suspended_cats:
                check(comps and all(v == 0 for v in en.values()),
                      f"停用分组 {i['category']} 的组件『生成即停用』(Enabled=0)", str(en))
            else:
                check(any(v == 1 for v in en.values()),
                      f"启用分组 {i['category']} 的组件已启用", str(en))

    # ---- 一键切换：启用被停用的管道 → 自动让路其它管道 ----
    target = next((i for i in insts
                   if i.get("status") == "suspended"
                   and (i.get("component_names") or [])), None)
    if not target:
        print("（无 suspended 管道，跳过一键切换验证）", flush=True)
    else:
        r = data_of(call("POST", f"/pipelines/instances/{target['id']}/enable"))
        leaked = r.get("disabled_others") or []
        print(f"启用 {target['id']} → disabled_others={leaked}", flush=True)
        check(bool(r.get("enabled")), "管道启用成功", str(target["id"]))
        check(bool(leaked), "许可不足时自动让路其它管道（一键切换语义）", str(leaked))
        insts2 = data_of(call("GET", "/pipelines/instances")).get("items", [])
        st2 = next((i for i in insts2 if i.get("id") == target["id"]), {})
        check(st2.get("status") == "active", "切换后该管道为 active", str(st2.get("status")))

    print("\n=== 结果 ===", flush=True)
    if FAILURES:
        print("失败项:", FAILURES, flush=True)
        sys.exit(1)
    print("全部通过 ✅", flush=True)


if __name__ == "__main__":
    main()
