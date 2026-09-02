"""JDBC 通用数据库连接封装（转换目标发现）。

基于 JayDeBeApi + JPype1 调用 JDBC（Java 标准数据库连接），
支持任意 JDBC 驱动（IRIS / MySQL / PostgreSQL / Oracle / SQL Server 等），
保证"连接数据库、选择目标（schema/表/列）"过程的通用性。

连接信息（Target.connection）格式：
{
  "jdbc_url": "jdbc:IRIS://iris:1972/USER",     # JDBC URL
  "driver_class": "com.intersystems.jdbc.IRISDriver",  # 驱动类
  "username": "superuser",
  "password": "SYS",
  "jar": "/app/jdbc/intersystems-jdbc-3.10.5.jar"       # 驱动 jar 路径（可选）
}
"""

import logging

import jaydebeapi

logger = logging.getLogger(__name__)

# 默认 IRIS JDBC 驱动 jar（docker-compose 挂载 ./jdbc -> /app/jdbc）
DEFAULT_JAR = "/app/jdbc/intersystems-jdbc-3.10.5.jar"
DEFAULT_DRIVER = "com.intersystems.jdbc.IRISDriver"


def _connect(conn: dict):
    """建立 JDBC 连接并返回连接对象（调用方负责 close）。"""
    url = conn.get("jdbc_url")
    if not url:
        raise ValueError("缺少 jdbc_url 连接信息")
    return jaydebeapi.connect(
        conn.get("driver_class", DEFAULT_DRIVER),
        url,
        [conn.get("username", ""), conn.get("password", "")],
        conn.get("jar", DEFAULT_JAR),
    )


def test_connection(conn: dict) -> dict:
    """联通测试：建立 JDBC 连接并执行 SELECT 1。

    参数:
        conn: JDBC 连接信息 dict。

    返回:
        {"ok": bool, "message": str, "product": str}。
    """
    try:
        with _connect(conn) as c:
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            product = ""
            try:
                product = c.jconn.getMetaData().getDatabaseProductName()
            except Exception:  # noqa: BLE001
                product = ""
        return {"ok": True, "message": "连接成功", "product": product}
    except Exception as exc:  # noqa: BLE001 - 需将连接错误反馈给前端
        logger.error("JDBC 联通测试失败: %s", exc)
        return {"ok": False, "message": str(exc)}


def list_schemas(conn: dict) -> list[str]:
    """列出数据库的所有 schema（DatabaseMetaData.getSchemas）。

    参数:
        conn: JDBC 连接信息 dict。

    返回:
        schema 名称列表（排序去重）。
    """
    with _connect(conn) as c:
        meta = c.jconn.getMetaData()
        rs = meta.getSchemas()
        schemas: list[str] = []
        try:
            while rs.next():
                schemas.append(rs.getString("TABLE_SCHEM"))
        finally:
            rs.close()
    return sorted(set(schemas))


def list_tables(conn: dict, schema: str) -> list[dict]:
    """列出指定 schema 的表（DatabaseMetaData.getTables，仅 TABLE 类型）。

    参数:
        conn: JDBC 连接信息 dict。
        schema: schema 名（可为空串或 None 表示不限定）。

    返回:
        [{"table": "Patient", "type": "TABLE", "remark": "..."}]。
    """
    with _connect(conn) as c:
        meta = c.jconn.getMetaData()
        rs = meta.getTables(None, schema or None, "%", ["TABLE"])
        tables: list[dict] = []
        try:
            while rs.next():
                tables.append({
                    "table": rs.getString("TABLE_NAME") or "",
                    "type": rs.getString("TABLE_TYPE") or "TABLE",
                    "remark": rs.getString("REMARKS") or "",
                })
        finally:
            rs.close()
    return sorted(tables, key=lambda t: t["table"])


def list_columns(conn: dict, schema: str, table: str) -> list[dict]:
    """分析指定表的列结构（DatabaseMetaData.getColumns）。

    参数:
        conn: JDBC 连接信息 dict。
        schema: schema 名。
        table: 表名。

    返回:
        [{"name": "ID", "type": "VARCHAR", "size": 20}]。
    """
    with _connect(conn) as c:
        meta = c.jconn.getMetaData()
        rs = meta.getColumns(None, schema or None, table, "%")
        columns: list[dict] = []
        try:
            while rs.next():
                columns.append({
                    "name": rs.getString("COLUMN_NAME") or "",
                    "type": rs.getString("TYPE_NAME") or "",
                    "size": rs.getInt("COLUMN_SIZE"),
                })
        finally:
            rs.close()
    return columns
