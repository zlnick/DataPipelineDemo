# -*- coding: utf-8 -*-
"""CLINIC 命名空间：患者/就诊/诊断/药嘱 四张源表初始化（幂等）。

数据源表（面向后续「SQL 表 → FHIR(US Core)」转换演示）：
  Patient        患者    → us-core-patient
  Encounter      就诊    → us-core-encounter
  Diagnosis      诊断    → us-core-condition-encounter-diagnosis
  MedicationOrder 药嘱   → us-core-medicationrequest
字段刻意保留与 FHIR 资源的可映射性（ID/外键/类目/状态/编码/文本）。

用法: python3 tools/clinic_tables.py
"""
import logging

import iris.dbapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HOST, PORT, NS, USER, PWD = "127.0.0.1", 1972, "CLINIC", "superuser", "SYS"

DDL = [
    # 患者（FHIR Patient）
    """
    CREATE TABLE Patient (
        ID          VARCHAR(20) PRIMARY KEY,
        MRN         VARCHAR(30),
        FamilyName  VARCHAR(50),
        GivenName   VARCHAR(50),
        Gender      VARCHAR(10),
        BirthDate   VARCHAR(20),
        Phone       VARCHAR(30),
        Address     VARCHAR(200),
        City        VARCHAR(50)
    )
    """,
    # 就诊（FHIR Encounter）
    """
    CREATE TABLE Encounter (
        ID           VARCHAR(20) PRIMARY KEY,
        PatientID    VARCHAR(20),
        ClassCode    VARCHAR(20),
        ClassDisplay VARCHAR(50),
        Status       VARCHAR(20),
        PeriodStart  VARCHAR(30),
        PeriodEnd    VARCHAR(30),
        ReasonCode   VARCHAR(20),
        ReasonText   VARCHAR(200)
    )
    """,
    # 诊断（FHIR Condition, encounter-diagnosis；Rank=主/次诊断；Code/Name/CodeSystem 取国标 ICD-10 中文术语）
    """
    CREATE TABLE Diagnosis (
        ID             VARCHAR(20) PRIMARY KEY,
        EncounterID    VARCHAR(20),
        PatientID      VARCHAR(20),
        Code           VARCHAR(20),
        Name           VARCHAR(200),
        CodeSystem     VARCHAR(60),
        Rank           INTEGER DEFAULT 0,
        OnsetDate      VARCHAR(20),
        ClinicalStatus VARCHAR(20) DEFAULT 'active'
    )
    """,
    # 药嘱（FHIR MedicationRequest；MedicationCode/Name/CodeSystem 取中文药品目录术语）
    """
    CREATE TABLE MedicationOrder (
        ID            VARCHAR(20) PRIMARY KEY,
        EncounterID   VARCHAR(20),
        PatientID     VARCHAR(20),
        MedicationCode VARCHAR(30),
        MedicationName VARCHAR(200),
        CodeSystem    VARCHAR(60),
        DosageValue   VARCHAR(50),
        DosageUnit    VARCHAR(20),
        Route         VARCHAR(50),
        Frequency     VARCHAR(50),
        StartDate     VARCHAR(20),
        Status        VARCHAR(20) DEFAULT 'active'
    )
    """,
]

# 关键列 CodeSystem 说明（供后续术语对照/LLM 使用）
COLUMN_COMMENTS = [
    ("SQLUser.Patient.ID", "患者主键"),
    ("SQLUser.Patient.MRN", "病历号（映射 us-core-patient.identifier, 体系 http://hospital.example/mrn）"),
    ("SQLUser.Patient.Gender", "FHIR administrative-gender：male|female|other"),
    ("SQLUser.Encounter.ID", "就诊主键"),
    ("SQLUser.Encounter.ClassCode", "v3 ActCode 就诊类别：IMP(住院)/AMB(门诊)/…"),
    ("SQLUser.Encounter.ReasonCode", "就诊原因=主诊断，CodeSystem=urn:cn-nhsa:icd10-gbt2016（国标 ICD-10）"),
    ("SQLUser.Encounter.ReasonText", "主诊断中文名（国标 ICD-10）"),
    ("SQLUser.Diagnosis.Code", "国标 ICD-10 诊断码（GB/T 14396-2016，已剔除 Z 章）"),
    ("SQLUser.Diagnosis.Name", "国标 ICD-10 诊断中文名"),
    ("SQLUser.Diagnosis.CodeSystem", "术语集 URI=urn:cn-nhsa:icd10-gbt2016"),
    ("SQLUser.MedicationOrder.MedicationCode", "中文药品目录编码（如 NRDL-xxxx / CB-xxxx）"),
    ("SQLUser.MedicationOrder.MedicationName", "中文药品名（医保目录名称）"),
    ("SQLUser.MedicationOrder.CodeSystem", "术语集 URI=urn:cn-nhsa:drug-nrdl(医保) / urn:cn-nhsa:drug-cbih(商保)"),
]

TABLES = ["Patient", "Encounter", "Diagnosis", "MedicationOrder"]


def main() -> None:
    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username=USER, password=PWD)
    cur = conn.cursor()
    for t in TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    for sql in DDL:
        cur.execute(sql)
    conn.commit()
    # 验证
    for t in TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        logger.info("%s 表就绪（行数=%s）", t, cur.fetchone()[0])
    conn.close()
    logger.info("CLINIC 四表初始化完成")


if __name__ == "__main__":
    main()
