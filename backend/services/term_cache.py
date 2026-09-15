# -*- coding: utf-8 -*-
"""术语判码缓存服务（运行期 Skill 执行层的“预判定”缓存）。

分工（遵守 AI 驱动红线）：
- 决策：Agent A 在 field_mapping 输出 term_map:<skill>（已是受控指令）；
- 判码：由判码 Skill 执行——这里**复用** cn2snomed / cn2rx 服务（词表召回 + LLM 判定），
  对源表里实际存在的编码值逐条判定一次；
- 缓存：结果写 ^demo.Config("termcache", <skill>, <源编码>) = {"code","display"}，
  IRIS 侧 FHIR 打包时按此追加目标系统 coding（双 coding），不做码决策、不写死规则。

触发：POST /api/mapping/term-cache（手动刷新），管道生成成功后自动调用。
"""
import json
import logging
import time

import iris.dbapi

from backend.services import iris_connector

from backend.services import cn2rx_mapping, cn2snomed_mapping
from backend.services.llm_client import AgentError

logger = logging.getLogger(__name__)

CLINIC_HOST = "iris"
IRIS_PORT = 1972
CLINIC_NS = "CLINIC"

# skill → 表内“编码列 / 显示名列”
CLINIC_CODE_COLS = {
    "Diagnosis": ("Code", "Name"),
    "MedicationOrder": ("MedicationCode", "MedicationName"),
    "Encounter": ("ReasonCode", "ReasonText"),
}
# skill → 目标 coding 取值字段（判码结果键）
SKILL_RESULT = {
    "cn2snomed": {"code": "code", "display": "display"},
    "cn2rx": {"code": "rxcui", "display": "enName"},
}


def _iter_demo_mappings() -> list[dict]:
    """读取 ^demo.Mapping 全部映射（连接用后即关：IRIS 许可按连接计数）。"""
    import iris

    from backend.services import iris_connector
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        out = []
        for _, raw in native.iterator("^demo.Mapping"):
            if raw:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
        return out
    finally:
        _close(conn)


def _close(conn) -> None:
    """显式关闭连接（立即释放许可单元）；关闭失败不抛错。"""
    try:
        if conn is not None:
            conn.close()
    except Exception:  # noqa: BLE001 - 连接可能已失效
        pass


def _open_clinic():
    """打开 CLINIC 命名空间连接（读源表编码列）。调用方必须 `_close`。"""
    return iris.dbapi.connect(hostname=CLINIC_HOST, port=IRIS_PORT, namespace=CLINIC_NS,
                              username="superuser", password="SYS")


def _distinct_codes(conn, table: str) -> list[tuple[str, str]]:
    """用**已打开的** CLINIC 连接读 DISTINCT (编码列, 显示名列) → [(code, name)]。

    复用同一连接（不再每张表新开）：IRIS 社区版许可按连接计数，多开连接会与 Ens 业务主机
    抢许可单元——这正是"判码缓存刷新失败：Unable to allocate a license"的成因之一。
    """
    col_info = CLINIC_CODE_COLS.get(table)
    if not col_info:
        return []
    code_col, name_col = col_info
    cur = conn.cursor()
    cur.execute(f"SELECT DISTINCT {code_col}, {name_col} FROM SQLUser.{table} "
                f"WHERE {code_col} IS NOT NULL")
    rows = [(str(r[0]), str(r[1] or "")) for r in cur.fetchall() if r[0]]
    try:
        cur.close()
    except Exception:  # noqa: BLE001 - 关闭游标失败可忽略
        pass
    return rows


def _call_skill(skill: str, query: str, context: str = "") -> dict:
    """调用判码 Skill（复用服务层）。失败抛 AgentError（不静默）。"""
    if skill == "cn2snomed":
        return cn2snomed_mapping.run_cn2snomed(query, context)
    if skill == "cn2rx":
        return cn2rx_mapping.run_cn2rx_mapping(query, context)
    raise AgentError(f"未注册判码 Skill: {skill}")


