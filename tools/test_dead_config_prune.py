# -*- coding: utf-8 -*-
"""死配置收敛清理自测（`prune_stale_bp_config`）：bp / bp_target / **sql2fhir.layout** 三类键。

为什么需要：清理判据依赖「组件清单 = 事实源」，一旦判据写错就会**误删活配置**（比留垃圾危险得多）；
且 2026-09-18 新纳入的 `sql2fhir.layout[<聚合BP名>]` 是**带下标的实例键**（枚举方式与 bp 不同），
必须有"造死键 → dry_run 只报不删 → apply 只删死键 → 活键与全局兜底键完好 → 幂等"的自证。

用法（容器内，真 IRIS）：
  docker exec -i dataflow-backend sh -c 'cat > /tmp/t.py' < tools/test_dead_config_prune.py
  docker exec dataflow-backend python /tmp/t.py
（datakit：`bash tools/datakit/run.sh test_dead_config_prune.py`）
"""
import json
import sys

sys.path.insert(0, "/app")
import iris  # noqa: E402

from backend.routes.pipelines import _config_subs, prune_stale_bp_config  # noqa: E402
from backend.services import iris_connector as ic  # noqa: E402

FAKE = {"bp": "__dead_test_bp__", "bp_target": "__dead_test_src__",
        "layout": "__dead_test_layout__"}
FAIL = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


def main() -> int:
    conn = ic.get_connection()
    native = iris.createIRIS(conn)
    try:
        raw = ic.class_method_value("demo.PipelineQuery", "GetItems") or "[]"
        comps = {str((i or {}).get("name") or "") for i in (json.loads(raw) or [])}
        # 「活键」= **组件在位**的键（有读者）。仅靠"键已存在"会把无组件支撑的陈旧键误当活键，
        # 从而把「正确检出」误判成「误报」（本自测首版就是这么写错的）。
        live_bp = {k for k in _config_subs(native, "bp") if k in comps}
        bt = {k: str(native.get("^demo.Config", "bp_target", k) or "")
              for k in _config_subs(native, "bp_target")}
        live_bt = {k for k, v in bt.items() if k in comps and v in comps}
        live_ly = {k for k in _config_subs(native, "sql2fhir", "layout") if k in comps}
        global_layout = native.get("^demo.Config", "sql2fhir", "layout")
        check("环境有活配置（判据事实源可用）", bool(comps and live_bp and live_bt),
              "comps=%d bp=%d bp_target=%d layout=%d" % (len(comps), len(live_bp), len(live_bt),
                                                         len(live_ly)))

        # 造 3 条假死键（指向不存在的组件）
        native.set("{}", "^demo.Config", "bp", FAKE["bp"])
        native.set("__no_such_component__", "^demo.Config", "bp_target", FAKE["bp_target"])
        native.set("{}", "^demo.Config", "sql2fhir", "layout", FAKE["layout"])

        # dry_run：只报不删（⚠ 沙箱化：only_prefix 限定只处理自造键，避免顺带清掉环境里的真实死键）
        dry = prune_stale_bp_config(set(), set(), dry_run=True,
                                    only_prefix="__dead_test_")["removed"]
        check("dry_run 报出 bp 死键", FAKE["bp"] in (dry.get("bp") or []), json.dumps(dry)[:200])
        check("dry_run 报出 bp_target 死键",
              FAKE["bp_target"] in [x.get("key") for x in (dry.get("bp_target") or [])])
        check("dry_run 报出 layout 死键", FAKE["layout"] in (dry.get("layout") or []))
        still = (FAKE["bp"] in _config_subs(native, "bp")
                 and FAKE["layout"] in _config_subs(native, "sql2fhir", "layout"))
        check("dry_run 不删任何键", still)
        check("dry_run 不报活键（组件仍在位）", not (set(dry.get("bp") or []) & live_bp)
              and not (set(dry.get("layout") or []) & live_ly)
              and not ({x.get("key") for x in (dry.get("bp_target") or [])} & live_bt))

        # apply：只删死键（同样限定前缀 → **不改动环境里其它真实死键**，2026-09-21 沙箱化）
        res = prune_stale_bp_config(set(), set(), dry_run=False,
                                    only_prefix="__dead_test_")["removed"]
        check("apply 删掉 bp 死键", FAKE["bp"] not in _config_subs(native, "bp"))
        check("apply 删掉 bp_target 死键", FAKE["bp_target"] not in _config_subs(native, "bp_target"))
        check("apply 删掉 layout 死键", FAKE["layout"] not in _config_subs(native, "sql2fhir", "layout"))
        check("活键完好", live_bp <= set(_config_subs(native, "bp"))
              and live_bt <= set(_config_subs(native, "bp_target"))
              and live_ly <= set(_config_subs(native, "sql2fhir", "layout")))
        check("全局兜底键 ^demo.Config(\"sql2fhir\",\"layout\") 未被当成实例键删",
              native.get("^demo.Config", "sql2fhir", "layout") == global_layout)

        # 幂等
        again = prune_stale_bp_config(set(), set(), dry_run=False,
                                      only_prefix="__dead_test_")["removed"]
        check("幂等：再跑无死键", not any(again.get(k) for k in ("bp", "bp_target", "layout")),
              json.dumps(again)[:200])
        print("  apply/again:", json.dumps({"apply": res, "again": again}, ensure_ascii=False)[:300])
    finally:
        for node, key in (("bp", FAKE["bp"]), ("bp_target", FAKE["bp_target"])):
            try:
                native.kill("^demo.Config", node, key)
            except Exception:  # noqa: BLE001 - 兜底清理
                pass
        try:
            native.kill("^demo.Config", "sql2fhir", "layout", FAKE["layout"])
        except Exception:  # noqa: BLE001 - 兜底清理
            pass
        ic.reset_connections()

    print("\n== RESULT:", "ALL-PASS" if not FAIL else "%d FAILED: %s" % (len(FAIL), FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
