# -*- coding: utf-8 -*-
"""诊断 SQL 源 BS 运行期状态（为什么不出消息）：事件日志 + Item 状态 + 扫描凭证。

用法（容器内）：cd tools/datakit && ./run.sh diag_sql_source_runtime.py
"""
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("diag_sql_src")

PROD = "demo.DataflowProduction"


def main() -> None:
    from backend.services import iris_connector as ic

    log.info("== 事件日志（最近 20 条：ConfigName / Text）==")
    for row in ic.query("SELECT TOP 20 ConfigName, Type, Text, TimeLogged "
                        "FROM Ens_Util.Log ORDER BY ID DESC"):
        # query 返回 tuple：列顺序同 SELECT
        log.info("  %s [%s] %s | %s", str(row[3])[:19], row[0], row[1],
                 str(row[2])[:150])

    log.info("\n== Production / Item 状态（Ens.Director.GetProductionStatus + GetItems）==")
    try:
        st = ic.class_method_value("Ens.Director", "GetProductionStatus")
        log.info("  Production 状态码 = %s（1=Running）", st)
    except Exception as exc:  # noqa: BLE001
        log.info("  读取 Production 状态失败: %s", str(exc)[:120])
    try:
        import json as _json
        items = _json.loads(ic.class_method_value("demo.PipelineQuery", "GetItems", PROD) or "[]")
        for it in items:
            if "SQLService" not in str(it.get("name")):
                continue
            log.info("  %-42s enabled=%s category=%s", it.get("name"), it.get("enabled"),
                     it.get("category"))
    except Exception as exc:  # noqa: BLE001
        log.info("  读取组件清单失败: %s", str(exc)[:120])
    log.info("  （注：Ens.Director 无 GetItemStatus 方法——旧脚本曾在此报「METHOD DOES NOT EXIST」，"
             "现改为 Production 状态 + 组件 Enabled 事实）")

    log.info("\n== ^Ens.AppData 一级下标（谁有扫描凭证）==")
    conn = ic.get_connection()
    try:
        import iris  # type: ignore
        n = iris.createIRIS(conn)
        sub = n.nextSubscript(False, "^Ens.AppData", "")
        if not sub:
            log.info("  （空）")
        while sub:
            log.info("  %s", sub)
            s2 = n.nextSubscript(False, "^Ens.AppData", sub, "")
            while s2:
                log.info("      %s = %r", s2, n.get("^Ens.AppData", sub, s2))
                s2 = n.nextSubscript(False, "^Ens.AppData", sub, s2)
            sub = n.nextSubscript(False, "^Ens.AppData", sub)

        log.info("\n== SQL 源 BS 的实际 settings（Ens_Config.Item）==")
        raw = ic.class_method_value("demo.PipelineQuery", "GetItemsDetail", PROD)
        import json
        for it in json.loads(raw or "[]"):
            if it.get("type") == "SQLService" or "SQLService" in str(it.get("name")):
                log.info("  %s:", it.get("name"))
                for k, v in (it.get("settings") or {}).items():
                    log.info("      %-20s %s", k, str(v)[:120])

        log.info("\n== SQL 源 last key（EnsLib.SQL.InboundAdapter.GetPersistentValue %LastKey "
                 "—— 非空表示该 BS 已消费过数据）==")
        for it in json.loads(raw or "[]"):
            nm = str(it.get("name") or "")
            if "SQLService" not in nm:
                continue
            try:
                last = ic.class_method_value("EnsLib.SQL.InboundAdapter",
                                             "GetPersistentValue", nm, "%LastKey")
            except Exception as exc:  # noqa: BLE001
                last = f"<读取失败: {str(exc)[:60]}>"
            log.info("  %-34s last key = %r", nm, last)
        log.info("  ⚠ last key 非空 + 重新生成过同名 BS → 不会重发历史行（要重扫用 rescan_sql_source.py）")

        log.info("\n== 消息历史规模 ==")
        cnt = ic.query("SELECT COUNT(*) FROM Ens.MessageHeader")
        log.info("  Ens.MessageHeader 行数: %s", cnt)
        for row in ic.query("SELECT TOP 8 ID, SourceConfigName, TargetConfigName, "
                            "Status, TimeCreated FROM Ens.MessageHeader ORDER BY ID DESC"):
            log.info("  %s %s -> %s status=%s %s", row[0], row[1], row[2], row[3], row[4])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
