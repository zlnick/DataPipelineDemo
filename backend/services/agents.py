"""已封装 AI Agent 目录。"""

# 已封装的 Agent 元数据（前端「AI Agents」页展示）
AGENTS = [
    {
        "id": "transformation-agent",
        "name": "数据转换 Agent",
        "role": "医疗数据集成专家",
        "purpose": "分析源资产字段与目标表列，生成资产→目标表匹配与字段级映射",
        "input": "源资产字段结构 + 目标表列结构",
        "output": "recommendations（资产→目标表 + 字段映射）",
        "trigger": "AI 智能匹配",
        "capabilities": ["字段语义匹配", "FHIRPath 路径", "日期转换"],
        "engine": "LLM 单轮 prompt 封装",
        "status": "ready",
    },
    {
        "id": "pipeline-agent",
        "name": "数据管道 Agent",
        "role": "IRIS 互操作性架构师",
        "purpose": "根据确认的转换关系与源/目标类型，设计数据管道组件拓扑",
        "input": "确认映射 + 源/目标类型 + 组件枚举",
        "output": "pipeline（组件拓扑）",
        "trigger": "生成数据管道",
        "capabilities": ["组件组合设计", "拓扑生成"],
        "engine": "LLM 单轮 prompt 封装",
        "status": "ready",
    },
    {
        "id": "transformation-validate-agent",
        "name": "数据转换验证 Agent",
        "role": "医疗数据转换验证专家",
        "purpose": "验证并修复转换关系（字段映射）：目标列存在性、源字段路径、字段语义错配",
        "input": "Agent A 推荐/用户确认的转换关系 + 资产结构",
        "output": "修正后的映射 + 验证报告（assessment/fix action）",
        "trigger": "AI 智能匹配后、生成数据管道前",
        "capabilities": ["目标列校验", "源路径校验", "语义错配判断", "映射修复"],
        "engine": "事实检查工具 + LLM 决策封装（L1 规则 → L2 LLM ≤2 轮）",
        "status": "ready",
    },
    {
        "id": "pipeline-validate-agent",
        "name": "数据管道验证 Agent",
        "role": "IRIS 数据管道验证与修复专家",
        "purpose": "验证并修复数据管道生成：组件拓扑/编译/启动/消息流转（L1 规则 → L2 LLM ≤2 轮 → L3 回退默认）",
        "input": "管道生成结果 + 拓扑 + ^demo.ValidationIssue 历史经验",
        "output": "assessment（问题判断）+ fix action（修复动作）+ 经验沉淀",
        "trigger": "生成数据管道失败或验证不通过",
        "capabilities": ["拓扑校验", "编译检查", "启动检查", "消息 smoke test", "分层修复"],
        "engine": "事实检查工具 + LLM 决策封装",
        "status": "ready",
    },
]


def list_agents() -> list[dict]:
    """返回已封装 Agent 列表。"""
    return AGENTS
