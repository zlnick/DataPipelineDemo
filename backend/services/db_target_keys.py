"""DB 目标主键事实解析（登记 → 现场探查 → 回写登记）。

背景（2026-09-17 Round 2 实测 P0 缺陷）：SQL 源（USER.SQLUser.Patient）→ DB 目标
（CLINIC.SQLUser.Patient）生成时，Agent A 把源主键 `ID` 映射到了目标 `MRN`，**目标表主键列
`ID` 从未被产出**；生成的 SQLOperation 是 `INSERT OR UPDATE INTO Patient(... ID ...) VALUES(*ID,...)`，
运行期 IRIS 判 `<PARAMETER>ID is required`（3 行源数据 → 9 条消息 Status=8 Error），
而生成接口在此之前一路 `code:0`（C1 只有 FHIR 必填检查、没有 DB 主键检查）。

本模块只做**事实解析**（不代写映射、不判语义）：
    目标模型传入的 key_columns → 已登记目标表记录的 key_columns → JDBC 元数据现场探查（并回写登记）
拿不到事实时返回 []（fail-open：不据此判错，交由运行期/其它检查暴露）。
AI 侧（Agent A 生成映射、C1 验证修复）拿到事实后自行决定用哪个源列或常量产出主键值。
"""

import logging

from backend.services import jdbc_client, repository

logger = logging.getLogger(__name__)

# DB 类型标记（含别名；与 pipeline_validator._TYPE_ALIASES 口径一致）
_DB_TOKENS = {"DB", "SQL", "SQLUSER"}


def _is_db(obj: dict) -> bool:
    """对象（目标/表记录/模型）是否属于 DB 类型（无类型标记时按 DB 处理，历史兼容）。"""
    for k in ("type", "target_type", "schema"):
        v = str(obj.get(k) or "").strip().upper()
        if v:
            return v in _DB_TOKENS
    return True


def _name_of(obj: dict) -> str:
    """取对象名（表/实体名）。"""
    return str(obj.get("table") or obj.get("entity_name") or obj.get("name") or "").strip()


def _keys_of(obj: dict) -> list[str]:
    """取对象上已声明的 key_columns（兼容 primary_keys/keys 字段名）。"""
    for k in ("key_columns", "primary_keys", "keys"):
        vals = obj.get(k)
        if isinstance(vals, list) and vals:
            return [str(v) for v in vals if str(v).strip()]
    return []


def key_columns_of(table: str, target_type: str = "DB", schema: str = "",
                   target_models: list[dict] | None = None,
                   persist: bool = True) -> list[str]:
    """解析目标表主键列（类型感知，跨类型同名不混用）。

    参数:
        table: 目标表名。
        target_type: 目标类型（DB/SQL 才解析；其它类型直接返回 []）。
        schema: 目标表 schema（探查用，可空则由登记记录提供）。
        target_models: 调用方传入的目标模型（可能已带 key_columns）。
        persist: 现场探查成功后是否回写登记记录（幂等；默认 True，便于后续复用）。

    返回:
        主键列名列表；无法确定时 []（fail-open）。
    """
    tbl = str(table or "").strip()
    tt = str(target_type or "").strip().upper()
    if not tbl or (tt and tt not in _DB_TOKENS):
        return []

    # 1. 调用方传入的目标模型（最贴近本次请求的事实）
    for tm in target_models or []:
        if not isinstance(tm, dict):
            continue
        if _name_of(tm).lower() != tbl.lower() or not _is_db(tm):
            continue
        keys = _keys_of(tm)
        if keys:
            return keys

    # 2. 已登记的目标表记录（含"上次现场探查"的结果）
    found: list[str] = []
    try:
        for tg in repository.list_targets() or []:
            if not _is_db(tg):
                continue
            for tb in tg.get("tables") or []:
                if not isinstance(tb, dict) or _name_of(tb).lower() != tbl.lower():
                    continue
                keys = _keys_of(tb)
                if keys:
                    return keys
                # 命中登记但缺主键事实 → 现场探查（结果回写登记）
                keys = _probe(tg, tb, tbl, schema)
                if keys:
                    if persist:
                        try:
                            repository.enrich_target_table(
                                str(tg.get("id") or ""), _name_of(tb),
                                {"key_columns": keys})
                        except Exception as exc:  # noqa: BLE001 - 回写失败不影响本次使用
                            logger.warning("回写目标表 key_columns 失败: %s", exc)
                    return keys
                found = []      # 探查不到 → 继续找其它同名登记（可能有可用连接）
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析目标表主键事实失败（跳过该检查）: %s", exc)
    return found


def _probe(tg: dict, tb: dict, table: str, schema: str) -> list[str]:
    """用 JDBC 元数据现场探查主键列（拿不到返回 []）。"""
    conn = dict(tg.get("connection") or {})
    if not conn.get("jdbc_url"):
        return []
    sch = str(schema or tb.get("schema") or "").strip()
    try:
        keys = jdbc_client.list_primary_keys(conn, sch, table)
    except Exception as exc:  # noqa: BLE001 - 驱动不支持/连接不可达 → 视为未知
        logger.info("JDBC 主键探查失败（%s.%s）: %s", sch, table, exc)
        return []
    if keys:
        logger.info("JDBC 主键探查成功: %s.%s key_columns=%s", sch, table, keys)
    return keys
