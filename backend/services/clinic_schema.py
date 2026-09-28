# -*- coding: utf-8 -*-
"""CLINIC 演示源库的表结构定义（单一事实源）。

四张源表（刻意保留与 FHIR 资源的可映射性）：
  Patient         患者   → us-core-patient
  Encounter       就诊   → us-core-encounter
  Diagnosis       诊断   → us-core-condition-encounter-diagnosis
  MedicationOrder 药嘱   → us-core-medicationrequest

被谁使用：
- ``backend/services/clinic_seed.py``：生成演示数据前 ensure 四表存在（幂等、不删数据）；
- ``tools/clinic_tables.py``：命令行入口（默认 ensure；``--reset`` 才 DROP 重建）；
- ``init_data.py``：backend 启动时 ensure（CLINIC 命名空间未就绪则跳过）。

⚠ 与旧版 ``tools/clinic_tables.py`` 的区别：旧版一律 ``DROP TABLE`` 重建（清空数据），
本模块默认 **只在缺表时 CREATE**，已有表与数据一律不动，故可安全放进初始化流程。
"""

CLINIC_TABLES = ("Patient", "Encounter", "Diagnosis", "MedicationOrder")

CLINIC_DDL = [
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
    # 诊断（FHIR Condition, encounter-diagnosis；Code/Name/CodeSystem 取国标 ICD-10 中文术语）
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
    ("SQLUser.MedicationOrder.CodeSystem",
     "术语集 URI=urn:cn-nhsa:drug-nrdl(医保) / urn:cn-nhsa:drug-cbih(商保)"),
]


def table_exists(cur, name: str) -> bool:
    """判断 CLINIC（SQLUser schema）下的表是否存在。"""
    cur.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = 'SQLUser' AND TABLE_NAME = ?", (name,))
    return int(cur.fetchone()[0]) > 0


def ensure_tables(cur, reset: bool = False) -> list:
    """确保 CLINIC 四表存在；缺表才建。返回 ``[(表名, 行数)]``。

    参数:
        cur:   IRIS DB-API 游标（需已连到 CLINIC 命名空间）。
        reset: True 时按旧行为先 ``DROP TABLE IF EXISTS`` 再重建（**会清空数据**，仅重置场景使用）。
    """
    if reset:
        for name in CLINIC_TABLES:
            cur.execute("DROP TABLE IF EXISTS %s" % name)
    created = []
    for name, sql in zip(CLINIC_TABLES, CLINIC_DDL):
        if reset or not table_exists(cur, name):
            cur.execute(sql)
            created.append(name)
    out = []
    for name in CLINIC_TABLES:
        cur.execute("SELECT COUNT(*) FROM %s" % name)
        out.append((name, int(cur.fetchone()[0])))
    return out, created
