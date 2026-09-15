# -*- coding: utf-8 -*-
"""验证 DSN 自动注册的命名分支，并清理测试 DSN(PROBE_DSN)。"""
from backend.services import iris_connector as ic
from backend.services import jdbc_dsn

# ① 已注册 URL → 复用原名（CLINIC/USER）
print("已注册 URL ->", jdbc_dsn.dsn_name_for(
    {"id": "DSX", "runtime": {"connection": {"jdbc_url": "jdbc:IRIS://iris:1972/USER"}}}))
# ② 未注册 URL → DS_<数据源ID>
print("未注册 URL ->", jdbc_dsn.dsn_name_for(
    {"id": "DSPROBE", "runtime": {"connection": {"jdbc_url": "jdbc:IRIS://iris:1972/NEWNS"}}}))

# ③ 清理测试 DSN
try:
    print("SQL 删除 PROBE_DSN ->", ic.execute(
        "DELETE FROM %Library.sys_SQLConnection WHERE Connection_Name=?", ["PROBE_DSN"]))
except Exception as exc:  # noqa: BLE001
    print("SQL 删除失败:", str(exc)[:160])
print("现有 DSN:", [r[0] for r in ic.query(
    "SELECT Connection_Name FROM %Library.sys_SQLConnection")])
