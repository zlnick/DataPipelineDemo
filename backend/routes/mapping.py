"""术语映射 API：术语服务器目录/盘点（术语服务器 = 术语转换映射的唯一事实源）+ C3 判定直连。

- `GET  /api/mapping/term-catalog`：只读目录 + **盘点摘要**（covered/negative/**待办 todo**/命名空间）
- `POST /api/mapping/cn2rx|cn2snomed`：单条判定直连（补录工具/调试用）

架构（2026-09-18 最终口径）：**运行期**由管道经**共享 BO** `demo.TerminologyOperation` 实时查术语
服务器（零本地码表副本）；生成期只做**只读盘点**，缺映射**默认放行**（进"待办清单"、运行期降级为
"保留源编码 + meta.tag=unmapped"），需要"先齐备才允许生成"时用 `strict_terms=true`。

术语服务器的生命周期与演示程序主体**独立**（独立容器 + 数据目录 `./data/iris-terminology`，
`tools/reset_ui_env.py` 不触及）⇒ 环境重置不影响术语映射。
"""

import logging

from flask import Blueprint, request

from backend.services.cn2rx_mapping import run_cn2rx_mapping
from backend.services.cn2snomed_mapping import run_cn2snomed
from backend.services.llm_client import AgentError
from backend.utils import error, success

logger = logging.getLogger(__name__)

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


@mapping_bp.get("/term-catalog")
def term_catalog_view():
    """术语映射目录与**盘点摘要**（**只读**：不写缓存、不改环境、不调 LLM，供 UI 展示）。

    返回::

        {"catalog": {"ok","pairs":[{sourceSystem,targetSystem,entries,active,negative}], "url"},
         "gate": {"jobs","namespaces","covered","negative","missing","todo","pending",
                  "unresolved","pairs","ok","error","skipped"},
         "hint": "缺映射时的补录方式（AI 判定写回服务器）",
         "runtime": "运行期口径说明（共享 BO 实时查询；缺映射默认降级）"}

    说明：术语服务器是术语转换映射的唯一事实源；盘点**只读**（读源表编码值 → 查服务器 availability）。
    运行期由共享 BO `demo.TerminologyOperation` 实时查询；缺映射**默认降级**（保留源编码 +
    `meta.tag=unmapped`），补录见 `python3 tools/term_map_build.py`（补录后无需重新生成）。
    """
    from backend.services import term_catalog, term_precheck

    catalog = term_catalog.systems(use_cache=False)
    try:
        gate = term_precheck.precheck()
    except Exception as exc:  # noqa: BLE001 - 读取失败也要给出可读原因（不假装"无任务"）
        logger.warning("术语盘点读取失败: %s", exc)
        gate = {"ok": False, "error": f"术语盘点读取失败: {exc}", "jobs": [], "skipped": ""}
    return success({
        "catalog": {"ok": bool(catalog.get("ok")), "url": catalog.get("url") or "",
                    "pairs": catalog.get("pairs") or [], "error": catalog.get("error") or ""},
        "gate": {"ok": bool(gate.get("ok")), "skipped": gate.get("skipped") or "",
                 "jobs": gate.get("jobs") or [], "namespaces": gate.get("namespaces") or {},
                 "covered": int(gate.get("covered") or 0),
                 "negative": int(gate.get("negative") or 0),
                 "missing": gate.get("missing") or [],
                 "todo": gate.get("todo") or [],
                 "pending": gate.get("pending") or [],
                 "unresolved": gate.get("unresolved") or [],
                 "pairs": gate.get("pairs") or [], "notes": gate.get("notes") or [],
                 "error": gate.get("error") or ""},
        "hint": term_precheck.hint(),
        "runtime": ("运行期由**共享 BO** demo.TerminologyOperation 实时查术语服务器："
                    "active→追加目标体系 coding（双 coding）；negative→不追加（服务器判定无匹配）；"
                    "missing/error→**默认降级**（保留源编码 + meta.tag=unmapped，不静默）"),
    })