def _jobs_from_mappings(mappings: list[dict] | None) -> dict[tuple[str, str], None]:
    """从映射里提取需要判码的 (源表, skill) 任务（受控指令 term_map:<skill> + 已登记源表）。"""
    jobs: dict[tuple[str, str], None] = {}
    for m in mappings or []:
        for fm in (m.get("field_mappings") or []):
            tr = str(fm.get("transform") or "")
            if not tr.startswith("term_map:"):
                continue
            skill = tr[len("term_map:"):].strip()
            if skill not in SKILL_RESULT:
                continue
            src = str(fm.get("source") or "")
            if "." not in src:
                continue
            table = src.split(".", 1)[0]
            if table not in CLINIC_CODE_COLS:
                continue
            jobs[(table, skill)] = None
    return jobs


def _read_codes_by_table(jobs: dict[tuple[str, str], None]) -> dict[str, list[tuple[str, str]]]:
    """按 job 集合读取源表编码值（单连接，读完即关）。"""
    out: dict[str, list[tuple[str, str]]] = {}
    conn = _open_clinic()
    try:
        for table in sorted({t for t, _ in jobs}):
            out[table] = _distinct_codes(conn, table)
    finally:
        _close(conn)
    return out


def term_cache_status(mappings: list[dict] | None = None) -> dict:
    """审计 term_map 决策的缓存齐备性（供生成响应/UI 显式提示，避免 AI 决策静默失效）。

    已判定即算就绪：有映射（`{"code":...}`）与**负缓存**（`{"code":"","match":false}`，
    Skill 判定"池内无对应"）都算已判定；只有节点不存在才算 missing。

    参数:
        mappings: 映射列表（无则读 ^demo.Mapping）。
    返回:
        {"required": 需要的源编码条数, "cached": 有映射条数, "negative": 负缓存条数,
         "missing": ["<skill>:<源编码>", ...], "ok": bool}
    """
    import iris

    jobs = _jobs_from_mappings(mappings)
    if not jobs:
        return {"required": 0, "cached": 0, "negative": 0, "missing": [], "ok": True}
    codes_by_table = _read_codes_by_table(jobs)
    required = 0
    cached = 0
    negative = 0
    missing: list[str] = []
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        for (table, skill) in jobs:
            for code, _name in codes_by_table.get(table, []):
                required += 1
                raw = native.get("^demo.Config", "termcache", skill, code)
                if not raw:
                    missing.append(f"{skill}:{code}")
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    obj = {}
                if obj.get("code"):
                    cached += 1
                elif obj.get("match") is False:
                    negative += 1
                else:
                    missing.append(f"{skill}:{code}")
    finally:
        _close(conn)
    return {"required": required, "cached": cached, "negative": negative,
            "missing": missing, "ok": not missing}


def _is_license_error(exc) -> bool:
    """判断异常是否为「许可单元不足」（IRIS 报 Unable to allocate a license）。"""
    text = str(exc).lower()
    return "license" in text or "许可" in text


def _resume_items(paused: list[str], before_enabled: set[str]) -> list[str]:
    """恢复被临时暂停的源 BS（只恢复原本处于启用状态的组件）。"""
    from backend.services import pipeline_validator

    if not paused:
        return []
    targets = [n for n in paused if not before_enabled or n in before_enabled]
    if not targets:
        return []
    res = pipeline_validator.set_items_enabled(targets, True)
    if not res.get("ok"):
        logger.warning("恢复临时暂停的源 BS 失败: %s", res.get("message") or res.get("result"))
        return []
    logger.info("判码缓存刷新后已恢复源 BS: %s", targets)
    return targets


def _judged_keys(jobs: dict[tuple[str, str], None]) -> set[tuple[str, str]]:
    """已判定过的 (skill, 源编码) 集合（含「判定无映射」的负缓存）——避免重复烧 LLM。"""
    out: set[tuple[str, str]] = set()
    if not jobs:
        return out
    import iris

    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        for (_table, skill) in jobs:
            sub = native.nextSubscript(False, "^demo.Config", "termcache", skill, "")
            while sub:
                out.add((skill, str(sub)))
                sub = native.nextSubscript(False, "^demo.Config", "termcache", skill, sub)
    finally:
        _close(conn)
    return out


