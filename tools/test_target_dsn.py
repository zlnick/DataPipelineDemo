# -*- coding: utf-8 -*-
"""验证「DB 目标的 DSN 按 jdbc_url 命名空间归一」链路（2026-09-16 缺陷修复）。

演示默认口径：**SQL 源 = USER 命名空间、SQL 目标 = CLINIC 命名空间**。
缺陷：DB 目标 SQLOp 曾硬编码 `localTarget`(=USER) → 目标登记的 `…/CLINIC` 被静默忽略，
转换结果写进了 USER 库；且目标侧没有"按命名空间注册 DSN"的步骤（源侧有）。

本脚本以只读为主，仅第 C / E 组会创建探针 DSN 并**立即删除**。在 backend 容器内直接跑：
  source tools/guard/docker_guard.sh
  docker exec -i dataflow-backend sh -c 'cat > /tmp/test_target_dsn.py' < tools/test_target_dsn.py
  docker exec dataflow-backend python /tmp/test_target_dsn.py
"""
from typing import Any

from backend.routes.pipelines import _target_dsn
from backend.services import iris_connector as ic
from backend.services import jdbc_dsn, repository

_PASS = 0
_FAIL = 0


def check(name: str, got: Any, want: Any) -> None:
    """断言并打印一行结果（不抛异常，跑完全部检查）。"""
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS {name}: {got!r}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}: got={got!r} want={want!r}")


_CLINIC = "jdbc:IRIS://iris:1972/CLINIC"
_USER = "jdbc:IRIS://iris:1972/USER"


