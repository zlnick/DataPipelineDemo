"""IRIS 数据库连接封装模块。

基于 InterSystems Native SDK for Python（pip 包 intersystems-irispython，import iris），
从环境变量读取连接参数，向上层模块提供统一的连接获取与 SQL 查询方法。
"""

import logging

import iris

from backend.config import IRISConfig

logger = logging.getLogger(__name__)


def get_connection():
    """获取一个 IRIS 原生连接。

    返回:
        iris.IRISConnection: 原生连接对象，使用完毕后需调用 close() 释放。
    """
    return iris.connect(**IRISConfig.as_dict())


def get_sql_connection():
    """获取一个 IRIS DB-API 连接，用于执行 SQL。

    返回:
        iris.dbapi.Connection: DB-API 连接对象，使用完毕后需调用 close() 释放。
    """
    import iris.dbapi

    return iris.dbapi.connect(
        hostname=IRISConfig.HOST,
        port=IRISConfig.PORT,
        namespace=IRISConfig.NAMESPACE,
        username=IRISConfig.USERNAME,
        password=IRISConfig.PASSWORD,
    )


def query(sql: str, params: list | None = None) -> list[tuple]:
    """执行一条 SQL 查询并返回全部结果行。

    参数:
        sql: SQL 语句。
        params: 可选的位置参数（? 占位符）。

    返回:
        查询结果行列表，每行为一个 tuple。
    """
    conn = get_sql_connection()
    try:
        cursor = conn.cursor()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        rows = [tuple(row) for row in cursor.fetchall()]
        cursor.close()
        return rows
    finally:
        conn.close()


def execute(sql: str, params: list | None = None) -> int:
    """执行一条写操作（INSERT / UPDATE / DELETE）并提交。

    参数:
        sql: SQL 语句。
        params: 可选的位置参数（? 占位符）。

    返回:
        受影响的行数。
    """
    conn = get_sql_connection()
    try:
        cursor = conn.cursor()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        conn.commit()
        rowcount = getattr(cursor, "rowcount", 0) or 0
        cursor.close()
        return rowcount
    finally:
        conn.close()


def class_method_value(className: str, methodName: str, *args):
    """调用 IRIS 类方法并返回结果（Native SDK）。

    参数:
        className: 类名（如 "demo.PipelineQuery"）。
        methodName: 方法名。
        args: 方法的位置参数。

    返回:
        类方法的返回值（字符串/数字等）。
    """
    conn = get_connection()
    try:
        native = iris.createIRIS(conn)
        return native.classMethodValue(className, methodName, *args)
    finally:
        conn.close()


def ping() -> bool:
    """探测 IRIS 是否可连接。

    返回:
        连接成功返回 True，否则返回 False。
    """
    try:
        conn = get_connection()
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - 探测函数需要捕获所有连接异常
        logger.warning("IRIS 连接探测失败: %s", exc)
        return False
