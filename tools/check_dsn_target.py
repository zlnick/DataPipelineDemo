# -*- coding: utf-8 -*-
"""检查 SQL 源所用 DSN 的真实定义，并（可选）用 JDBC 网关实测该 DSN 能否查到 CLINIC 数据。

回答的问题：SQL 源 BS 轮询「0 行」时，DSN 到底连到哪个库、查询能否返回行。

用法（容器内）：cd tools/datakit && ./run.sh check_dsn_target.py [DSN名]
"""
import sys

from backend.services import iris_connector as ic

dsn = sys.argv[1] if len(sys.argv) > 1 else "DS_DS46798"
print("== %Library.sys_SQLConnection 中的 DSN ==")
for row in ic.query("SELECT Connection_Name, DSN, URL FROM %Library.sys_SQLConnection"):
    print("  ", row)

print(f"\n== 目标 DSN = {dsn} ==")
print("  ", ic.query("SELECT Connection_Name, DSN, URL FROM %Library.sys_SQLConnection "
                      "WHERE Connection_Name=?", [dsn]))

print("\n== 该 DSN 在 Production 中的使用者（SQL 源 BS 的 Adapter 设置）==")
try:
    import json

    raw = ic.class_method_value("demo.PipelineQuery", "GetItemsDetail",
                                "demo.DataflowProduction")
    hit = 0
    for it in json.loads(raw or "[]"):
        st = it.get("settings") or {}
        if str(st.get("DSN") or "") != dsn:
            continue
        hit += 1
        print("  %-42s Query=%s | KeyFieldName=%s | JGService=%s"
              % (it.get("name"), st.get("Query"), st.get("KeyFieldName"), st.get("JGService")))
    if not hit:
        print("  （当前 Production 无组件使用该 DSN）")
except Exception as exc:  # noqa: BLE001
    print("  读取失败:", str(exc)[:160])
print("  （实测连通/查询请用 diag_sql_source_runtime.py 看扫描凭证 + 消息数；"
      "JDBC 实测由 SQL 源 BS 自身在轮询时完成）")
