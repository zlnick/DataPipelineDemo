# -*- coding: utf-8 -*-
"""双管道联调检查：SOAP 落库（PatientEntity）+ 最近业务消息。"""
from backend.services import iris_connector as ic

print("PatientEntity 总数:", ic.query("SELECT COUNT(*) FROM SQLUser.PatientEntity"))
print("Patient 源行数:", ic.query("SELECT COUNT(*) FROM SQLUser.Patient"))
print("P 开头（源 → SOAP 落库的全部行）:")
for r in ic.query("SELECT PatientNo, FullName, Gender FROM SQLUser.PatientEntity "
                  "WHERE PatientNo LIKE 'P%' ORDER BY PatientNo"):
    print("   ", r)
print("最近业务消息:")
for r in ic.query("SELECT TOP 10 ID, SourceConfigName, TargetConfigName, Status "
                  "FROM Ens.MessageHeader WHERE SourceConfigName NOT LIKE 'Ens.%' "
                  "ORDER BY ID DESC"):
    print("   ", r)
