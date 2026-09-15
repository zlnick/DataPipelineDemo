# -*- coding: utf-8 -*-
"""验证「数据管道实体（受管理持久对象）」：

1. 重复生成同一 (源, 目标) → 只更新不新增（generation_count 递增，instances 数量不变）；
2. 组件 Category = 管道类别（含 shared），/pipelines/items 能按类别回显；
3. 按管道整条启停（enable/disable）→ 状态与许可占用随之变化；
4. 许可容量守卫：启用后超许可单元时**显式失败**（不静默、不把后端打成 500）。

用法：python3 tools/verify_pipeline_instances.py
前提：已有一条 SQL→FHIR 管道与一条 SQL→SOAP 管道（或先跑 tools/verify_license_budget.py）。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:5001/api"


def call(method, path, body=None, timeout=600):
    """调用后端 API；HTTP 错误返回 (code, 响应体) 而不抛中断（本脚本要验证失败路径）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"code": exc.code, "message": raw[:300]}


def instances():
    """当前管道实体清单。"""
    return (call("GET", "/pipelines/instances") or {}).get("data") or {}


def show_instances(label):
    """打印管道实体概览（id / 状态 / 生成次数 / 组件）。"""
    d = instances()
    print(f"\n--- {label} ---")
    print("管道数:", len(d.get("items") or []), "| active:", d.get("active_count"),
          "| 已启用组件:", d.get("enabled_count"), "/ 许可单元:", d.get("units"))
    for it in d.get("items") or []:
        print(f"  {it['id']:26s} {it.get('source_type')}→{it.get('target_type'):5s} "
              f"skill={it.get('design_skill'):22s} status={it.get('status'):11s} "
              f"gen={it.get('generation_count')} 组件={it.get('component_names')}")
    return d


def items_by_category():
    """组件清单按类别汇总（验证 Category 落地）。

    后端返回 categories = [{"category", "items": [组件名], "enabled_count"}]（列表）。
    """
    data = (call("GET", "/pipelines/items") or {}).get("data") or {}
    cats = data.get("categories") or []
    print("\n--- 组件类别分布（Category = 管道类别）---")
    for grp in cats:
        names = [n if isinstance(n, str) else n.get("name")
                 for n in (grp.get("items") or [])]
        print(f"  [{grp.get('category')}] 启用 {grp.get('enabled_count')}/{len(names)}: {names}")
    missing = [i.get("name") for i in data.get("items") or []
               if not (i.get("category") or "").strip()]
    print("  无类别组件（历史遗留，应为空）:", missing or "无")
    return data


REQUIRED_INSTANCE_FIELDS = ("id", "source_type", "source_id", "target_type", "target_id",
                            "design_skill", "category", "status", "component_names",
                            "shared_component_names", "generation_count", "updated_at")
REQUIRED_GROUP_FIELDS = ("category", "active", "items", "enabled_count", "component_count")


def check_ui_contract() -> bool:
    """前端 Pipelines.vue「数据管道」卡片/组件表所依赖的字段契约自检。

    字段缺失不会报错，只会让 UI 渲染空白 → 用断言式自检把问题暴露在后端侧。
    """
    d = instances()
    missing = {f for it in (d.get("items") or []) for f in REQUIRED_INSTANCE_FIELDS if f not in it}
    missing |= {f for g in (d.get("groups") or []) for f in REQUIRED_GROUP_FIELDS if f not in g}
    comp = (call("GET", "/pipelines/items") or {}).get("data") or {}
    missing |= {f for i in (comp.get("items") or [])
                for f in ("name", "className", "enabled", "category") if f not in i}
    print("\n--- 前端字段契约自检（Pipelines.vue 卡片 + 组件表）---")
    print("缺失字段:", missing or "无（所需字段齐备）")
    return not missing


if "--contract-only" in sys.argv:   # 快速自检（不起 Production、不生成）
    raise SystemExit(0 if check_ui_contract() else 1)


