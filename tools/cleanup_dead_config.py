# -*- coding: utf-8 -*-
"""清理 `^demo.Config` 里**指向不存在组件**的陈旧登记（死配置）—— 生成后收敛的「手动/体检」版。

为什么需要它：生成期的收敛清理（`routes/pipelines.py::prune_stale_bp_config`）只在
**backend 载入新代码后**才会执行；若后端未重启（或想先体检一遍），用本脚本即可
「先看清单、再一键清」，避免 `bp[TransformProcess__sql2fhir_*]` 这类无读者登记越积越多。

判据（与生成期同一实现，单一事实源）：
  `bp[<BP名>]`        —— 键必须是**当前 Production 里的组件名**（否则没有读者）
  `bp_target[<源BS>]` —— 键必须是组件名，且值必须指向存在的组件（否则悬空）
  `sql2fhir.layout[<聚合BP名>]` —— 键必须是组件名（该 BP 运行期 `..%ConfigName` 读自己那份）
保护（绝不删）：`last_good` / `last_good_at`（Agent BP 源码存档）+ `--keep-bps/--keep-srcs`
+ 全局兜底键 `^demo.Config("sql2fhir","layout")`（无下标，历史兼容路径仍在读）。

安全：
  * **默认只查不改**；要真删必须显式 `--apply`（符合破坏性操作三段式）；
  * 组件清单为空时**拒绝执行**（判据失效 ⇒ 全部键都会被当成 stale，属"工具撒谎"风险）；
  * 删除前后都打印键清单与数量，便于复核。

用法（宿主 `bash tools/datakit/run.sh cleanup_dead_config.py [--apply]`）：
  python3 tools/cleanup_dead_config.py            # 只体检（列出将删项）
  python3 tools/cleanup_dead_config.py --apply    # 执行清理
  python3 tools/cleanup_dead_config.py --json     # 机器可读输出
"""
import argparse
import json
import sys

from backend.routes.pipelines import prune_stale_bp_config
from backend.services import iris_connector as ic


def _component_names() -> list[str]:
    """当前 Production 的组件名清单（判据事实源；读不到返回空 → 调用方应中止）。"""
    raw = ic.class_method_value("demo.PipelineQuery", "GetItems") or "[]"
    return sorted(str((i or {}).get("name") or "") for i in (json.loads(raw) or []))


def _config_keys(*path: str) -> list[str]:
    """枚举 `^demo.Config(<path...>)` 的下一级下标（复核用，只读）。"""
    import iris
    conn = ic.get_connection()
    try:
        native = iris.createIRIS(conn)
        prefix = '^demo.Config(%s)' % ",".join('"%s"' % p for p in path)
        out, s = [], native.nextSubscript(False, prefix, "")
        while s:
            out.append(str(s))
            s = native.nextSubscript(False, prefix, s)
        return out
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 关闭失败可忽略
            pass


def main() -> int:
    """入口：体检（默认）或执行清理（--apply）。"""
    ap = argparse.ArgumentParser(description="清理 ^demo.Config 里指向不存在组件的陈旧登记")
    ap.add_argument("--apply", action="store_true", help="真正执行删除（缺省只查不改）")
    ap.add_argument("--json", dest="as_json", action="store_true", help="JSON 输出")
    ap.add_argument("--keep-bps", default="", help="附加保护的 BP 名（逗号分隔）")
    ap.add_argument("--keep-srcs", default="", help="附加保护的源 BS 名（逗号分隔）")
    args = ap.parse_args()

    comps = _component_names()
    report: dict = {"components": comps, "dry_run": not args.apply}
    if not comps:
        report["error"] = ("当前 Production 组件清单为空 → 判据失效（全部键都会被误判为陈旧），"
                           "拒绝执行。请先确认 Production 已加载（/api/pipelines/status）。")
        print(json.dumps(report, ensure_ascii=False, indent=1) if args.as_json
              else "[cleanup] " + report["error"])
        return 2

    keep_bps = {x.strip() for x in args.keep_bps.split(",") if x.strip()}
    keep_srcs = {x.strip() for x in args.keep_srcs.split(",") if x.strip()}
    res = prune_stale_bp_config(keep_bps=keep_bps, keep_srcs=keep_srcs, dry_run=not args.apply)
    report.update(res)
    report["remaining"] = {"bp": _config_keys("bp"), "bp_target": _config_keys("bp_target"),
                           "sql2fhir.layout": _config_keys("sql2fhir", "layout")}

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    removed = res.get("removed") or {}
    print("[cleanup] Production 组件 %d 个（判据事实源）；模式=%s"
          % (len(comps), "删除" if args.apply else "只查不改（--apply 才删）"))
    print("[cleanup] 陈旧 bp 键      : %s" % (removed.get("bp") or "（无）"))
    print("[cleanup] 陈旧 bp_target 键: %s"
          % ([x.get("key") for x in (removed.get("bp_target") or [])] or "（无）"))
    print("[cleanup] 陈旧 layout 键  : %s" % (removed.get("layout") or "（无）"))
    print("[cleanup] 剩余键数        : bp=%d, bp_target=%d, layout=%d"
          % (len(report["remaining"]["bp"]), len(report["remaining"]["bp_target"]),
             len(report["remaining"]["sql2fhir.layout"])))
    if res.get("skipped"):
        print("[cleanup] 跳过原因       : %s" % res["skipped"])
    if res.get("error"):
        print("[cleanup] 执行错误       : %s" % res["error"])
    if not args.apply and any(removed.get(k) for k in ("bp", "bp_target", "layout")):
        print("[cleanup] 需要清理 → 请复核上面清单后重跑：python3 tools/cleanup_dead_config.py --apply")
    elif not any(removed.get(k) for k in ("bp", "bp_target", "layout")):
        print("[cleanup] ✅ 无死配置（bp / bp_target / sql2fhir.layout 全部指向存在的组件）")
    else:
        print("[cleanup] ✅ 已清理完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
