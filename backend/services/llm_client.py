"""OpenAI 兼容 LLM 客户端：两个 AI Agent 的服务封装。"""

import json
import logging

from openai import OpenAI

from backend.config import LLMConfig

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """Agent 调用失败异常。"""


# ===== Agent A：数据转换（系统提示词） =====
SYSTEM_PROMPT_TRANSFORMATION = (
    "你是医疗数据集成专家。给定源数据资产（HL7 FHIR 资源或 SQL 数据库表及其字段）"
    "与目标接口模型，为源资产集合生成 Transformation Plan；目标可以是数据库 SQL 表或 SOAP WSDL 实体，"
    "不能假设源表与目标实体一一对应，需根据字段、关系和接口结构决定一对一、多对一或一对多。"
    "要求：1. 基于字段语义、主键/关联字段和目标实体结构匹配，生成转换映射策略。"
    "2. 当多张 SQL 源表映射到同一个目标实体（如主从表或多表拼接）时，需以主源表名或代表资产名作为 asset/source，"
    "在 field_mappings 的 source 中使用 '表名.字段名' 或表达式，target 使用目标实体的字段路径。"
    "3. 日期字段建议加 transform:date（提取 YYYY-MM-DD），拼接字段可写 concat 表达式。"
    "4. 必须同时输出 recommendations 数组和 transformation_plan.mappings 数组！"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"recommendations":[{"asset":"PatientTable","target_table":"PatientEntity","confidence":0.90,'
    '"reason":"根据 ID 和姓名字段精准匹配","field_mappings":[{"source":"PatientTable.ID","target":"PatientNo","transform":null},'
    '{"source":"PatientTable.GivenName","target":"FullName","transform":null}]}],'
    '"transformation_plan":{"source_models":[],"target_models":[],"mappings":[{"source":"PatientTable","target":"PatientEntity","field_mappings":[]}],"status":"draft"}}'
)


# ===== Agent B：数据管道（系统提示词） =====
SYSTEM_PROMPT_PIPELINE = (
    "你是 IRIS 互操作性架构师。给定已确认的转换关系（源资产→目标表）、源/目标类型、运行契约与组件枚举，"
    "你负责**生成数据管道 Production 的完整组件拓扑**（组件构成与顺序由你决定，系统只做参数补全与完整性校验）。"
    "要求：1. 组件 type 必须从 available_components 枚举中选择（不要自创组件类型），"
    "并为每个组件给出合适的 name（SQLOperation 建议命名 SQLOp_<表名>，SOAPOperation 建议 SOAPOp_<service>）。"
    "2. 组件必须**完整**：涵盖该源类型所需的全部服务组件（例如 FHIR 源需同时包含 FHIRSyncService 与 "
    "FHIRService 两个服务；SQL 源为 SQLService）、TransformProcess、目标操作组件"
    "（DB 目标：为每个 mapping 的 target_table 各生成一个 SQLOperation 并在其 table 字段填表名；"
    "SOAP 目标：一个 SOAPOperation）、以及 JavaGateway。"
    "3. 数据流顺序：service（抓取）→service（处理）→process（转换）→operation（写入）→gateway。"
    "4. SQLOperation 组件的 table 填目标表名；不要输出 SQL、字段映射或代码。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"pipeline":{"components":[{"type":"FHIRSyncService","name":"FHIRSyncService"},'
    '{"type":"FHIRService","name":"FHIRService"},'
    '{"type":"TransformProcess","name":"TransformProcess"},'
    '{"type":"SQLOperation","name":"SQLOp_patient","table":"Patient"},'
    '{"type":"JavaGateway","name":"EnsLib.JavaGateway.Service"}]}}'
)


# ===== 公共调用逻辑（token 日志 + 一次重试） =====
def _call_llm(system_prompt: str, user_content: str, agent_name: str) -> dict:
    """调用 LLM 一次并解析 JSON 返回。"""
    if not LLMConfig.API_KEY or LLMConfig.API_KEY.startswith("sk-xxxx"):
        raise AgentError("未配置 LLM_API_KEY，请在 .env 中设置（OpenAI 兼容服务）")
    client = OpenAI(base_url=LLMConfig.BASE_URL, api_key=LLMConfig.API_KEY, timeout=180.0)
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=LLMConfig.MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                logger.info("[%s] token 用量: prompt=%s completion=%s total=%s",
                            agent_name, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
            text = resp.choices[0].message.content or ""
            return json.loads(text)
        except json.JSONDecodeError as exc:
            if attempt == 0:
                logger.warning("[%s] LLM 返回非 JSON，重试一次: %s", agent_name, exc)
                continue
            raise AgentError(f"LLM 返回内容解析失败: {exc}") from exc
        except Exception as exc:
            if attempt == 0:
                logger.warning("[%s] LLM 调用失败，重试一次: %s", agent_name, exc)
                continue
            raise AgentError(f"AI 调用失败: {exc}") from exc
    raise AgentError("AI 调用失败")


# ===== 知识库润色（ValidationIssue → Obsidian 知识） =====
SYSTEM_PROMPT_POLISH = (
    "你是医疗 IT 数据集成知识库整理编辑。给定验证-修复 Agent 沉淀的问题经验列表"
    "（同一故障可能被重复记录多次），请："
    "1. 语义去重合并：问题相同/近似（如相同故障、同一修复）的条目合并为一条，"
    "   解决方案保留最完整/最新者；"
    "2. 每条润色为结构化知识：title（≤25 字问题名）、problem（问题现象+原因分析，≤150 字）、"
    "   solution（解决方案，含关键细节/修复位置，≤220 字）、prevention（预防措施，≤90 字）；"
    "3. 只依据给定经验整理，不得虚构细节；全部使用中文；"
    "4. 若输入为空，返回 items 空数组。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"items":[{"title":"多管道 FHIR 目标表被路由覆盖",'
    '"problem":"...","solution":"...","prevention":"..."}]}'
)


def polish_validation_issues(items: list[dict]) -> list[dict]:
    """LLM 研读润色 + 去重验证经验，返回结构化知识条目。

    失败抛 AgentError（不静默回退原始文本，避免伪称已润色）。
    """
    user_content = json.dumps({"issues": items or []}, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_POLISH, user_content, "知识润色Agent")
    out = result.get("items")
    if not isinstance(out, list):
        raise AgentError("知识润色输出不合规（缺 items 数组）")
    cleaned = []
    for it in out:
        if not isinstance(it, dict):
            continue
        if not (it.get("title") or "").strip():
            continue
        cleaned.append({
            "title": str(it.get("title", "")).strip()[:60],
            "problem": str(it.get("problem", "") or it.get("pattern", "")).strip(),
            "solution": str(it.get("solution", "") or it.get("resolution", "")).strip(),
            "prevention": str(it.get("prevention", "") or "").strip(),
        })
    if not cleaned:
        raise AgentError("知识润色输出为空（可能输入不足）")
    return cleaned


# ===== Agent A：数据转换 =====
def recommend_transformation(assets: list[dict], targets: list[dict],
                             source_models: list[dict] | None = None,
                             target_models: list[dict] | None = None) -> dict:
    """Agent A：生成资产→目标表匹配建议与字段级映射。"""
    user_content = json.dumps(
        {"assets": assets, "targets": targets,
         "source_models": source_models or assets,
         "target_models": target_models or targets}, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_TRANSFORMATION, user_content, "数据转换Agent")
    recs = result.get("recommendations")
    if not isinstance(recs, list):
        recs = []

    # 容错提取：如果 recommendations 为空，但 transformation_plan.mappings 有值，自动提取构建
    plan_mappings = (result.get("transformation_plan") or {}).get("mappings") or []
    if not recs and isinstance(plan_mappings, list) and plan_mappings:
        for i, pm in enumerate(plan_mappings, 1):
            if isinstance(pm, dict):
                src = (pm.get("source") or pm.get("source_asset") or pm.get("source_entity")
                       or (assets[0].get("name") if assets else ""))
                tgt = (pm.get("target") or pm.get("target_table") or pm.get("target_entity")
                       or pm.get("emit") or (targets[0].get("table") if targets else ""))
                fms = pm.get("field_mappings") or []
                recs.append({
                    "id": f"R{i}",
                    "asset": src,
                    "target_table": tgt,
                    "confidence": 0.85,
                    "reason": pm.get("reason") or f"从转换计划中自动提取 {src} ➔ {tgt} 映射",
                    "field_mappings": fms,
                })
        result["recommendations"] = recs

    return result


# ===== Agent B：数据管道 =====
def _mask_runtime(runtime: dict | None) -> dict | None:
    """对 runtime 的连接凭据脱敏（password/secret/token → ***），仅能力/参数送 LLM。"""
    if not runtime:
        return None
    rt = dict(runtime)
    conn = rt.get("connection") or {}
    if isinstance(conn, dict):
        conn = {k: ("***" if k.lower() in ("password", "secret", "token") else v)
                for k, v in conn.items()}
    rt["connection"] = conn
    return rt


def recommend_pipeline(mappings: list[dict], source_type: str, target_type: str,
                       available_components: list[dict],
                       source_models: list[dict] | None = None,
                       target_models: list[dict] | None = None,
                       transformation_plan: dict | None = None,
                       source_runtime: dict | None = None,
                       target_runtime: dict | None = None) -> dict:
    """Agent B：生成管道组件拓扑。"""
    user_content = json.dumps(
        {
            "source": {"type": source_type,
                       "runtime": _mask_runtime(source_runtime)},
            "target": {"type": target_type,
                       "runtime": _mask_runtime(target_runtime)},
            "mappings": mappings,
            "transformation_plan": transformation_plan or {"mappings": mappings},
            "source_models": source_models or [],
            "target_models": target_models or [],
            "available_components": available_components,
        }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_PIPELINE, user_content, "数据管道Agent")
    pipeline = result.get("pipeline")
    if not isinstance(pipeline, dict):
        raise AgentError("pipeline 必须是对象")
    return result
