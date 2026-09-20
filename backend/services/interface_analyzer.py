"""接口分析 Agent（AI 语义层）：源/目标接口「分析结论」的 LLM 生成。

分工（遵守 AI 驱动红线）：
- 确定性工具（CapabilityStatement 拉取 / information_schema 列 / WSDL 实体解析 /
  connection_profiler 连通与能力探测）只负责**精确事实采集**——事实不能由 LLM 编造；
- 本模块负责把事实**升维成 AI 分析结论**：资产/实体语义、字段与主键/轮询键建议、
  写读方向与适配提示、运行契约（runtime）解读与建议；
- LLM 失败 = 分析失败（抛 AgentError 由路由返回明确错误），绝不静默退回规则文案。

产物落位：
- 源资产：description / key_hint（建议轮询键）
- 目标（表/实体）：description / direction（write|read）建议
- 运行契约：runtime["note"]["ai"]（自然语言解读 + suggestions）
"""

import copy
import json
import logging
import re

from backend.services.llm_client import AgentError, _call_llm

logger = logging.getLogger(__name__)


# ===== 系统提示词：源资产分析 =====
SYSTEM_PROMPT_SOURCE = (
    "你是医疗数据集成分析师。给定接口探测到的结构事实（FHIR 资源类型 + 样例字段，或 SQL 表 + 列名），"
    "为每个资产输出 AI 分析结论。要求：1. 每个资产必须输出语义说明（该资源/表业务含义与主要信息内容）；"
    "2. 给出主键/业务键建议 key_hint（FHIR 资源建议 identifier/search 路径；SQL 表建议能作为轮询增量的键列）；"
    "3. 若能从列名与语义识别出携带编码/术语的字段（诊断码、药品码、CodeSystem URI 列、名称显示列等），"
    "输出字段级术语结构化结论 field_terms：{字段名: {kind, note}}，kind 取值 "
    "coded_value(编码列，如国标ICD-10码/药品码) | term_uri(CodeSystem URI 列) | display_name(术语显示名) | plain(普通)；"
    "仅当能可靠判断时输出，不要臆造；"
    "4. 只依据给定事实，不臆造不存在的字段；输出保持中文。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"items":[{"name":"Diagnosis","semantics":"就诊诊断表：国标ICD-10诊断及术语来源",'
    '"key_hint":"ID","comment":"适合作为管道源资产",'
    '"field_terms":{"Code":{"kind":"coded_value","note":"国标ICD-10诊断码"},"Name":{"kind":"display_name","note":"中文诊断名"},'
    '"CodeSystem":{"kind":"term_uri","note":"每行术语 URI"}}}]}'
)


# ===== 系统提示词：目标接口分析 =====
SYSTEM_PROMPT_TARGET = (
    "你是数据目标接口分析师。给定目标结构事实（SQL 目标表 + 列名；SOAP WSDL 实体的实体名与属性；"
    "FHIR 资源类型 + 关键元素路径与结构约束），为每个目标输出 AI 分析结论。要求："
    "1. 每个目标必须输出语义说明与字段要点；"
    "2. 给出 direction 判定：实体/表是否适合作为写入型管道目标（write）或仅查询型（read），"
    "并给一句理由 reason；FHIR 资源（REST PUT/POST 事务写入）属 write；"
    "3. 只依据给定事实，不臆造字段；输出保持中文。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"items":[{"name":"AddPatient","semantics":"第三方 HIS 患者新增写入接口（扁平三字段）",'
    '"direction":"write","reason":"请求消息为写入型 AddPatient，参数为患者标识/姓名/性别"}]}'
)


# ===== 系统提示词：运行契约解读 =====
SYSTEM_PROMPT_CONTRACT = (
    "你是数据管道运行契约顾问。给定源或目标的运行契约（探查结果：能力/增量/投递/健康/语义），"
    "用自然语言输出该连接对构建数据管道的要点解读与建议。要求：1. 输出 text（≤120 字要点）与 "
    "suggestions（≤3 条、每条 ≤40 字的可行建议，如增量同步、轮询键、目标适配等）；"
    "2. 只解读给定事实，不虚构能力；输出保持中文。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"text":"FHIR 端点支持 _lastUpdated 增量查询，适合定时增量同步","suggestions":["用 _lastUpdated 游标做增量","分批拉取避免超时"]}'
)