def run_lifecycle(inst_ids):
    """步骤 3/4：按管道整条启停 + 许可容量守卫（不重新生成，可独立运行）。"""
    print("\n--- 按管道启停（整条管道占用/释放许可单元）---")
    for pid in inst_ids:
        res = call("POST", f"/pipelines/instances/{pid}/disable")
        print(f"停用 {pid}:", res.get("code"), "|", str(res.get("message"))[:80])
    show_instances("全部停用后")
    for pid in inst_ids:
        res = call("POST", f"/pipelines/instances/{pid}/enable")
        print(f"启用 {pid}:", res.get("code"), "|", str(res.get("message"))[:150])
        d = res.get("data") or {}
        if res.get("code") != 0 and "许可不足" in str(res.get("message") or ""):
            print("  ✓ 许可容量守卫生效（让路后仍不够 → 显式失败而非耗尽许可把后端打挂）")
        elif d.get("disabled_others"):
            print("  ✓ 一键切换自动让路（为腾许可被停用的其它管道组件: "
                  f"{d['disabled_others']}）")
    show_instances("启用尝试后")
    print("\n最终组件清单（含类别）:")
    for it in (call("GET", "/pipelines/items") or {}).get("data", {}).get("items") or []:
        print("  ", "ON " if int(it.get("enabled") or 0) == 1 else "off",
              it.get("name"), "|", it.get("category") or "-")


def _mode() -> str:
    """运行模式：full（默认，含重复生成）/ items-only / lifecycle-only。

    注意：模式值不带 `--` 前缀（曾因 `MODE == "items-only"` 与 `--items-only` 比较不上，
    导致 items-only 静默退化成"只跑 lifecycle"，自检未生效）。
    """
    for flag, name in (("--items-only", "items-only"),
                       ("--lifecycle-only", "lifecycle-only")):
        if flag in sys.argv:
            return name
    return "full"


MODE = _mode()
print(f"[模式] {MODE}（命令行: {' '.join(sys.argv[1:]) or '无'}）")

before = show_instances("基线（生成前）")
ids = {it["id"]: it for it in before.get("items") or []}
if not ids:
    raise SystemExit("没有管道实体：请先跑 tools/verify_license_budget.py 生成两条管道")
check_ui_contract()

if MODE == "items-only":        # 只查类别分布 + 字段契约（快）
    items_by_category()
    raise SystemExit(0)

if MODE == "full":
    # —— 1/2. 重复生成验证：需要 mappings 里既有 FHIR 也有 SOAP 目标 ——
    maps = (call("GET", "/mappings") or {}).get("data", {}).get("items", [])
    fhir_maps = [m for m in maps if m.get("target_type") == "FHIR"]
    soap_maps = [m for m in maps if m.get("target_type") == "SOAP"]
    dss = (call("GET", "/datasources") or {}).get("data", {}).get("items", [])
    tgs = (call("GET", "/targets") or {}).get("data", {}).get("items", [])

    def pick(kind):
        """按类型取第一个目标的 ID（/targets 返回的是表级行，ID 在 target_id）。"""
        for it in tgs:
            if it.get("type") == kind and (it.get("id") or it.get("target_id")):
                return it.get("id") or it.get("target_id")
        raise SystemExit(f"找不到 type={kind} 的目标")

    sql_ds = next((d["id"] for d in dss if d.get("type") == "SQL"), None)
    if fhir_maps and sql_ds:
        r = call("POST", "/pipelines/generate", {"pipelines": [{
            "source_type": "SQL", "source_id": sql_ds,
            "target_type": "FHIR", "target_id": pick("FHIR"), "mappings": fhir_maps}]})
        print("\n重复生成 SQL→FHIR:", r.get("code"), "|", str(r.get("message"))[:90])

    after = show_instances("重复生成后")
    if len(after.get("items") or []) != len(before.get("items") or []):
        raise SystemExit("✗ 重复生成新增了管道实体（应当只更新）")
    gen_up = [it["id"] for it in after.get("items") or []
              if int(it.get("generation_count") or 0) > 1]
    print("✓ 管道实体数量不变，generation_count>1 的管道:",
          gen_up or "无（注意：本次可能未命中同身份）")
    items_by_category()

run_lifecycle(list(ids))
