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
    "你是医疗数据集成专家。给定源数据资产（HL7 FHIR 资源类型及其关键字段）"
    "与目标表（远端数据库表及列结构），为每个源资产推荐最合适的投放目标表并生成字段级映射。"
    "要求：1. 基于字段语义匹配（FHIR 资源字段→目标表列），字段高度匹配时 confidence 高。"
    "2. field_mappings 的 source 使用 FHIRPath 风格路径，target 为目标表列名。"
    "3. 日期字段建议加 transform:date（提取 YYYY-MM-DD）。"
    "4. 无法匹配的目标返回空列表即可。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"recommendations":[{"asset":"Patient","target_table":"patient","confidence":0.92,'
    '"reason":"字段高度匹配","field_mappings":[{"source":"id","target":"ID","transform":null},'
    '{"source":"birthDate","target":"BirthDate","transform":"date"}]}]}'
)


# ===== Agent B：数据管道（系统提示词） =====
SYSTEM_PROMPT_PIPELINE = (
    "你是 IRIS 互操作性架构师。给定已确认的转换关系（源资产→目标表）、源/目标类型与组件枚举，"
    "设计数据管道 Production 的组件拓扑。"
    "要求：1. 组件 type 必须从 available_components 枚举中选择（不要自创组件类型）。"
    "2. 按源/目标类型选择合适的 service / process / operation 组件。"
    "3. 数据流顺序：service（抓取）→service（处理）→process（转换）→operation（写入）→gateway。"
    "4. SQLOperation 组件的 table 填目标表名。"
    "5. 只输出拓扑，不要输出 SQL、字段映射或代码。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"pipeline":{"components":[{"type":"FHIRSyncService","name":"FHIRSyncService"},'
    '{"type":"FHIRService","name":"FHIRService"},'
    '{"type":"TransformProcess","name":"TransformProcess"},'
    '{"type":"SQLOperation","name":"SQLOp_patient","table":"patient"},'
    '{"type":"JavaGateway","name":"EnsLib.JavaGateway.Service"}]}}'
)


# ===== 公共调用逻辑（token 日志 + 一次重试） =====
def _call_llm(system_prompt: str, user_content: str, agent_name: str) -> dict:
    """调用 LLM 一次并解析 JSON 返回。"""
    if not LLMConfig.API_KEY or LLMConfig.API_KEY.startswith("sk-xxxx"):
        raise AgentError("未配置 LLM_API_KEY，请在 .env 中设置（OpenAI 兼容服务）")
    client = OpenAI(base_url=LLMConfig.BASE_URL, api_key=LLMConfig.API_KEY)
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


# ===== Agent A：数据转换 =====
def recommend_transformation(assets: list[dict], targets: list[dict]) -> dict:
    """Agent A：生成资产→目标表匹配建议与字段级映射。"""
    user_content = json.dumps(
        {"assets": assets, "targets": targets}, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_TRANSFORMATION, user_content, "数据转换Agent")
    recs = result.get("recommendations", [])
    if not isinstance(recs, list):
        raise AgentError("recommendations 必须是数组")
    return result


# ===== Agent B：数据管道 =====
def recommend_pipeline(mappings: list[dict], source_type: str, target_type: str,
                       available_components: list[dict]) -> dict:
    """Agent B：生成管道组件拓扑。"""
    user_content = json.dumps(
        {
            "source": {"type": source_type},
            "target": {"type": target_type},
            "mappings": mappings,
            "available_components": available_components,
        }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_PIPELINE, user_content, "数据管道Agent")
    pipeline = result.get("pipeline")
    if not isinstance(pipeline, dict):
        raise AgentError("pipeline 必须是对象")
    return result
