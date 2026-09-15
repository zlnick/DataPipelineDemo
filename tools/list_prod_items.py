# -*- coding: utf-8 -*-
"""打印 Production 中 BS/BO 组件名与启用状态（用于确认 SQL 源 Item 名）。"""
# -*- coding: utf-8 -*-
"""打印 Production 中 BS/BO 组件名与启用状态（用于确认 SQL 源 Item 名）。"""
from backend.services import iris_connector

cols = iris_connector.query(
    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA='Ens_Config' AND TABLE_NAME='Item' ORDER BY ORDINAL_POSITION")
print("columns:", [c[0] for c in cols])
rows = iris_connector.query("SELECT TOP 20 * FROM Ens_Config.Item")
for r in rows:
    print(r)