def build_term_cache(mappings: list[dict] | None = None, *, force: bool = False) -> dict:
    """按 mappings 中 term_map 指令，对源表存在的编码值调用判码 Skill 并写入缓存。

    缓存三种形态（供审计与运行期区分）：
      - `{"code":"44054006","display":"..."}`：判定有映射 → 运行期追加该系统 coding（双 coding）
      - `{"code":"","match":false}`：**负缓存**——Skill 判定"池内无对应"，运行期不追加（不是失败）
      - 节点不存在：尚未判定 → 审计报 missing（显式提示，不静默）

    连接纪律（重要：社区版仅 8 个许可单元，与 Ens 业务主机共用）：
    **读源码 1 个 CLINIC 连接 → 判码（不占连接）→ 写缓存 1 个 USER 连接**，每步用完即关。

    参数:
        mappings: 显式映射列表（无则读 ^demo.Mapping）。
        force: True 时忽略已有缓存重新判定（默认跳过已判定项，省 LLM 调用）。
    返回:
        {"skills": {...skill: {attempted, cached, negative, skipped}},
         "total_cached": 本轮新增「判出目标码」条数, "negative": 本轮新增「判定无映射」条数,
         "cached_total": 缓存内累计命中条数, "negative_total": 缓存内累计负缓存条数,
         "required": 需要的源编码条数, "missing": ["<skill>:<源编码>", ...]}

    ⚠ 口径说明：`total_cached` / `negative` 只统计**本轮**动作；`cached_total` / `negative_total`
    与 `required` / `missing` 同为**全量**口径。两套口径混用会让"缓存明明有值却显示 0"（已踩），
    故必须成对出现、语义写清。
    """
    import iris

    if mappings is None:
        mappings = _iter_demo_mappings()
    jobs = _jobs_from_mappings(mappings)
    if not jobs:
        return {"skills": {}, "total_cached": 0, "negative": 0, "cached_total": 0,
                "negative_total": 0, "required": 0, "missing": [],
                "skipped": "无 term_map 指令"}

    # ① 读源编码（单连接，读完即关）
    codes_by_table = _read_codes_by_table(jobs)
    judged = set() if force else _judged_keys(jobs)

    # ② 判码（LLM 决策；此阶段不占 IRIS 连接）
    pending: list[tuple[str, str, dict]] = []      # (skill, 源码, 缓存 payload)
    counts: dict[str, dict[str, int]] = {}
    for (table, skill) in jobs:
        attempted = cached = negative = skipped = 0
        for code, name in codes_by_table.get(table, []):
            if (skill, code) in judged:
                skipped += 1
                continue
            query = name or code
            if not query:
                continue
            attempted += 1
            try:
                verdict = _call_skill(skill, query, context=f"源表 {table}")
            except AgentError as exc:
                logger.warning("判码 Skill 调用失败 %s/%s: %s", skill, code, exc)
                continue
            keys = SKILL_RESULT[skill]
            target_code = verdict.get(keys["code"]) if verdict.get("match") else ""
            if target_code:
                pending.append((skill, code,
                                {"code": str(target_code),
                                 "display": str(verdict.get(keys["display"]) or "")}))
                cached += 1
            else:
                # 负缓存：Skill 判定"无映射"（或未给目标码）——记录事实，避免下次重复判定
                pending.append((skill, code, {"code": "", "match": False,
                                              "reason": str(verdict.get("reason") or "")[:200]}))
                negative += 1
        counts[skill] = {"attempted": attempted, "cached": cached,
                         "negative": negative, "skipped": skipped}

    # ③ 写缓存（单连接，写完即关）
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        for skill, code, payload in pending:
            native.set(json.dumps(payload, ensure_ascii=False),
                       "^demo.Config", "termcache", skill, code)
    finally:
        _close(conn)

    total = sum(1 for _s, _c, p in pending if p.get("code"))
    negatives = len(pending) - total
    logger.info("判码缓存刷新完成：%s（本轮命中 %d / 本轮负缓存 %d）", counts, total, negatives)
    status = term_cache_status(mappings)
    return {"skills": counts, "total_cached": total, "negative": negatives,
            "cached_total": int(status.get("cached") or 0),
            "negative_total": int(status.get("negative") or 0),
            "required": status.get("required", 0),
            "missing": status.get("missing") or []}



