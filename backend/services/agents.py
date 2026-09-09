"""已封装 AI Agent 目录。"""

# 已封装的 Agent 元数据（前端「AI 能力」页展示：Skill / Agent）
# 流水线：接口分析 Skill（源/目标语义 + 契约解读）
#         → 数据转换 Skill（A）→ C1 转换验证 Agent → 数据管道 Skill（B）
#         → C2 管道验证-修复 Agent → 经验沉淀 →（导出时）知识润色 Skill → Obsidian 知识库
# 注：确定性组件（connection_profiler 探查 / check_connection 门禁）是事实工具，
#     不做为 AI 能力目录项（见 list_agents）。
#
# 业界口径（kind 字段，避免过度宣称）：
# - kind=skill：单次 LLM 决策（prompt 封装 + JSON 解析），职责单一、可复用，由流水线/Agent 按需调用
#   → 业界称「Skill（单轮 LLM 技能）」
# - kind=agent：事实检查工具 + LLM 决策 ≤2 轮修复循环 + 经验沉淀注入
#   → 业界认可为「Agent / agentic workflow（验证-修复循环）」
AGENTS = [
    {
        "id": "interface-analyzer-agent",
        "name": "接口分析",
        "kind": "skill",
        "role": "数据源/目标接口语义分析专家",
        "purpose": "在确定性事实采集（CapabilityStatement / 列结构 / WSDL 实体 / 运行契约）之上生成 AI 分析结论："
                   "源资产语义与轮询键建议、目标表/实体语义与写读方向、运行契约自然语言解读；"
                   "产物写回资产/目标与 runtime.note.ai，作为 Agent A/B 的上下文。"
                   "确定性只负责采集事实，结论由 LLM 生成（不静默回退规则）。",
        "input": "确定性探查事实（capability / 列 / WSDL 实体 / runtime 契约，凭据脱敏）",
        "output": "ai_semantics / key_hint / direction / ai_reason + runtime.note.ai（契约解读）",
        "trigger": "数据源 Profile 分析 / SQL 选表 / SOAP 目标创建与重导入 / DB 目标选表 时自动",
        "capabilities": ["资产语义归纳", "轮询键建议", "实体写读方向判定", "运行契约解读"],
        "engine": "LLM 单轮 prompt 封装（interface_analyzer），失败返回显式 AgentError",
        "status": "ready",
    },
    {
        "id": "connection-profiler-agent",
        "name": "连接探查 Agent",
        "role": "数据源/目标连接与能力探查器",
        "purpose": "探测源/目标并产出「运行契约」（connection/capabilities/poll/delivery/health）："
                   "FHIR 版本与 _lastUpdated 增量能力、SQL 轮询增量键、SOAP 操作语义（写入/查询）、端点可达性、"
                   "FHIR 资产真实样例字段——成为前端 / AI 上下文 / 管道生成 / 自动验证的单一参数来源",
        "input": "连接信息（endpoint/wsdl/jdbc + 凭据）",
        "output": "runtime 契约（Connection Contract，写回源/目标记录）",
        "trigger": "数据源 Profile 分析 / SQL 选表 / SOAP WSDL 导入 成功后自动",
        "capabilities": ["FHIR metadata 探测", "_lastUpdated 增量判定", "SQL 增量键探测",
                         "SOAP 操作语义判定", "FHIR 样例字段提取", "端点可达性"],
        "engine": "确定性探测函数（connection_profiler），无需 LLM",
        "status": "ready",
    },
    {
        "id": "transformation-agent",
        "name": "数据转换生成",
        "kind": "skill",
        "role": "医疗数据集成专家",
        "purpose": "基于源资产字段（含连接探查的真实字段）与目标列/写入语义，生成资产→目标匹配与字段级映射",
        "input": "源资产字段 + 目标结构 + 源/目标运行契约（能力/轮询/投递，凭据脱敏）",
        "output": "recommendations（资产→目标表/实体 + 字段映射）",
        "trigger": "AI 智能匹配",
        "capabilities": ["字段语义匹配", "FHIRPath 路径", "concat() 表达式", "表.列 前缀兼容", "日期转换"],
        "engine": "LLM 单轮 prompt 封装",
        "status": "ready",
    },
    {
        "id": "transformation-validate-agent",
        "name": "数据转换验证 Agent",
        "kind": "agent",
        "role": "医疗数据转换验证专家",
        "purpose": "验证并修复转换关系（字段映射）：目标列存在性、源字段路径、字段语义错配（单/多管道生成前均执行）",
        "input": "Agent A 推荐/用户确认的转换关系 + 资产结构",
        "output": "修正后的映射 + 验证报告（assessment/fix action）",
        "trigger": "AI 智能匹配后、生成数据管道前（含多管道逐组）",
        "capabilities": ["目标列校验", "源路径校验", "语义错配判断", "映射修复"],
        "engine": "事实检查工具 + LLM 决策封装（L1 规则 → L2 LLM ≤2 轮）",
        "status": "ready",
    },
    {
        "id": "pipeline-agent",
        "name": "数据管道设计",
        "kind": "skill",
        "role": "IRIS 互操作性架构师",
        "purpose": "根据确认的转换关系与源/目标运行契约（轮询/投递能力），生成管道组件拓扑——"
                   "AI 决定组件构成与顺序（含多目标表/多管道并存）；类型注册表只补 className/settings"
                   "与保底补齐（补齐标注 ai_supplemented），LLM 失败即报错（不静默回退规则）",
        "input": "确认映射 + 源/目标运行契约（脱敏 connection/poll/delivery）+ 组件枚举（含接口分析 Agent 语义）",
        "output": "pipeline（组件拓扑：AI 决策构成，注册表参数化）",
        "trigger": "生成数据管道",
        "capabilities": ["组件组合设计", "异构源/目标组合", "多管道拓扑", "按来源路由设计", "AI 决策可审计"],
        "engine": "LLM 生成拓扑（recommend_pipeline）+ 类型注册表参数化/完整性校验",
        "status": "ready",
    },
    {
        "id": "connection-gate",
        "name": "连通性门禁检查（check_connection）",
        "role": "生成前源/目标连通门禁",
        "purpose": "生成管道前用运行契约检查源/目标可达（FHIR metadata / SOAP endpoint / SQL ping）；"
                   "参数缺失或不可达在生成前即拦截（如 SOAP 组缺 endpoint、SQL 增量键错误）",
        "input": "各管道组的源/目标 runtime 契约",
        "output": "通过 / 拦截（错误明细返回前端）",
        "trigger": "单/多管道 generate 之前",
        "capabilities": ["FHIR 可达", "SOAP endpoint 探测", "SQL ping", "参数完整性拦截"],
        "engine": "事实检查（pipeline_validator.check_connection）",
        "status": "ready",
    },
    {
        "id": "pipeline-validate-agent",
        "name": "数据管道验证 Agent",
        "kind": "agent",
        "role": "IRIS 数据管道验证与修复专家",
        "purpose": "验证并修复数据管道生成：组件拓扑/编译/启动/消息流转（含多管道多类型并集）；"
                   "失败自动重建一次并沉淀经验 ^demo.ValidationIssue 供后续修复参考",
        "input": "管道生成结果 + 拓扑 + 源/目标类型（单/多） + ^demo.ValidationIssue 历史经验",
        "output": "验证报告（issue/error）+ 修复动作 + 经验沉淀",
        "trigger": "生成数据管道失败或验证不通过（单/多管道统一管线）",
        "capabilities": ["拓扑校验", "编译检查", "启动检查", "消息 smoke test", "分层修复", "经验沉淀"],
        "engine": "事实检查工具 + LLM 决策封装（L1 规则 → L2 LLM ≤2 轮 → L3 回退默认）",
        "status": "ready",
    },
    {
        "id": "knowledge-polish-agent",
        "name": "知识润色",
        "kind": "skill",
        "role": "医疗 IT 数据集成知识库编辑",
        "purpose": "把验证-修复 Agent 沉淀到 ^demo.ValidationIssue 的经验研读去重并润色为结构化知识"
                   "（title/problem/solution/prevention），以 Obsidian 笔记写入知识库——供人可检索的沉淀",
        "input": "最近验证经验（pattern/resolution/source，可含同一故障的重复记录）",
        "output": "去重合并后的结构化知识条目",
        "trigger": "宿主导出 export_validation_issues.py（写 knowledge/04-Pitfalls 前）",
        "capabilities": ["语义去重合并", "经验研读结构化", "Obsidian 知识导出"],
        "engine": "LLM 单轮 prompt 封装（llm_client.polish_validation_issues），失败即报错",
        "status": "ready",
    },
    {
        "id": "mapping-agent",
        "name": "术语映射判定 Agent（C3）",
        "kind": "agent",
        "role": "中国药品名 ↔ RxNorm 术语映射判定专家",
        "purpose": "把中文药品名（医保/商保目录，可能含剂型）判定为 RxNorm 概念："
                   "多路召回（chinese-map 词表确定性命中 + 英文名二次向量召回 IN/SCD/SBD/BN + 中文原词兜底）"
                   "后由 LLM 在候选证据上选最佳 RXCUI（明确 TTY 层）或判无映射",
        "input": "中文药名 + 可选语境（来源/剂型/商品名）",
        "output": "判定结果（match/rxcui/tty/enName/reason/confidence/alias_based）+ 召回证据",
        "trigger": "POST /api/mapping/cn2rx（术语互操作/数据管道目标映射阶段）",
        "capabilities": ["多路召回合并", "音译/同族混淆判定", "无映射显式拒绝", "证据可审计"],
        "engine": "确定性多路召回（词表/向量，非决策）+ LLM 单轮判定；召回为空或 LLM 失败均显式返回",
        "status": "ready",
    },
]


def list_agents() -> list[dict]:
    """返回已封装 AI 能力列表（仅 LLM 驱动：4 个 Skill + 2 个 Agent；确定性探查/门禁组件不在此目录展示）。

    确定性组件（无 LLM）不作为能力目录项：连接探查见 backend/services/connection_profiler.py，
    连通性门禁见 pipeline_validator.check_connection。
    """
    skip = {"connection-profiler-agent", "connection-gate"}
    return [a for a in AGENTS if a.get("id") not in skip]
