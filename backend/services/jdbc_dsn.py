# -*- coding: utf-8 -*-
"""JDBC DSN 兜底：SQL 源轮询要求 DSN 真实存在，不存在时按 jdbc_url 自动创建。

背景（真实踩坑）：数据源的 jdbc_url 指向某个 namespace（如 `jdbc:IRIS://iris:1972/USER`）时，
平台按 URL 推导出的 DSN 名（`USER`）未必真实存在 —— 系统里通常只有 `localTarget`(USER 写) 与
`CLINIC`(演示源)，于是 BS 生成出 `DSN=USER`，启动时退化成 ODBC 并报
`ErrOutConnectFailed: ODBC Connect failed ... Access Denied`。

本模块在生成前把 DSN 兜底创建（幂等），保证 SQL 源组件引用的 DSN 一定可用。
"""
import logging

from backend.services import iris_connector

logger = logging.getLogger(__name__)


def ensure_jdbc_dsn(name: str, jdbc_url: str,
                    user: str = "superuser", pwd: str = "SYS") -> str:
    """确保 DSN 存在（不存在则按 jdbc url 创建）。返回 exists/created/failed:.../skipped。"""
    name = str(name or "").strip()
    jdbc_url = str(jdbc_url or "").strip()
    if not name or not jdbc_url or name.startswith("jdbc:"):
        return "skipped"
    try:
        result = iris_connector.class_method_value(
            "demo.PipelineQuery", "EnsureJDBCDSN", name, jdbc_url,
            user or "superuser", pwd or "SYS")
        result = str(result or "")
        if result == "created":
            logger.info("DSN 兜底创建成功: %s -> %s", name, jdbc_url)
        elif result.startswith("failed"):
            logger.warning("DSN 兜底创建失败: %s -> %s", name, result)
        return result
    except Exception as exc:  # noqa: BLE001 - 兜底失败不阻断生成（生成后校验会暴露连接问题）
        logger.warning("DSN 兜底调用失败（%s）: %s", name, exc)
        return f"failed:{exc}"


def find_dsn_by_url(jdbc_url: str) -> str:
    """按 jdbc url 查已注册的 DSN 名（复用现有 DSN）。空=未注册。"""
    jdbc_url = str(jdbc_url or "").strip()
    if not jdbc_url:
        return ""
    try:
        return str(iris_connector.class_method_value(
            "demo.PipelineQuery", "FindDSNByUrl", jdbc_url) or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("按 URL 查 DSN 失败: %s", exc)
        return ""


def dsn_name_for(ds: dict) -> str:
    """给数据源定一个稳定唯一的 DSN 名。

    - 该 jdbc_url 已注册过 DSN → 复用其名（如既有 CLINIC/localTarget，保持兼容）；
    - 否则用 `DS_<数据源ID>`（唯一、可追溯，避免"URL 的 namespace 名"撞名/不存在）。
    """
    ds = ds or {}
    url = jdbc_url_of(ds)
    existing = find_dsn_by_url(url)
    if existing:
        return existing
    return f"DS_{ds.get('id')}"


def jdbc_url_of(ds: dict) -> str:
    """从数据源（runtime 契约优先，回落 config）取 jdbc url。"""
    ds = ds or {}
    conn = (ds.get("runtime") or {}).get("connection") or {}
    return str(conn.get("jdbc_url") or (ds.get("config") or {}).get("jdbc_url") or "").strip()


def register_for_datasource(ds: dict) -> str:
    """【数据源侧主路径】为 SQL 数据源注册 DSN 并写回 runtime/config，返回 DSN 名。

    调用时机：连通测试成功、选表分析成功（即"连接已验证、表已纳入数据源"）。
    之后生成管道时直接读 runtime.connection.dsn 引用它，无需人工建 DSN。
    """
    from backend.services import repository  # 延迟导入避免循环依赖
    ds = ds or {}
    ds_id = ds.get("id")
    url = jdbc_url_of(ds)
    if not ds_id or not url:
        return ""
    name = dsn_name_for(ds)
    user = ((ds.get("runtime") or {}).get("connection") or {}).get("username") \
        or (ds.get("config") or {}).get("username") or "superuser"
    pwd = ((ds.get("runtime") or {}).get("connection") or {}).get("password") \
        or (ds.get("config") or {}).get("password") or "SYS"
    status = ensure_jdbc_dsn(name, url, user, pwd)
    logger.info("数据源 %s 的 DSN 注册: %s -> %s（%s）", ds_id, name, url, status)
    # 写回：runtime 契约 + config（单一事实源，生成管道直接引用）
    rt = ds.get("runtime") or repository.datasource_runtime(ds)
    conn = dict(rt.get("connection") or {})
    conn["dsn"] = name
    conn["jdbc_url"] = url
    rt["connection"] = conn
    repository.update_datasource(ds_id, {
        "config": {**(ds.get("config") or {}), "dsn": name},
        "runtime": rt,
    })
    return name


def ensure_for(cfg: dict) -> str:
    """按源配置（dsn/jdbc_url/username/password）做 DSN 兜底。"""
    cfg = cfg or {}
    return ensure_jdbc_dsn(cfg.get("dsn"), cfg.get("jdbc_url"),
                           cfg.get("username") or "superuser",
                           cfg.get("password") or "SYS")
