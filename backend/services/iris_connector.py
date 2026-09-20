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

    说明：IRIS 社区版许可证按**进程/连接**计数，backend 侧连接数与 Ens 业务主机
    （每个组件 1 个进程）共同竞争许可证。这里保持"用后即关"的最小占用策略，
    由调用方负责 close（iris_connector.reset_connections 兜底）。

    返回:
        iris.IRISConnection: 原生连接对象。
    """
    return iris.connect(**IRISConfig.as_dict())


def get_sql_connection():
    """获取一个 IRIS DB-API 连接，用于执行 SQL。

    返回:
        iris.dbapi.Connection: DB-API 连接对象（由调用方 close）。
    """
    import iris.dbapi

    return iris.dbapi.connect(
        hostname=IRISConfig.HOST,
        port=IRISConfig.PORT,
        namespace=IRISConfig.NAMESPACE,
        username=IRISConfig.USERNAME,
        password=IRISConfig.PASSWORD,
    )


def reset_connections() -> None:
    """兼容接口：清理可能残留的连接（当前实现为"用后即关"，无需缓存清理）。

    保留该函数是为了让上层（repository / pipelines / wsdl_importer / term_precheck）
    在原本 `conn.close()` 的位置调用统一的释放入口。
    """
    return None


def query(sql: str, params: list | None = None) -> list[tuple]:
    """执行一条 SQL 查询并返回全部结果行。

    参数:
        sql: SQL 语句。
        params: 可选的位置参数（? 占位符）。

    返回:
        查询结果行列表，每行为一个 tuple。
    """
    last_exc: Exception | None = None
    for attempt in (0, 1):
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
        except Exception as exc:  # noqa: BLE001 - 连接失效时重建后重试一次
            last_exc = exc
            if attempt:
                break
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败可忽略
                pass
    raise last_exc if last_exc else RuntimeError("SQL 查询失败")


def execute(sql: str, params: list | None = None) -> int:
    """执行一条写操作（INSERT / UPDATE / DELETE）并提交。

    参数:
        sql: SQL 语句。
        params: 可选的位置参数（? 占位符）。

    返回:
        受影响的行数。
    """
    last_exc: Exception | None = None
    for attempt in (0, 1):
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
        except Exception as exc:  # noqa: BLE001 - 连接失效时重建后重试一次
            last_exc = exc
            if attempt:
                break
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败可忽略
                pass
    raise last_exc if last_exc else RuntimeError("SQL 执行失败")


def class_method_value(className: str, methodName: str, *args):
    """调用 IRIS 类方法并返回结果（Native SDK，连接按线程复用）。

    参数:
        className: 类名（如 "demo.PipelineQuery"）。
        methodName: 方法名。
        args: 方法的位置参数。

    返回:
        类方法的返回值（字符串/数字等）。
    """
    last_exc: Exception | None = None
    for attempt in (0, 1):
        conn = get_connection()
        try:
            native = iris.createIRIS(conn)
            return native.classMethodValue(className, methodName, *args)
        except Exception as exc:  # noqa: BLE001 - 连接失效时重建后重试一次
            last_exc = exc
            if attempt:
                break
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败可忽略
                pass
    raise last_exc if last_exc else RuntimeError("类方法调用失败")


def class_method_void(className: str, methodName: str, *args) -> None:
    """调用**无返回值**的 IRIS 类方法（Native SDK classMethodVoid）。

    为什么要单独提供：Ens 的清理/控制类方法（如 `Ens.Adapter.ClearStaticAppData`、
    `Ens.Adapter.ClearRuntimeAppData`）无返回值，用 classMethodValue 调用会报
    `<COMMAND> ... *Function must return a value at ClearStaticAppData+1^Ens.Adapter.1`
    （易被误判为"库方法在本版本无效"），必须用 void 语义调用。

    参数:
        className: 类名（如 "Ens.Adapter"）。
        methodName: 方法名。
        args: 方法的位置参数。
    """
    last_exc: Exception | None = None
    for attempt in (0, 1):
        conn = get_connection()
        try:
            native = iris.createIRIS(conn)
            native.classMethodVoid(className, methodName, *args)
            return None
        except Exception as exc:  # noqa: BLE001 - 连接失效时重建后重试一次
            last_exc = exc
            if attempt:
                break
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败可忽略
                pass
    raise last_exc if last_exc else RuntimeError("类方法（void）调用失败")


def global_get(*subscripts, default=None):
    """读取 IRIS global 节点（Native SDK），失败返回 default。

    典型用途：读 `^Ens.Runtime("ConfigItem", <组件名>, "Job")` 判断 Ens 组件主机
    **是否真的在运行**（配置层 Enabled=1 不等于主机已启动，见 pipeline_validator.set_items_enabled）。
    """
    try:
        import iris  # 延迟导入：仅在需要 Native 访问时加载

        conn = get_connection()
        return iris.createIRIS(conn).get(*subscripts)
    except Exception as exc:  # noqa: BLE001 - 读取失败按"未取到"处理
        logger.debug("读取 global %s 失败: %s", subscripts, exc)
        return default


def ping() -> bool:
    """探测 IRIS 是否可连接。

    返回:
        连接成功返回 True，否则返回 False。
    """
    try:
        get_connection()
        return True
    except Exception as exc:  # noqa: BLE001 - 探测函数需要捕获所有连接异常
        logger.warning("IRIS 连接探测失败: %s", exc)
        reset_connections()
        return False