def build_term_cache_safe(mappings: list[dict] | None = None, *,
                          pause_items: list[str] | None = None,
                          attempts: int = 3, force: bool = False) -> dict:
    """带「许可腾挪」的判码缓存刷新；失败**显式返回**（不静默降级 AI 的 term_map 决策）。

    为什么需要：刷新要在后端连接之外再开连接，而 IRIS 社区版共 8 个许可单元、每个 Ens
    业务主机常驻 1 个 → 管道启用后（实测 6 业务主机 + JavaGateway + 后端 = 8/8）刷新报
    `Unable to allocate a license`，导致 Agent A 的 `term_map:cn2snomed/cn2rx` 决策在运行期
    悄悄失效（FHIR 落地的 Condition/MedicationRequest 缺目标系统 coding）。这里按候选顺序
    **临时停用源 BS 腾出单元**，刷新完立即恢复（源 BS 停几秒不丢数据：last key 未前进的行
    下轮仍会重发）。

    参数:
        mappings: 映射列表（无则读 ^demo.Mapping）。
        pause_items: 可按需暂停的业务主机名（建议：本管道源 BS + 其它已登记管道源 BS）。
        attempts: 最大尝试次数（每次多暂停一个候选，逐步腾单元）。
        force: True 时忽略已有缓存重新判定全部编码（默认跳过已判定项，省 LLM 调用）。
    返回:
        {"ok", "cache", "paused_items", "restored_items", "attempts", "error",
         "required", "cached", "negative", "cached_total", "negative_total", "missing"}
        - `cached` / `negative` = **本轮新增**命中/负缓存；`cached_total` / `negative_total` = 缓存累计；
        - ok=False 时调用方必须显式告知用户（AI 决策未能落地），不得当作成功。
    """
    from backend.services import pipeline_validator

    candidates = [str(n).strip() for n in (pause_items or []) if str(n).strip()]
    paused: list[str] = []
    before_enabled: set[str] = set()
    last_err = ""
    tries = max(1, attempts)
    for i in range(tries):
        try:
            cache = build_term_cache(mappings, force=force)
            restored = _resume_items(paused, before_enabled)
            if i or paused:
                logger.info("判码缓存刷新成功（第 %d 次尝试，暂停过 %s）", i + 1, paused)
            return {"ok": True, "cache": cache, "paused_items": paused,
                    "restored_items": restored, "attempts": i + 1, "error": "",
                    "required": int(cache.get("required") or 0),
                    "cached": int(cache.get("total_cached") or 0),
                    "negative": int(cache.get("negative") or 0),
                    "cached_total": int(cache.get("cached_total") or 0),
                    "negative_total": int(cache.get("negative_total") or 0),
                    "missing": list(cache.get("missing") or [])}
        except Exception as exc:  # noqa: BLE001 - 连接/许可异常都要显式报告
            last_err = str(exc)
            if i == tries - 1 or not _is_license_error(exc):
                break
            nxt = [n for n in candidates if n not in paused]
            if not nxt:
                break
            if not before_enabled:
                before_enabled = {str(it.get("name"))
                                  for it in pipeline_validator.production_items()
                                  if int(it.get("enabled") or 0) == 1}
            logger.warning("判码缓存刷新遇许可不足 → 暂停源 BS 腾单元: %s", nxt[:1])
            res = pipeline_validator.set_items_enabled(nxt[:1], False)
            if not res.get("ok"):
                last_err = (f"{last_err}；暂停 {nxt[:1]} 失败: "
                            f"{res.get('message') or res.get('result')}")
                break
            paused.extend(nxt[:1])
            time.sleep(1.5)
    restored = _resume_items(paused, before_enabled)
    return {"ok": False, "cache": {}, "paused_items": paused,
            "restored_items": restored, "attempts": tries, "error": last_err,
            "required": 0, "cached": 0, "negative": 0, "cached_total": 0,
            "negative_total": 0, "missing": []}

