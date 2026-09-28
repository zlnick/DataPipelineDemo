"""IRIS 目标表初始化脚本。

在 backend 容器启动时自动执行：
1. 等待 IRIS 超级服务器就绪（重试 + 退避）。
2. 创建「模拟远端数据库」的目标表（Patient / Observation，默认 SQLUser schema），
   作为数据投放的转换目标（对应 FHIR Patient / Observation 资源）。

说明：目标表建在 USER namespace，模拟"远端数据库"；数据由后续 Production
管道从 FHIR 数据源转换后写入。
"""

import logging
import time

import iris

from backend.config import IRISConfig
from backend.services import iris_connector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 建表 SQL（默认 SQLUser schema，表名不带前缀）
CREATE_TABLES_SQL = [
    # 患者目标表（对应 FHIR Patient）
    """
    CREATE TABLE Patient (
        ID VARCHAR(20) PRIMARY KEY,
        FamilyName VARCHAR(50),
        GivenName VARCHAR(50),
        Gender VARCHAR(20),
        BirthDate VARCHAR(20),
        Phone VARCHAR(20),
        Address VARCHAR(200),
        City VARCHAR(50)
    )
    """,
    # SQL 源演示表（模拟"第三方业务库"患者表，结构与 Patient 一致但独立）：
    # 供 SQL→SOAP 管道的 SQL 源轮询使用，避免与 FHIR→DB 目标表（Patient）同表造成回环
    """
    CREATE TABLE PatientSource (
        ID VARCHAR(20) PRIMARY KEY,
        FamilyName VARCHAR(50),
        GivenName VARCHAR(50),
        Gender VARCHAR(20),
        BirthDate VARCHAR(20),
        Phone VARCHAR(20),
        Address VARCHAR(200),
        City VARCHAR(50)
    )
    """,
    # 检验观察目标表（对应 FHIR Observation）
    """
    CREATE TABLE Observation (
        ID VARCHAR(20) PRIMARY KEY,
        PatientID VARCHAR(20),
        Code VARCHAR(50),
        CodeDisplay VARCHAR(100),
        Value VARCHAR(50),
        Unit VARCHAR(20),
        EffectiveDate VARCHAR(20),
        Status VARCHAR(20)
    )
    """,
    # 增量同步队列表（FHIRSyncService 写入，FHIRService 的 SQL Inbound 逐条消费）
    """
    CREATE TABLE FHIRQueue (
        ID BIGINT IDENTITY PRIMARY KEY,
        ResourceType VARCHAR(50),
        ResourceId VARCHAR(50),
        ResourceJson VARCHAR(10000),
        MappingId VARCHAR(50),
        Status VARCHAR(20) DEFAULT 'pending'
    )
    """,
    # SOAP 写入型目标演示表（Python mock 收到 AddPatient 实体后保存于此，
    # 便于演示「转换后的三字段实体投递到第三方 HIS 成功」）
    """
    CREATE TABLE PatientEntity (
        PatientNo VARCHAR(20) PRIMARY KEY,
        FullName VARCHAR(100),
        Gender VARCHAR(20)
    )
    """,
]


def wait_for_iris(max_retries: int = 60, delay: float = 2.0) -> None:
    """等待 IRIS 超级服务器就绪。

    参数:
        max_retries: 最大重试次数。
        delay: 每次重试间隔（秒）。
    """
    for attempt in range(1, max_retries + 1):
        try:
            conn = iris.connect(**IRISConfig.as_dict())
            conn.close()
            logger.info("IRIS 已就绪（第 %d 次尝试成功）", attempt)
            return
        except Exception as exc:  # noqa: BLE001 - 等待期间任何连接异常都应继续重试
            logger.info("等待 IRIS 就绪中...（第 %d/%d 次）: %s", attempt, max_retries, exc)
            time.sleep(delay)
    raise RuntimeError(f"IRIS 在 {max_retries * delay:.0f} 秒内未能就绪，初始化失败。")


def create_tables(conn) -> None:
    """创建目标表与队列表（已存在则先删除，保证脚本可重复执行）。"""
    cursor = conn.cursor()
    try:
        cursor.execute("DROP TABLE IF EXISTS PatientEntity")
        cursor.execute("DROP TABLE IF EXISTS PatientSource")
        cursor.execute("DROP TABLE IF EXISTS FHIRQueue")
        cursor.execute("DROP TABLE IF EXISTS Observation")
        cursor.execute("DROP TABLE IF EXISTS Patient")
        for sql in CREATE_TABLES_SQL:
            cursor.execute(sql)
        conn.commit()
        logger.info("目标表/队列表创建完成：Patient / PatientSource / Observation / FHIRQueue / PatientEntity")
    finally:
        cursor.close()


def ensure_clinic_tables() -> None:
    """确保 CLINIC 演示源库四表存在（Patient/Encounter/Diagnosis/MedicationOrder，幂等）。

    CLINIC 命名空间由 iris/setup.sh 创建；这里只做"缺表补建"，**不删已有数据**。
    失败只告警不抛出：既不影响 USER 侧初始化，也不阻止 backend 启动
    （例如容器刚起时 CLINIC 命名空间尚未建好）。
    """
    try:
        from backend.services.clinic_schema import ensure_tables

        conn = iris.dbapi.connect(hostname="iris", port=1972, namespace="CLINIC",
                                  username="superuser", password="SYS")
        try:
            cur = conn.cursor()
            rows, created = ensure_tables(cur)
            conn.commit()
            logger.info("CLINIC 源库四表就绪（本次新建：%s）：%s",
                        ",".join(created) or "无", ", ".join("%s=%s" % r for r in rows))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - CLINIC 未就绪时跳过
        logger.warning("CLINIC 源库建表检查跳过/失败（由初始化脚本兜底）：%s", exc)


def main() -> None:
    """初始化主流程。"""
    # 1. 等待 IRIS 就绪
    wait_for_iris()

    # 2. 建目标表（模拟远端数据库的表结构，供演示选表）
    conn = iris_connector.get_sql_connection()
    try:
        create_tables(conn)
        logger.info("IRIS 目标表初始化完成（表结构保留，数据待管道投放）")
    finally:
        conn.close()

    # 3. 确保 CLINIC 演示源库四表存在（幂等；CLINIC 命名空间未就绪时自动跳过）
    ensure_clinic_tables()


if __name__ == "__main__":
    main()
