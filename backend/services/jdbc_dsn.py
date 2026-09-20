# -*- coding: utf-8 -*-
"""JDBC DSN 兜底：SQL 源轮询 / DB 目标写入都要求 DSN 真实存在，不存在时按 jdbc_url 自动创建。

背景（真实踩坑）：数据源的 jdbc_url 指向某个 namespace（如 `jdbc:IRIS://iris:1972/USER`）时，
平台按 URL 推导出的 DSN 名（`USER`）未必真实存在 —— 系统里通常只有 `localTarget`(USER 写) 与
`CLINIC`(演示源 + 默认 DB 目标库)，于是 BS 生成出 `DSN=USER`，启动时退化成 ODBC 并报
`ErrOutConnectFailed: ODBC Connect failed ... Access Denied`。

本模块在生成前把 DSN 兜底创建（幂等），保证 SQL 源 / DB 目标组件引用的 DSN 一定可用。

演示默认命名空间口径（2026-09-16）：**SQL 源 = `USER`（`SQLUser.Patient`）、SQL 目标 = `CLINIC`**
—— 源/目标两侧的 DSN 名都按 jdbc_url 的**命名空间**推导（`jdbc:IRIS://host:1972/<ns>` → `<ns>`），
所以 UI 上改 jdbc 链接（换命名空间）即真正改读/写的库；`localTarget` 仅作"无 jdbc_url"时的历史兜底。

命名规则（源侧 `dsn_name_for` / 目标侧 `dsn_name_for_target` 同口径）：
① 已有同**命名空间**的规范化 DSN 名 → 复用它（幂等）；② 该名字被别的命名空间占用 → 放弃它，
退"按 URL 复用既有 DSN"（源侧，历史兼容）或 `localTarget`（目标侧）；
③ 命名空间都取不到 → 源侧 `DS_<数据源ID>`、目标侧 `localTarget`。
⚠ 刻意**不**做"任意按 URL 复用"（源侧仅作兜底）：IRIS 默认大小写不敏感排序规则下，
`…/CLINIC` 会命中历史遗留 DSN `DS_DS46798`（其 URL 是 `…/Clinic`），
导致组件引用怪名 DSN 且难以清理（实测）。
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


def find_dsn_url_by_name(name: str) -> str:
    """按 DSN 名取它连的 jdbc url（不存在返回 ""）。用于「同名 DSN 是否真的连本命名空间」判据。"""
    name = str(name or "").strip()
    if not name:
        return ""
    try:
        for row in iris_connector.query(
                "SELECT URL FROM %Library.sys_SQLConnection WHERE Connection_Name = ?", [name]):
            return str(row[0] or "")
    except Exception as exc:  # noqa: BLE001 - 查不到按"未知/无"处理（走兜底路径，不比现状更差）
        logger.warning("按名查 DSN URL 失败（%s）: %s", name, exc)
    return ""


def dsn_name_for(ds: dict) -> str:
    """给 SQL 数据源定 DSN 名（与目标侧 `dsn_name_for_target` 同口径：**命名空间即 DSN 名**）。

    - jdbc_url 的命名空间可用（演示默认源库 USER → DSN `USER`）→ 用命名空间名，
      由 `ensure_jdbc_dsn` 幂等创建；仅当**同名 DSN 已存在却连别的命名空间**时才放弃它
      （避免静默连错库，退 ②）；
    - 命名空间取不到 → 复用同 URL 的既有 DSN（历史兼容）→ `DS_<数据源ID>`（唯一、可追溯）。
    """
    ds = ds or {}
    url = jdbc_url_of(ds)
    ns = namespace_of(url)
    if ns:
        cur = find_dsn_url_by_name(ns)
        if not cur or not namespace_of(cur) or namespace_of(cur).lower() == ns.lower():
            return ns
        logger.warning("DSN %s 已被 %s 占用（≠ %s），数据源 %s 退按 URL 复用",
                       ns, cur, url, ds.get("id"))
    existing = find_dsn_by_url(url)
    if existing:
        return existing
    return f"DS_{ds.get('id')}"


def jdbc_url_of(ds: dict) -> str:
    """从数据源（runtime 契约优先，回落 config）取 jdbc url。"""
    ds = ds or {}
    conn = (ds.get("runtime") or {}).get("connection") or {}
    return str(conn.get("jdbc_url") or (ds.get("config") or {}).get("jdbc_url") or "").strip()


def namespace_of(jdbc_url: str) -> str:
    """从 IRIS JDBC url 取命名空间：`jdbc:IRIS://host:1972/<ns>` → `<ns>`（非 IRIS url 返回 ""）。"""
    url = str(jdbc_url or "").strip()
    if not url.lower().startswith("jdbc:iris:"):
        return ""
    return url.rsplit("/", 1)[-1].strip()


def jdbc_url_of_target(tg: dict) -> str:
    """从数据目标（runtime 契约优先，回落 connection/config）取 jdbc url。"""
    tg = tg or {}
    conn = (tg.get("runtime") or {}).get("connection") or {}
    return str(conn.get("jdbc_url") or (tg.get("connection") or {}).get("jdbc_url")
               or (tg.get("config") or {}).get("jdbc_url") or "").strip()


def dsn_name_for_target(tg: dict) -> str:
    """给数据目标定 DSN 名（与数据源侧 `dsn_name_for` 同口径：**命名空间即 DSN 名**）。

    - 已显式登记 dsn → 用它；
    - 否则取 jdbc_url 的**命名空间**（演示默认目标库 CLINIC → DSN `CLINIC`）；
    - 都没有 → 历史兜底 `localTarget`（= USER）。

    备注（实测）：不要用"按 URL 找已有 DSN"来复用 —— IRIS 默认大小写不敏感排序规则下，
    `…/CLINIC` 会命中历史遗留 DSN `DS_DS46798`（其 URL 是 `…/Clinic`），导致目标引用怪名 DSN；
    命名空间同名 DSN 不存在时由 `register_for_target` 幂等创建。
    """
    tg = tg or {}
    rt_conn = (tg.get("runtime") or {}).get("connection") or {}
    explicit = str(rt_conn.get("dsn")
                   or (tg.get("connection") or {}).get("dsn") or "").strip()
    url = jdbc_url_of_target(tg)
    if explicit and not explicit.startswith("jdbc:"):
        return explicit
    if not url and explicit.lower().startswith("jdbc:iris:"):
        url = explicit  # dsn 字段被误填成 jdbc url 时也能推导
    name = namespace_of(url) or "localTarget"
    # 可观测性：同名 DSN 已存在却连别的命名空间（病态情形：用户手工建了同名 DSN）
    # → 仍按名引用（显式名优先），但留一条告警，免得"目标登记 A、实际写 B"查不出来。
    cur = (find_dsn_url_by_name(name) or "") if name != "localTarget" else ""
    if cur and namespace_of(cur) and namespace_of(cur).lower() != name.lower():
        logger.warning("目标 %s 的 DSN %s 实际连的是 %s（与命名空间不一致），请核对",
                       tg.get("id"), name, cur)
    return name


def register_for_target(tg: dict) -> str:
    """【目标侧主路径】为 DB 目标注册 DSN 并写回 runtime/connection，返回 DSN 名。

    调用时机：DB 目标创建、连通测试（与数据源侧 `register_for_datasource` 对称）。
    实测缺陷（2026-09-16）：DB 目标原先既无此步骤、生成时 DSN 又硬编码 `localTarget`(USER)
    → 目标登记的 `jdbc:IRIS://…/CLINIC` 被静默忽略，转换结果写进了 USER 库。
    """
    from backend.services import repository  # 延迟导入避免循环依赖
    tg = tg or {}
    tg_id = tg.get("id")
    url = jdbc_url_of_target(tg)
    if not tg_id or not url:
        return ""
    name = dsn_name_for_target(tg)
    conn = (tg.get("runtime") or {}).get("connection") or (tg.get("connection") or {})
    status = ensure_jdbc_dsn(name, url, conn.get("username") or "superuser",
                             conn.get("password") or "SYS")
    logger.info("数据目标 %s 的 DSN 注册: %s -> %s（%s）", tg_id, name, url, status)
    rt = tg.get("runtime") or repository.target_runtime(tg)
    rconn = dict(rt.get("connection") or {})
    rconn["dsn"] = name
    rconn["jdbc_url"] = url
    rt["connection"] = rconn
    repository.update_target(tg_id, {
        "connection": {**(tg.get("connection") or {}), "dsn": name},
        "runtime": rt,
    })
    return name


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
