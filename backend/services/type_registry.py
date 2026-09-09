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
        # FHIR 存储库（US Core 声明式）：写入 FHIR server 的官方内置互操作 BO。
        # BO=HS.FHIRServer.Interop.Operation（IRIS for Health 内置，勿手写类）；
        # 网络配置落在其 Adapter（EnsLib.HTTP 子类）上：HTTPServer/HTTPPort/Username/Password。
        {"type": "FHIROperation", "className": "HS.FHIRServer.Interop.Operation",
         "role": "operation", "comment": "FHIR 资源投放（US Core profile 声明式，内置互操作 BO）",
         "adapter_settings": {"HTTPServer": "", "HTTPPort": 52773,
                              "Username": "", "Password": ""},
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
]


def get_available_components() -> list[dict]:
    """全部组件枚举（type/role/desc，供 LLM 管道 Agent 选择）。"""
    comps = []
    for srcs in SOURCE_COMPONENTS.values():
        comps.extend({"type": c["type"], "role": c["role"], "desc": c["comment"]} for c in srcs)
    for tgts in TARGET_COMPONENTS.values():
        comps.extend({"type": c["type"], "role": c["role"], "desc": c["comment"]} for c in tgts)
    for c in COMMON_COMPONENTS:
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
