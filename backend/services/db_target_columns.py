"""DB 目标表的列清单解析（**必须按目标自身的库/命名空间取**）。

背景（2026-09-17 Round 2 实测 P1，静默丢列）：
DB 目标 SQLOp 的 UPSERT 列清单原先固定查**当前命名空间**（backend 的 `IRISConfig.NAMESPACE` = USER）
的 `information_schema`。演示默认口径是 **SQL 目标 = CLINIC**（跨库），USER 里恰好也有同名 `Patient`
表但列不同（USER 8 列 / CLINIC 9 列含 MRN）→ 生成的
`INSERT OR UPDATE INTO Patient (ID, FamilyName, …)` **缺 MRN** → 映射里 `→ MRN` 的条目**永远不落库**
（无任何告警；消息全 Completed、校验全绿）。若 USER 没有同名表，则列清单为空、SQLOp 直接被跳过。

取值顺序（权威优先，全部只做事实采集，不改变任何 AI 决策）：
    ① `target_config["table_columns"]`（登记时的 JDBC 元数据事实，最贴近目标库）
    ② 目标命名空间 `information_schema`（按 `jdbc_url` 的命名空间直连；仅当目标就是本 IRIS 实例）
    ③ 当前命名空间 `information_schema` 兜底 + **显式告警**（提示可能不是目标库）
"""

import logging

from backend.config import IRISConfig

logger = logging.getLogger(__name__)


def _names(cols) -> list[str]:
    """列结构（dict/str 混合）→ 列名清单。"""
    out: list[str] = []
    for c in cols or []:
        if isinstance(c, dict) and c.get("name"):
            out.append(str(c["name"]))
        elif isinstance(c, str) and c:
            out.append(c)
    return out


def _from_config(table: str, cfg: dict) -> list[str]:
    """从 target_config 的登记事实里取该表列清单（table_columns 按表名索引，大小写不敏感）。"""
    if not isinstance(cfg, dict):
        return []
    mapping = cfg.get("table_columns")
    if isinstance(mapping, dict):
        for k, v in mapping.items():
            if str(k).lower() == str(table).lower():
                cols = _names(v)
                if cols:
                    return cols
    cols = _names(cfg.get("columns"))
    return cols


def _from_namespace(namespace: str, table: str, schema: str) -> list[str]:
    """按命名空间直连查 information_schema（跨库目标的正确来源）。"""
    import iris.dbapi

    conn = iris.dbapi.connect(hostname=IRISConfig.HOST, port=IRISConfig.PORT,
                              namespace=namespace, username=IRISConfig.USERNAME,
                              password=IRISConfig.PASSWORD)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
                        (schema, table))
            return [str(r[0]) for r in cur.fetchall()]
        finally:
            cur.close()
    finally:
        conn.close()


def columns_of(table: str, target_config: dict | None = None) -> list[str]:
    """解析 DB 目标表的列清单（按目标库取；取不到时兜底当前命名空间并告警）。

    参数:
        table: 目标表名。
        target_config: 目标连接契约（`jdbc_url`/`dsn`/`table_columns`；可空）。

    返回:
        列名列表（按 ordinal_position）；全失败返回 []（调用方决定是否跳过该 SQLOp）。
    """
    tbl = str(table or "").strip()
    if not tbl:
        return []
    cfg = target_config or {}
    schema = str(cfg.get("schema") or "SQLUser").strip() or "SQLUser"

    # ① 登记事实（注册时按目标 JDBC 元数据取得）
    cols = _from_config(tbl, cfg)
    if cols:
        return cols

    # ② 目标命名空间（jdbc_url 的命名空间；只在本 IRIS 实例上直连）
    from backend.services import jdbc_dsn
    url = cfg.get("jdbc_url") or ""
    ns = jdbc_dsn.namespace_of(url) if url else ""
    if ns:
        try:
            cols = _from_namespace(ns, tbl, schema)
            if cols:
                logger.info("DB 目标列清单取自命名空间 %s: %s(%d 列)", ns, tbl, len(cols))
                return cols
            logger.warning("命名空间 %s 中未找到表 %s.%s（按当前命名空间兜底）", ns, schema, tbl)
        except Exception as exc:  # noqa: BLE001 - 探查失败即降级
            logger.warning("按命名空间 %s 取列清单失败（降级兜底）: %s", ns, exc)

    # ③ 兜底：当前命名空间（历史行为）——必须告警，否则又是一次"静默拿错库的列"
    from backend.services import iris_connector
    try:
        rows = iris_connector.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='SQLUser' AND table_name=? ORDER BY ordinal_position",
            [tbl])
        cols = [str(r[0]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("取目标表 %s 列清单失败: %s", tbl, exc)
        return []
    logger.warning("目标表 %s 的列清单按**当前命名空间**（%s）兜底取得：若目标库不是该命名空间，"
                   "列可能不匹配（跨库目标请登记 jdbc_url/table_columns）",
                   tbl, IRISConfig.NAMESPACE)
    return cols
