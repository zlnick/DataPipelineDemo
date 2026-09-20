"""数据源/数据目标类型注册表（单一事实源）。

每种类型登记：组件模板（type / className / role / comment / settings）。
- PipelineGenerator 渲染时按拓扑 JSON 的 className + settings 通用渲染；
- LLM 管道 Agent 的 available_components 从注册表动态生成；
- 加一种类型 = 加一条注册项 + 对应的 settings 构建逻辑，核心流程不动。
"""

# ---------------- 源类型组件 ----------------
SOURCE_COMPONENTS = {
    "FHIR": [
        {"type": "FHIRSyncService", "className": "demo.FHIRSyncService",
         "role": "service", "comment": "FHIR 增量同步抓取", "settings": {}},
        {"type": "FHIRService", "className": "demo.FHIRService",
         "role": "service", "comment": "队列表逐条处理（定时器，每条独立会话）", "settings": {}},
    ],
    "SQL": [
        {"type": "SQLService", "className": "EnsLib.SQL.Service.GenericService",
         "role": "service", "comment": "SQL 轮询服务（定时执行查询，逐行入队）",
         "adapter_settings": {"DSN": "", "Query": "", "KeyFieldName": "", "JGService": "EnsLib.JavaGateway.Service"},
         "host_settings": {"TargetConfigNames": ""}},
    ],
}

# ---------------- 目标类型组件 ----------------
TARGET_COMPONENTS = {
    "DB": [
        {"type": "SQLOperation", "className": "EnsLib.SQL.Operation.GenericOperation",
         "role": "operation", "comment": "写入数据库(JDBC)", "settings": {}},
    ],
    "SOAP": [
        # WSDL 导入型：className 由 %SOAP.WSDL.Reader 读 WSDL 生成的 BO 决定（勿手写 SOAP 请求构造）
        {"type": "SOAPOperation", "className": "",
         "role": "operation", "comment": "SOAP 调用（WSDL 导入生成 BO）",
         "import_based": True,
         "host_settings": {}},
    ],
    "FHIR": [
        # FHIR 存储库本质是标准 REST 接口（PUT/POST + Content-Type: application/fhir+json
        # + Basic Auth + body=资源 JSON），无需生成/引用 FHIR 专用 BO。
        # 通用做法 = EnsLib.HTTP.GenericOperation（USER 可直接引用的内置泛化 HTTP 转发 Operation）：
        #   - Adapter 配远端（HTTPServer/HTTPPort/SSLConfig），认证放请求 Authorization 头；
        #   - 请求 = EnsLib.HTTP.GenericMessage（Stream=资源 JSON + HTTPHeaders 头）；
        #   - 具体 method/path/body 由发送方（TransformProcess 打包器）构造。
        {"type": "HTTPOperation", "className": "EnsLib.HTTP.GenericOperation",
         "role": "operation", "comment": "FHIR R4 目标：通用 HTTP Operation（REST PUT，头/体由发送方构造）",
         "adapter_settings": {"HTTPServer": "", "HTTPPort": 52773,
                              "SSLConfig": "", "ResponseTimeout": 30},
         "host_settings": {}},
    ],
}

# ---------------- 通用组件（跨类型） ----------------
COMMON_COMPONENTS = [
    {"type": "TransformProcess", "className": "demo.TransformProcess",
     "role": "process", "comment": "字段映射转换", "settings": {}},
    {"type": "JavaGateway", "className": "EnsLib.JavaGateway.Service",
     "role": "gateway", "comment": "Java 网关（SQL Operation JDBC 连接）",
     "settings": {"%gatewayName": "%Java Server"}},
    # 共享术语 BO：术语转换能力在 IRIS 上的唯一出口（任何管道的 BP 都可 SendRequestSync 调它）。
    # role=infra → 不进 LLM 的 available_components 枚举（基础设施不由 Agent 选型），
    # 但进 get_common_components()（拓扑校验放行 + 平台按需追加）。
    {"type": "TerminologyOperation", "className": "demo.TerminologyOperation",
     "role": "infra",
     "comment": "术语转换共享 BO（调术语服务器 /terminology/mapping/lookup）",
     "settings": {"TermServer": "iris-terminology", "TermPort": 52773, "Timeout": 5}},
]

# ---------------- Skill 专属组件的生成契约（非预置资产） ----------------
# sql2fhir-patient-tx 的患者聚合 BP：**绝不允许平台预置 BP 资产**——其类源码由
# 数据管道设计 Agent（LLM）在每次生成该管道时产出（Skill 规范 + 布局驱动），平台只做
# 注册此"类型 token + 生成类名契约"供拓扑校验/渲染，不提供任何预置实现。
# 子表查询 BO 复用现成 EnsLib.SQL.Operation.GenericOperation（读语义参数化），无需新组件。
PIPELINE_ASSET_COMPONENTS = [
    {"type": "PatientTxProcess", "className": "demo.SqlFhirPatientTxProcess",
     "role": "process", "comment": "sql2fhir 患者聚合 BP（控制中心）——类源码由 Agent 生成，平台不预置",
     "settings": {}},
]


def get_available_components() -> list[dict]:
    """全部组件枚举（type/role/desc，供 LLM 管道 Agent 选择）。

    ⚠ 排除 `role=infra` 的基础设施组件（如 TerminologyOperation）：它们由**平台按需追加**
    （判据 = 映射里是否有 term_map 决策），不是 Agent 的可选决策项。
    """
    comps = []
    for srcs in SOURCE_COMPONENTS.values():
        comps.extend({"type": c["type"], "role": c["role"], "desc": c["comment"]} for c in srcs)
    for tgts in TARGET_COMPONENTS.values():
        comps.extend({"type": c["type"], "role": c["role"], "desc": c["comment"]} for c in tgts)
    for c in COMMON_COMPONENTS:
        if c.get("role") == "infra":
            continue
        comps.append({"type": c["type"], "role": c["role"], "desc": c["comment"]})
    return comps


def get_source_components(source_type: str) -> list[dict]:
    """源类型组件模板列表。"""
    return SOURCE_COMPONENTS.get(source_type, [])


def get_target_components(target_type: str) -> list[dict]:
    """目标类型组件模板列表。"""
    return TARGET_COMPONENTS.get(target_type, [])


def get_common_components() -> list[dict]:
    """通用组件模板列表。"""
    return COMMON_COMPONENTS


def get_pipeline_asset_components() -> list[dict]:
    """设计 Skill 专属运行资产模板（C2 校验放行/className 单一来源；不进 AI 枚举）。"""
    return PIPELINE_ASSET_COMPONENTS
