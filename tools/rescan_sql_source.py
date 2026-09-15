# -*- coding: utf-8 -*-
"""重置 SQL 源 BS 的扫描凭证与错误行记录，触发全量重扫（后端容器内运行）。

背景踩坑 1：`EnsLib.SQL.InboundAdapter.ClearAllAppData("<Item>")` 在本版本 IRIS 报
`<COMMAND> *Function must return a value at ClearAllAppData+2^Ens.Adapter.1`；
`Ens.Adapter.ClearStaticAppData` / `ClearRuntimeAppData` 用 **classMethodValue** 调用同样报
`*Function must return a value`——根因是这些方法**无返回值**，必须用 **classMethodVoid**
调用（`iris_connector.class_method_void`，实测有效，见 tools/check_ens_clear_api.py）。

因此本脚本两步走：
1. 官方 API（void 语义）：`Ens.Adapter.ClearStaticAppData` / `ClearRuntimeAppData`
   / `EnsLib.SQL.InboundAdapter.InitializeLastKeyValue(item, "")`；
2. 若 API 仍失败，退回实测可用的直接清 global（凭证/last key 位置）：
   `kill ^Ens.AppData(item,"adapter.sqlrow")`、`(item,"adapter.sqlparam")`、
   `kill ^IRIS.Temp.Adapter.sqlrow(item)`。

用法：
    python /tmp/rescan_sql_source.py                  # 停 Production → 重置全部 SQL 源 BS → 启 + 核验
    python /tmp/rescan_sql_source.py <Item1> [Item2]  # 只重置指定 Item
    python /tmp/rescan_sql_source.py --no-cycle       # 只清凭证（Production 已被外部停掉时用）
    python /tmp/rescan_sql_source.py --timeout=90     # 调整"重扫是否发生"的等待上限（默认 60s）

⚠ 必须**停 Production** 才能清干净：适配器的 last key 有内存副本，运行期清 global 后它仍按
旧 key 轮询 → 表现为「重扫了但一条消息都没有」（静默无效，极易误判为源/管道故障）。
本脚本默认自带「停 → 清 → 启」并**核验重扫是否真的发生**（last key 非空或消息数增长），
未观察到则显式告警并要求用 diag_sql_source_runtime.py 细查。

说明：SQL 源 BS 名可能按数据管道类别/去重变化（`SQLService_Patient`、
`SQLService_Patient__sql2fhir_patient_tx`、`SQLService_Patient_2` …），故名默认自动枚举，
不写死单个名字。
"""
import sys

from backend.services import iris_connector as ic

DEFAULT_ITEM = "SQLService_Patient"


def detect_items() -> list:
    """枚举所有 SQL 源 BS 名。

    优先 `^Ens.AppData` 一级下标（含历史/按类别改名后的名字，且 Production 删掉后仍可枚举），
    回退 `Ens_Config.Item` 配置表，最后回退旧版默认名。

    返回:
        去重排序后的 Item 名列表。
    """
    names: set = set()
    try:
        import iris

        conn = ic.get_connection()
        try:
            native = iris.createIRIS(conn)
            sub = native.nextSubscript(False, "^Ens.AppData", "")
            while sub:
                if str(sub).startswith("SQLService"):
                    names.add(str(sub))
                sub = native.nextSubscript(False, "^Ens.AppData", sub)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 枚举失败则退回配置表
        print(f"[warn] 枚举 ^Ens.AppData 失败（回退配置表）: {exc}")
    if not names:
        try:
            for r in ic.query("SELECT Name FROM Ens_Config.Item "
                              "WHERE ClassName='EnsLib.SQL.Service.GenericService'"):
                if r[0]:
                    names.add(str(r[0]))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 读取 Ens_Config.Item 失败: {exc}")
    if not names:
        names.add(DEFAULT_ITEM)
    return sorted(names)


def show(item: str, tag: str) -> None:
    """打印指定 Item 当前的 last key。"""
    try:
        last = ic.class_method_value("EnsLib.SQL.InboundAdapter", "GetPersistentValue",
                                     item, "%LastKey")
    except Exception as exc:  # noqa: BLE001
        last = f"<读取失败: {exc}>"
    print(f"  {tag} last key = {last!r}")


def kill_appdata(item: str) -> None:
    """直接清 global（官方 API 失效时的实证可用兜底）。"""
    import iris
    conn = ic.get_connection()
    try:
        native = iris.createIRIS(conn)
        for g, subs in (("^Ens.AppData", (item, "adapter.sqlrow")),
                        ("^Ens.AppData", (item, "adapter.sqlparam")),
                        ("^IRIS.Temp.Adapter.sqlrow", (item,))):
            native.kill(g, *subs)
    finally:
        conn.close()


def reset(item: str) -> None:
    """重置单个 SQL 源 BS 的凭证/错误行与 last key。"""
    print(f"[{item}]")
    show(item, "before:")
    api_ok = True
    for cls, label, args in (
            ("Ens.Adapter", "ClearStaticAppData", (item,)),
            ("Ens.Adapter", "ClearRuntimeAppData", (item,)),
            ("EnsLib.SQL.InboundAdapter", "InitializeLastKeyValue", (item, ""))):
        try:
            ic.class_method_void(cls, label, *args)
            print(f"  {label}: ok（void 调用）")
        except Exception as exc:  # noqa: BLE001 - 单项失败退回 global
            api_ok = False
            print(f"  {label} 失败: {str(exc)[:160]}")
    if not api_ok:
        try:
            kill_appdata(item)
            print("  已退回直接清 global（^Ens.AppData / ^IRIS.Temp.Adapter.sqlrow）")
        except Exception as exc:  # noqa: BLE001
            print(f"  清 global 也失败: {str(exc)[:160]}")
    show(item, "after :")


