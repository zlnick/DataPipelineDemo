# -*- coding: utf-8 -*-
"""诊断：查最近消息头 + 源扫描凭证 global，确认重扫是否发生。"""
from backend.services import iris_connector

print("--- 最近 12 条消息头 ---")
rows = iris_connector.query(
    "SELECT TOP 12 ID, TimeCreated, SourceConfigName, TargetConfigName, Status "
    "FROM Ens.MessageHeader ORDER BY ID DESC")
for r in rows:
    print(r)

print("--- SQLUser.Patient 列名 ---")
cols = iris_connector.query(
    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA='SQLUser' AND TABLE_NAME='Patient' ORDER BY ORDINAL_POSITION")
print([c[0] for c in cols])
print("--- SQLUser.Patient 数据 ---")
for r in iris_connector.query("SELECT TOP 10 * FROM SQLUser.Patient"):
    print(r)
print("--- 消息数 / 最新 ID ---")
print(iris_connector.query("SELECT COUNT(*) AS C, MAX(ID) AS M FROM Ens.MessageHeader"))