def main() -> int:
    """跑 A~D 四组检查，返回失败数。"""
    print("== A. repository.target_runtime：目标 DSN 按命名空间归一（纯推导，不写库）==")
    check("CLINIC 目标（未显式给 dsn）",
          repository.target_runtime({"id": "T1", "type": "DB",
                                     "connection": {"jdbc_url": _CLINIC}})
          ["connection"]["dsn"], "CLINIC")
    check("USER 目标（未显式给 dsn）",
          repository.target_runtime({"id": "T2", "type": "DB",
                                     "connection": {"jdbc_url": _USER}})
          ["connection"]["dsn"], "USER")
    check("显式 dsn 优先",
          repository.target_runtime({"id": "T3", "type": "DB",
                                     "connection": {"jdbc_url": _CLINIC, "dsn": "MYDSN"}})
          ["connection"]["dsn"], "MYDSN")
    check("无 jdbc_url → 历史兜底",
          repository.target_runtime({"id": "T4", "type": "DB", "connection": {}})
          ["connection"]["dsn"], "localTarget")
    check("非 IRIS url → 历史兜底",
          repository.target_runtime({"id": "T5", "type": "DB",
                                     "connection": {"jdbc_url": "jdbc:postgresql://h:5432/db"}})
          ["connection"]["dsn"], "localTarget")
    check("jdbc_url 只在 config 里也能推导",
          repository.target_runtime({"id": "T6", "type": "DB", "connection": {},
                                     "config": {"jdbc_url": _CLINIC}})
          ["connection"]["dsn"], "CLINIC")

    print("== B. pipelines._target_dsn：生成 SQLOp 时的 DSN 取值口径 ==")
    check("契约 dsn", _target_dsn({"dsn": "CLINIC"}), "CLINIC")
    check("只有 jdbc_url", _target_dsn({"jdbc_url": _CLINIC}), "CLINIC")
    check("dsn 误填成 URL", _target_dsn({"dsn": _CLINIC}), "CLINIC")
    check("空配置 → localTarget", _target_dsn({}), "localTarget")

    print("== C. jdbc_dsn 目标侧注册（真实 IRIS，探针 DSN 用完即删）==")
    check("dsn_name_for_target(CLINIC)",
          jdbc_dsn.dsn_name_for_target({"id": "T7", "type": "DB",
                                        "connection": {"jdbc_url": _CLINIC}}), "CLINIC")
    check("dsn_name_for_target(无 url)", jdbc_dsn.dsn_name_for_target({"id": "T8"}), "localTarget")
    check("CLINIC DSN 幂等（exists/created）",
          jdbc_dsn.ensure_jdbc_dsn("CLINIC", _CLINIC) in ("exists", "created"), True)
    probe = "DS_PROBE_TARGET"
    probe_url = "jdbc:IRIS://iris:1972/PROBENS"
    print(f"  探针创建 {probe} -> {probe_url}: {jdbc_dsn.ensure_jdbc_dsn(probe, probe_url)}")
    rows = ic.query("SELECT Connection_Name, DSN FROM %Library.sys_SQLConnection "
                    "WHERE Connection_Name=?", [probe])
    check("探针 DSN 已建", [tuple(r) for r in rows],
          [(probe, probe_url)])
    try:
        ic.execute("DELETE FROM %Library.sys_SQLConnection WHERE Connection_Name=?", [probe])
        left = ic.query("SELECT Connection_Name FROM %Library.sys_SQLConnection "
                        "WHERE Connection_Name=?", [probe])
        check("探针 DSN 已清", list(left), [])
    except Exception as exc:  # noqa: BLE001 - 清理失败要显式可见
        print(f"  FAIL 探针清理失败: {str(exc)[:160]}")

    print("== E. jdbc_dsn 源侧命名：命名空间即 DSN 名（+ 同名冲突守卫）==")
    check("源 URL …/CLINIC → CLINIC",
          jdbc_dsn.dsn_name_for({"id": "DSX", "config": {"jdbc_url": _CLINIC}}), "CLINIC")
    check("源 URL …/USER → USER（演示默认源库）",
          jdbc_dsn.dsn_name_for({"id": "DSX", "config": {"jdbc_url": _USER}}), "USER")
    check("非 IRIS url → DS_<数据源ID> 兜底",
          jdbc_dsn.dsn_name_for({"id": "DSX",
                                 "config": {"jdbc_url": "jdbc:postgresql://h:5432/db"}}),
          "DS_DSX")
    check("find_dsn_url_by_name(CLINIC) 指向的命名空间一致",
          jdbc_dsn.namespace_of(jdbc_dsn.find_dsn_url_by_name("CLINIC")).lower(), "clinic")
    # 同名冲突守卫：建一个名叫 PROBENS 却连别的命名空间的 DSN → 源侧应放弃它，不静默连错库
    probe2, probe2_url = "PROBENS", "jdbc:IRIS://iris:1972/OTHER"
    try:
        print(f"  探针创建 {probe2} -> {probe2_url}: {jdbc_dsn.ensure_jdbc_dsn(probe2, probe2_url)}")
        check("同名 DSN 命名空间不一致 → 放弃该名（退 DS_<id>）",
              jdbc_dsn.dsn_name_for({"id": "DSY",
                                     "config": {"jdbc_url": "jdbc:IRIS://iris:1972/PROBENS"}}),
              "DS_DSY")
    finally:
        try:
            ic.execute("DELETE FROM %Library.sys_SQLConnection WHERE Connection_Name=?", [probe2])
            check("冲突探针 DSN 已清",
                  list(ic.query("SELECT Connection_Name FROM %Library.sys_SQLConnection "
                                "WHERE Connection_Name=?", [probe2])), [])
        except Exception as exc:  # noqa: BLE001 - 清理失败要显式可见
            print(f"  FAIL 冲突探针清理失败: {str(exc)[:160]}")
    # 登记侧不变量：SQL 数据源的 DSN 名 == 其 jdbc_url 的命名空间（本轮修的就是这条）
    for ds in repository.list_datasources():
        if (ds.get("type") or "").upper() != "SQL":
            continue
        rt = repository.datasource_runtime(ds)
        conn = rt.get("connection") or {}
        check(f"登记不变量 {ds.get('id')}: dsn 名 == 命名空间",
              str(conn.get("dsn") or "").lower(),
              jdbc_dsn.namespace_of(conn.get("jdbc_url") or "").lower())

    print("== D. 现状（只读）：已注册 DSN 与 DB 目标 ==")
    print("  DSN:", [(r[0], r[1]) for r in ic.query(
        "SELECT Connection_Name, DSN FROM %Library.sys_SQLConnection")])
    for tg in repository.list_targets():
        if (tg.get("type") or "").upper() == "DB":
            rt = repository.target_runtime(tg)
            print(f"  DB 目标 {tg.get('id')}: dsn={rt['connection'].get('dsn')} "
                  f"jdbc_url={rt['connection'].get('jdbc_url')}")
    print(f"== 结果：PASS={_PASS} FAIL={_FAIL} ==")
    return _FAIL


if __name__ == "__main__":
    raise SystemExit(main())