def _mask(d: dict | None) -> dict | None:
    """运行契约脱敏：删除 password/secret（不给 LLM 明文凭据）。"""
    if not isinstance(d, dict):
        return d
    out = copy.deepcopy(d)
    for k, v in list(out.items()):
        if isinstance(v, dict):
            out[k] = _mask(v)
        elif k.lower() in ("password", "secret", "token", "apikey", "api_key"):
            out[k] = "***"
    return out


def _ok_items(data: dict, key: str = "items") -> list[dict]:
    items = data.get(key)
    return items if isinstance(items, list) else []


# ===== 源资产：语义/键建议 =====
def analyze_source_assets(source_type: str, assets: list[dict],
                          facts: dict | None = None,
                          agent: str = "接口分析Agent(源)") -> list[dict]:
    """对源资产做 AI 分析（一次调用整批）。失败抛 AgentError（不静默）。

    参数:
        assets: [{"name","fields","structure","source_type","kind"}] 结构事实
        facts:  探测事实摘要（capability / 表来源 / schema 等），仅作上下文
    返回:
        [{"name","semantics","key_hint","comment"}]，仅含 LLM 确实覆盖的资产
    """
    if not assets:
        return []
    user_content = json.dumps({
        "source_type": source_type,
        "facts": _mask(facts or {}),
        "assets": assets,
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_SOURCE, user_content, agent)
    return _ok_items(result)


# ===== 目标接口：语义/direction =====
def analyze_target_interfaces(target_type: str, tables: list[dict],
                              facts: dict | None = None,
                              agent: str = "接口分析Agent(目标)") -> list[dict]:
    """对目标表/实体做 AI 分析。失败抛 AgentError。

    参数:
        tables: [{"name"|"table"|"entity_name","columns","fields","schema"}] 结构事实
        facts:  探测事实（SOAP MessageMap 操作、写读判定原始等）
    返回:
        [{"name","semantics","direction","reason"}]
    """
    if not tables:
        return []
    rows = []
    for t in tables:
        name = t.get("entity_name") or t.get("table") or t.get("name") or ""
        cols = t.get("columns") or t.get("fields") or []
        if not name:
            continue
        rows.append({"name": name,
                     "columns": [c.get("name") if isinstance(c, dict) else c for c in cols],
                     "schema": t.get("schema", "")})
    if not rows:
        return []
    user_content = json.dumps({
        "target_type": target_type,
        "facts": _mask(facts or {}),
        "targets": rows,
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_TARGET, user_content, agent)
    return _ok_items(result)


# ===== 系统提示词：无数据时按 FHIR R4 规范补字段 =====
SYSTEM_PROMPT_FHIR_FIELDS = (
    "你是 FHIR R4 规范专家。给定若干 FHIR 资源类型（这些类型在数据源里**暂时没有样例数据**），"
    "按 **FHIR R4 规范**（必要时结合 US Core 约束）给出每个类型的**常用可映射元素路径**。要求："
    "1. 只输出 R4 规范定义的元素，**不要发明扩展或自定义元素**；"
    "2. 路径用 `.` 连接，多值元素用 `[0]`（例：identifier[0]、name[0].family、telecom[0].value、"
    "code.coding[0].code、subject）；"
    "3. 每个类型最多 30 个路径，优先**可映射业务元素**：标识/编号、名称、状态、编码（code/coding）、"
    "日期时间、引用（subject/patient/encounter/performer）、数量/单位、性别、地址等；"
    "4. 不要输出纯结构元素（text、contained、extension、meta、modifierExtension、id 除外）；"
    "5. 只依据 R4 规范，不臆造；输出保持中文注释。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"items":[{"name":"Account","fields":["identifier[0]","status","type[0].coding[0].code",'
    '"subject","servicePeriod.start"],"note":"账号：标识/状态/类型/主体引用/服务期"}]}'
)

# AI 规范字段补全的类型上限（单次调用；145 类全量会让请求过大且无必要）
FHIR_AI_FIELDS_MAX_TYPES = 20
# 单类型字段数上限（防 LLM 输出过长）
FHIR_AI_FIELDS_MAX_PER_TYPE = 40
# 合法路径形态：段用 . 连接，段可带 [n] 下标（与转换引擎/AI 映射的访问路径一致）
_FIELD_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\[\d+\])?(\.[A-Za-z][A-Za-z0-9_]*(\[\d+\])?)*$")


def _clean_field_paths(raw) -> list[str]:
    """清洗 LLM 给出的字段路径：只保留合法形态、去空去重、保序、限量。

    只做**首尾空白**清理（内部空格/怪异字符 = 非法形态，直接丢弃，不"修好"）。
    """
    out: list[str] = []
    for f in (raw or []):
        s = str(f).strip().lstrip(".")
        if not s or not _FIELD_PATH_RE.match(s):
            continue
        if s.startswith(("meta", "text", "contained", "extension", "modifierExtension")):
            continue
        if s not in out:
            out.append(s)
        if len(out) >= FHIR_AI_FIELDS_MAX_PER_TYPE:
            break
    return out


def complete_fhir_resource_fields(resource_types: list[str], facts: dict | None = None,
                                  max_types: int = FHIR_AI_FIELDS_MAX_TYPES,
                                  agent: str = "接口分析Agent(FHIR规范字段)") -> dict:
    """**无样例数据**时按 FHIR R4 规范补该资源类型的可映射字段路径（一次 LLM 调用）。

    为什么需要：FHIR 是标准，字段不该"靠库里有数据才知道"。已建模的 11 类由平台规范快照兜底
    （`connection_profiler` 的 `spec_model`），其余类型由本 Agent 依 R4 规范给出，并在运行契约里
    标注 `provenance=ai_spec`（可审计；绝不静默退回规则文案）。

    参数:
        resource_types: 待补的资源类型名（调用方负责限量，超出 max_types 的会被截断）
        facts: 探测事实摘要（fhir_version / endpoint 等），仅作上下文
    返回:
        {类型: {"fields": [...], "note": "..."}}；输出不合规（缺 items / 全部无法解析）抛 AgentError
    """
    types = [str(t).strip() for t in (resource_types or []) if str(t).strip()]
    if not types:
        return {}
    if max_types and max_types > 0:
        types = types[:max_types]
    user_content = json.dumps({
        "requested_types": types,
        "facts": _mask(facts or {}),
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_FHIR_FIELDS, user_content, agent)
    items = _ok_items(result)
    if not items:
        raise AgentError("FHIR 规范字段补全输出不合规（缺 items）")
    _want = {t.lower() for t in types}          # 只接受**请求过**的类型（LLM 多给的忽略）
    out: dict = {}
    for it in items:
        name = str(it.get("name") or "").strip()
        if not name or name.lower() not in _want:
            continue
        fields = _clean_field_paths(it.get("fields"))
        if not fields:
            continue
        out[name] = {"fields": fields, "note": str(it.get("note") or "")[:200]}
    if not out:
        raise AgentError("FHIR 规范字段补全输出不合规（各类型 fields 均无法解析为合法路径）")
    return out


# ===== 运行契约解读 =====
def interpret_contract(kind: str, runtime: dict | None,
                       agent: str = "接口分析Agent(契约)") -> dict:
    """把确定性探测的运行契约解读为自然语言要点与建议。失败抛 AgentError。

    参数:
        kind: "source" | "target"
        runtime: 契约 dict（connection/capabilities/health/note…）
    返回:
        {"text": "...", "suggestions": [...]}
    """
    if not isinstance(runtime, dict):
        raise AgentError("缺少运行契约（runtime）")
    user_content = json.dumps({
        "role": kind,
        "runtime": _mask(runtime),
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_CONTRACT, user_content, agent)
    text = result.get("text")
    suggestions = result.get("suggestions")
    if not text or not isinstance(suggestions, list):
        raise AgentError("契约解读输出不合规（缺 text/suggestions）")
    return {"text": str(text), "suggestions": [str(s)[:60] for s in suggestions]}