PROD = "demo.DataflowProduction"


def production_state() -> int:
    """读取 Production 运行状态码（1=Running）。

    返回:
        状态码整数；读取失败返回 -1（调用方据此外显式告警，不静默当作运行中）。
    """
    try:
        return int(ic.class_method_value("Ens.Director", "GetProductionStatus") or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读取 Production 状态失败: {str(exc)[:120]}")
        return -1


def message_count() -> int:
    """统计 Ens 消息总数（判断重扫是否真的产生了新消息）。

    返回:
        消息行数；查询失败返回 -1。
    """
    try:
        return int(ic.query("SELECT COUNT(*) FROM Ens.MessageHeader")[0][0])
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 统计消息数失败: {str(exc)[:120]}")
        return -1


def stop_production() -> None:
    """停止 Production。

    清凭证必须在停止状态下做：适配器 last key 有内存副本，运行期清 global 后
    适配器仍按旧 key 轮询 → 重扫静默无效。
    """
    if production_state() != 1:
        print("[production] 当前未在运行，无需停止")
        return
    res = ic.class_method_value("Ens.Director", "StopProduction", 60)
    print(f"[production] StopProduction -> {res}")


def start_production() -> bool:
    """启动 Production，失败时先 RecoverProduction 再重试一次。

    返回:
        启动成功（返回值 1）为 True；否则 False —— 调用方必须显式失败，
        不得让"启动没成功"被当成"重扫完成"。
    """
    res = ic.class_method_value("Ens.Director", "StartProduction", PROD)
    if res != 1:
        print(f"[warn] StartProduction -> {res}，尝试 RecoverProduction 后重试")
        try:
            ic.class_method_value("Ens.Director", "RecoverProduction")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] RecoverProduction 失败: {str(exc)[:120]}")
        res = ic.class_method_value("Ens.Director", "StartProduction", PROD)
    print(f"[production] StartProduction -> {res}")
    return res == 1


def wait_rescan(items: list, before_msgs: int, timeout: int) -> bool:
    """有界核验重扫是否真的发生（last key 非空 或 消息数增长）。

    参数:
        items: 待核对的 SQL 源 BS 名列表。
        before_msgs: 重置前的消息总数（-1 表示未知）。
        timeout: 最长等待秒数。

    返回:
        观察到重扫动作为 True；超时仍无动作为 False（调用方需显式告警）。
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        keys = {}
        for it in items:
            try:
                keys[it] = ic.class_method_value("EnsLib.SQL.InboundAdapter",
                                                 "GetPersistentValue", it, "%LastKey")
            except Exception:  # noqa: BLE001
                keys[it] = None
        now = message_count()
        if any(keys.values()) or (before_msgs >= 0 and now > before_msgs):
            print(f"[verify] 重扫已启动：last key={keys}，消息数 {before_msgs} -> {now}")
            return True
        time.sleep(5)
    print(f"[verify] {timeout}s 内未观察到重扫动作（last key 仍为空且消息数未增长）")
    print("[verify] 常见原因：① Production 未真正运行 ② 源 BS 未启用"
          "（许可调度停用，需在「数据管道」卡片一键切换）③ 源表无数据 ④ DSN/JDBC 网关异常"
          " —— 用 ./run.sh diag_sql_source_runtime.py 细查")
    return False


if __name__ == "__main__":
    argv = sys.argv[1:]
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        raise SystemExit(0)
    no_cycle = "--no-cycle" in argv
    timeout = 60
    item_args = []
    for a in argv:
        if a == "--no-cycle":
            continue
        if a.startswith("--timeout"):
            try:
                timeout = int(a.split("=", 1)[1])
            except (IndexError, ValueError):
                print(f"[error] --timeout 需要整数秒，如 --timeout=90（收到 {a!r}）")
                raise SystemExit(2)
            continue
        item_args.append(a)
    bad = [a for a in item_args if a.startswith("-")]
    if bad:   # 防止把 "--help" 之类当成 Item 名去重置（会写进 ^Ens.AppData 造成脏节点）
        print(f"[error] 未知参数: {', '.join(bad)}（Item 名不应以 '-' 开头）")
        print(__doc__)
        raise SystemExit(2)
    items = item_args or detect_items()
    print(f"待重置 SQL 源 BS（{len(items)} 个）: {', '.join(items)}")
    before = message_count()
    if no_cycle:
        print("[production] --no-cycle：跳过停/启（清凭证后需自行重启 Production 才生效）")
    else:
        stop_production()
    for it in items:
        reset(it)
    if no_cycle:
        print("RESCAN-RESET-DONE（未动 Production）")
        raise SystemExit(0)
    if not start_production():
        print("[error] Production 启动失败 —— 本次重扫不会生效"
              "（请检查许可单元/组件配置后重试，或用 Ens.Director.RecoverProduction 清理后重试）")
        raise SystemExit(1)
    ok = wait_rescan(items, before, timeout)
    print("RESCAN-RESET-DONE" if ok else "RESCAN-RESET-DONE（未能确认重扫发生，见上方提示）")
    raise SystemExit(0 if ok else 1)
