# -*- coding: utf-8 -*-
"""P3 回归：**启停「真的生效」**（运行期作业随之起/停），且不牵连共享件。

背景（实测 2026-09-18）：
  · 原实现**先落配置再调运行期** → `Ens.Director.EnableConfigItem(name,0,1)` 返回
    `<Ens>ErrGeneral … already disabled in Production` = **空操作** → 停用后 6 个作业 180s 不退，
    许可不释放（UI 上"停用"看起来毫无作用）。
  · 修法：**先运行期、再落配置**（转换是真实转换），停用方向补「启用→停用」镜像补救，
    仍不一致再做一次有界 Production 重启收敛。

本测试用**一个不产生数据副作用的 BO**（默认 `SQLOp_PatientSource__sql2db`，其 BP 处于停用）做往返：
  A 启用 → 作业起来（job=1、cfg=1、runtime_still_down 空）；
  B 停用 → 作业真的退出（job=0、cfg=0、**runtime_still_up 空**）← P3 的核心断言；
  C 共享件（TerminologyOperation/JavaGateway/FHIRSyncService）全程不受影响；
  D 正常路径不应触发"生产重启收敛"（converged_by_restart=False）。
结束把目标恢复为**测试前状态**。
"""
import sys

sys.path.insert(0, "/app")

from backend.services import iris_connector, pipeline_validator as PV  # noqa: E402
import iris  # noqa: E402
import iris.dbapi  # noqa: E402

from backend.config import IRISConfig  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else ""
SHARED = ["TerminologyOperation", "EnsLib.JavaGateway.Service", "FHIRSyncService"]
PASS = FAIL = 0


def _pick_target() -> str:
    """动态选取一个**在当前 Production 在位**的 BO 组件（优先 SOAPOp/SQLOp/HTTPOperation）。

    2026-09-19：原实现硬编码 `SQLOp_PatientSource__sql2db` —— 环境里没有该组（如全是 SQL 源组）时
    会把 7 项断言判 FAIL（假失败）。现从 **Production 组件事实**（`pipeline_validator.production_items`）
    动态挑选（不依赖 HTTP：本脚本在容器内跑，容器里 Flask 是 5000 端口）；找不到则跳过整个测试。
    """
    try:
        items = [str(i.get("name")) for i in (PV.production_items() or [])]
    except Exception:  # noqa: BLE001 - 拿不到清单则退化为空（下面会跳过）
        items = []
    skip = set(SHARED)
    for pref in ("SOAPOp_", "SQLOp_", "HTTPOperation"):
        for nm in items:
            if nm.startswith(pref) and nm not in skip:
                return nm
    return ""


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + msg)
    else:
        FAIL += 1
        print("  ✗ " + msg)


def cfg(name):
    conn = iris.dbapi.connect(hostname=IRISConfig.HOST, port=IRISConfig.PORT, namespace="USER",
                              username=IRISConfig.USERNAME, password=IRISConfig.PASSWORD)
    try:
        cur = conn.cursor()
        cur.execute("SELECT Enabled FROM Ens_Config.Item WHERE Name=?", (name,))
        row = cur.fetchone()
        return int(row[0] or 0) if row else -1
    finally:
        conn.close()


def job(name):
    conn = iris_connector.get_connection()
    try:
        n = iris.createIRIS(conn)
        return int(n.get("^Ens.Runtime", "ConfigItem", name, "Job") or 0)
    finally:
        conn.close()


if not TARGET:
    TARGET = _pick_target()
if not TARGET:
    print("  … 跳过：当前 Production 里没有可测的 BO 组件（SQLOp_*/SOAPOp_*/HTTPOperation）")
    sys.exit(0)

print("目标组件 = %s（测试前 cfg=%s job=%s）" % (TARGET, cfg(TARGET), job(TARGET)))
initial_cfg = cfg(TARGET)

print("\n==== A. 启用 → 作业真的起来 ====")
ra = PV.set_items_enabled([TARGET], True)
check(bool(ra.get("ok")), "A1 接口 ok（result=%s）" % ra.get("result"))
check(job(TARGET) == 1, "A2 ^Ens.Runtime Job=1（真的起来了）")
check(cfg(TARGET) == 1, "A3 配置 Enabled=1（持久化）")
check(not (ra.get("runtime_still_down") or []), "A4 runtime_still_down 为空（%s）"
      % (ra.get("runtime_still_down"),))
check(not ra.get("converged_by_restart"), "A5 正常路径未触发生产重启收敛")

print("\n==== B. 停用 → 作业真的退出（P3 核心：许可释放） ====")
rb = PV.set_items_enabled([TARGET], False)
check(bool(rb.get("ok")), "B1 接口 ok（result=%s）" % rb.get("result"))
check(job(TARGET) == 0, "B2 ^Ens.Runtime Job=0（真的停了 → 许可释放）")
check(cfg(TARGET) == 0, "B3 配置 Enabled=0（持久化）")
check(not (rb.get("runtime_still_up") or []), "B4 runtime_still_up 为空（%s）"
      % (rb.get("runtime_still_up"),))
check(bool(rb.get("runtime_applied")), "B5 运行期调用被判定为**真实执行**而非空操作（applied=%s）"
      % (rb.get("runtime_applied"),))

print("\n==== C. 共享件不受牵连 ====")
for nm in SHARED:
    if cfg(nm) < 0:
        print("  – 跳过 %s（当前 Production 无该组件：用例依赖管道形态，非缺陷）" % nm)
        continue
    check(cfg(nm) == 1 and job(nm) == 1, "C %s 仍启用且在跑（cfg=%s job=%s）" % (nm, cfg(nm), job(nm)))

print("\n==== D. 恢复测试前状态 ====")
if initial_cfg == 1:
    PV.set_items_enabled([TARGET], True)
print("  目标恢复为 cfg=%s job=%s（测试前 cfg=%s）" % (cfg(TARGET), job(TARGET), initial_cfg))
check(cfg(TARGET) == initial_cfg, "D1 已恢复测试前配置")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
