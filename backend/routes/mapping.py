"""术语映射判定 API（C3 Mapping Agent）：中文药品名 → RxNorm。"""

from flask import Blueprint, request

from backend.services.cn2rx_mapping import run_cn2rx_mapping
from backend.services.cn2snomed_mapping import run_cn2snomed
from backend.services.llm_client import AgentError
from backend.utils import error, success

mapping_bp = Blueprint("mapping", __name__, url_prefix="/api/mapping")


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
