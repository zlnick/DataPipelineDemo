# -*- coding: utf-8 -*-
"""验证 DSN 兜底：已存在的返回 exists，不存在则创建后返回 created（并清理测试 DSN）。"""
from backend.services import iris_connector as ic
from backend.services import jdbc_dsn

print("USER（已存在）->", jdbc_dsn.ensure_jdbc_dsn("USER", "jdbc:IRIS://iris:1972/USER"))
print("PROBE_DSN（不存在）->", jdbc_dsn.ensure_jdbc_dsn("PROBE_DSN", "jdbc:IRIS://iris:1972/USER"))
# 清理测试 DSN（保持环境干净）
try:
    print("cleanup ->", ic.class_method_value("%SQLConnection", "Delete", "PROBE_DSN"))
except Exception as exc:  # noqa: BLE001
    print("cleanup 跳过:", str(exc)[:120])
print("当前 DSN 列表:")
for r in ic.query("SELECT connection_name FROM %Library.sys_SQLConnection"):
    print("  ", r[0])
