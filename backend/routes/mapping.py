"""术语映射判定 API（C3 Mapping Agent）：中文药品名 → RxNorm。"""

from flask import Blueprint, request

from backend.services import pipeline_instances
from backend.services.cn2rx_mapping import run_cn2rx_mapping
from backend.services.cn2snomed_mapping import run_cn2snomed
from backend.services.llm_client import AgentError
from backend.services.term_cache import build_term_cache_safe
from backend.utils import error, success

mapping_bp = Blueprint("mapping", __name__, url_prefix="/api/mapping")


def _pause_candidates() -> list[str]:
    """可按需暂停的源 BS（本平台已登记管道的源 BS；每暂停 1 个腾出 1 个许可单元）。"""
    names: list[str] = []
    for rec in pipeline_instances.list_instances():
        routes = rec.get("routes") or {}
        cand = list(routes.get("source_bs_names") or [])
        if routes.get("source_bs"):
            cand.append(routes["source_bs"])
        for n in cand:
            if n and n not in names:
                names.append(n)
    return names


@mapping_bp.post("/cn2rx")
def cn2rx():
    """中文药品名 → RxNorm 映射判定（多路召回 + LLM 判定，C3 Mapping Agent）。

    Body: {"query": "阿基仑赛注射液", "context": "商保目录，CAR-T 细胞疗法"}
    返回: 判定结果（match/rxcui/tty/enName/reason/confidence/alias_based）+ 召回证据。
    LLM 失败或术语服务不可达时显式返回错误（不静默回退规则）。
    """
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return error("参数 query（中文药名）必填")
    try:
        result = run_cn2rx_mapping(query, body.get("context"))
    except AgentError as exc:
        return error(f"[MappingAgent] {exc}")
    return success(result)


@mapping_bp.post("/cn2snomed")
def cn2snomed():
    """中文诊断（国标 ICD-10）→ SNOMED CT 映射判定（C3-Dx：词表先决 + 向量 Top-N + LLM）。

    Body: {"query": "原发性高血压", "context": "门诊诊断"}
    返回: match/code/display/reason/confidence/alias_based + 召回证据。
    LLM 失败或术语服务不可达显式报错。
    """
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return error("参数 query（中文诊断名）必填")
    try:
        result = run_cn2snomed(query, body.get("context"))
    except AgentError as exc:
        return error(f"[MappingAgent-Dx] {exc}")
    return success(result)


@mapping_bp.post("/term-cache")
def refresh_term_cache():
    """按 ^demo.Mapping 中 term_map 指令，调用判码 Skill 预判定并写缓存（运行期双 coding 用）。

    许可不足（社区版 8 个单元被 Ens 业务主机占满）时，自动**临时暂停源 BS** 腾出单元后再
    刷新，刷完恢复；仍失败则显式返回 `ok=false` 与原因——`term_map` 是 AI 决策，不得静默失效。
    """
    body = request.get_json(silent=True) or {}
    try:
        report = build_term_cache_safe(body.get("mappings"),
                                       pause_items=_pause_candidates(),
                                       force=bool(body.get("force")))
    except AgentError as exc:
        return error(f"[判码缓存] {exc}")
    if not report.get("ok"):
        return error(f"[判码缓存] 刷新失败（term_map 决策将无法在运行期落地）: {report.get('error')}",
                     data=report)
    payload = dict(report.get("cache") or {})
    payload["paused_items"] = report.get("paused_items") or []
    payload["restored_items"] = report.get("restored_items") or []
    # 口径写清（曾因两套口径混用出现"缓存明明有值却显示 cached=0"）：
    #   cached / negative   = 缓存**累计**（与 required / missing 同口径）
    #   new_cached / new_negative = **本次**新判定条目（全已判定时为 0，属正常）
    payload["cached"] = report.get("cached_total")
    payload["negative"] = report.get("negative_total")
    payload["new_cached"] = report.get("cached")
    payload["new_negative"] = report.get("negative")
    payload["missing"] = report.get("missing") or []
    payload["note"] = ("cached/negative=缓存累计；new_cached/new_negative=本次新判定；"
                       "missing=尚未判定（运行期会缺目标系统 coding）")
    return success(payload, "判码缓存已刷新（判定结果来自 cn2rx/cn2snomed Skill）")
