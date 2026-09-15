"""数据管道设计 Skill 注册表（单一事实源）。

角色定位：
- 数据管道设计 Agent（规划师，agents.py 的 pipeline-agent）是「入口 + 规划师」：
  根据源/目标对 + 已确认映射 + 运行契约，从本目录选择/组合适用的设计 Skill 并绑定参数；
- 每个「管道设计 Skill」是**可复用的受控实现能力**：声明适用的 (源, 目标) 组合、
  拓扑规范（组件角色 + 数据流 + 受控规则）、参数白名单、运行资产（className）、依赖与状态。
- 本注册表只登记与描述能力；Skill 的执行（executor 参数化生成拓扑/运行资产接入）
  与 AI 决策（Skill 选型）分离：选型是 LLM（ai.driven），执行参数化/校验是确定性基础设施。

覆盖组合：
  SQL→FHIR（sql2fhir-patient-tx，患者级事务推送，新）+ 既有闭环收编回归基线
  （sql2db / fhir2db / sql2soap / fhir2soap）。
"""

DESIGN_SKILLS = [
    {
        "id": "sql2fhir-patient-tx",
        "name": "SQL→FHIR 患者事务设计 Skill",
        "kind": "design-skill",
        "role": "CLINIC 型 SQL 源 → FHIR Server 的患者级事务推送设计",
        "purpose": "把 SQL 源中一个患者及其全部关联（就诊/诊断/药嘱）以「整户一个 FHIR transaction Bundle」"
                   "投递到 FHIR 库：只由 BS 抽取患者主表行，子表数据在 BP 内经查询 BO 按 key 拉回，"
                   "BP 组装 Bundle 后经 HTTP Operation 整体 POST，保证引用完整性与全量幂等。",
        "applies_to": {"source": "SQL", "target": "FHIR"},
        "input": "已确认映射（覆盖患者主表及其子表的目标资源）+ 源表清单（含主外键列）+ "
                 "源运行契约（DSN/轮询键）+ FHIR base/认证 + fhir model schema",
        "output": "管道组件拓扑（Skill executor 参数化）+ design_skill 决策记录（ai.driven）",
        "params": [
            {"name": "source_assets", "source": "已确认 mapping 涉及的全部源表（含列与外键引用列、接口分析语义）",
             "required": True, "note": "参与表集合 = mapping 涉及集合；患者主表/子表划分为 Skill 推导输出"},
            {"name": "mappings", "source": "已确认转换关系", "required": True,
             "note": "Patient/Encounter/Condition/MedicationRequest 各自映射（选中哪些资源即推导哪些 BO）"},
            {"name": "fhir_target_config", "source": "FHIR 目标 base/auth（target_config/base_url）",
             "required": True, "note": "作为 HTTP Operation 的 Adapter 连接参数"},
            {"name": "fhir_model_schema", "source": "目标资源字段模型（path/type/references）",
             "required": True, "note": "引用关系树（Encounter.subject→Patient 等）由 model references 提供"},
        ],
        "topology_spec": [
            {"role": "患者主表 BS", "type": "SQLService",
             "note": "EnsLib.SQL.Service.GenericService；Query=患者主表；KeyFieldName=患者主键；逐患者入队",
             "asset": "EnsLib.SQL.Service.GenericService"},
            {"role": "子表查询 BO ×N", "type": "SQLOperation",
             "note": "复用现成 EnsLib.SQL.Operation.GenericOperation 作读语义查询 BO（配置由数据管道设计 "
                    "Agent 依 bo_contract 产出：Host Query=SELECT * FROM SQLUser.<表> WHERE <fk>=?、"
                    "InputParameters=*<fk>；Adapter DSN/JGService；请求/响应=Ens.StreamContainer JSON），无需新类",
             "asset": "EnsLib.SQL.Operation.GenericOperation（现成组件）"},
            {"role": "患者聚合 BP（控制中心）", "type": "PatientTxProcess",
             "note": "类源码由数据管道设计 Agent 在生成管道时产出（generated_by_agent，平台绝无预置 BP）："
                    "收到患者行→依布局发查询请求→确定性 UUID→全 PUT transaction 组装→POST FHIR BO",
             "asset": "Agent 生成的 demo.SqlFhirPatientTxProcess（生成产物，非平台资产）"},
            {"role": "FHIR BO", "type": "HTTPOperation",
             "note": "EnsLib.HTTP.GenericOperation；Adapter=base；POST Bundle(type=transaction)",
             "asset": "EnsLib.HTTP.GenericOperation"},
        ],
        "rules": [
            "患者主表只由 BS 抽取（一个 BS 只抽一张表）；子表不由 BS 直抽，避免扫描凭证错位",
            "每个患者 = 整户一个 Bundle(type=transaction) 整体投递，不逐条 PUT 单资源",
            "每个资源先按源主键派生确定性 UUID（同一源行重扫/重推得同一 UUID，保证幂等）",
            "全部 entry 同构：fullUrl=urn:uuid:<UUID>、resource.id=同 UUID、request={method:PUT,url:<ResourceType>/<UUID>}",
            "引用一律用 bundle 内 fullUrl（urn:uuid:）表达，由 FHIR server 在事务内解析",
            "Skill 只改投递组织形态，不改字段映射（数据转换 Skill 不动）",
        ],
        "derivation": {
            "原理": "组件数量与聚合方式由输入数据驱动推导（不固定 BO 数/不固定四表）："
                    "executor 读「已确认 mapping（= 用户/AI 选中的处理集合）+ 源资产结构（表/列/外键）+ "
                    "fhir model schema（目标资源引用路径）」后推出查询 BO 布局与聚合 BP 编排。",
            "参与表集合": "取 mapping 涉及的全部源表；未出现在 mapping 的表不参与（如只选 Patient+Encounter，"
                          "则不产生诊断/药嘱查询 BO，bundle 只含 Patient+Encounter）",
            "患者主表判定": "含患者主标识列（ID/MRN/PatientID；接口分析 ai_semantics.key_hint 优先）且映射目标为 "
                            "Patient 的表；推导不出时显式报错（向接口分析/规划师要语义，不猜测）",
            "BO 数量推导": "每张「被选中的子表」生成一个查询 BO（Encounters 被选→1 个；Encounter+Diagnosis+"
                           "MedicationOrder 被选→3 个）；无子表被选→0 个查询 BO，仅患者 PUT",
            "BO 查询参数": "子表 BO 的 WHERE 条件按其引用父 key 的列派生：仅依赖患者 key 的表（Encounter "
                           "WHERE PatientID=?）先查并得就诊 UUID；依赖就诊 key 的表（Diagnosis/MedicationOrder "
                           "WHERE EncounterID=?）绑定到每个就诊 UUID 后查；引用列由资产列名+接口分析语义判定，"
                           "无外键列的子表拒绝参与（显式报错）",
            "聚合 BP 编排": "按目标资源引用关系树聚合：根=Patient→（若 Encounter 被选）Encounter；"
                            "Condition/MedicationRequest 的 encounter 引用仅在 Encounter 被选且为父时填写，"
                            "否则省略该引用字段（不编造）；bundle 顺序=Patient→Encounter→诊断/药嘱",
            "UUID 派生": "Patient UUID←患者主键、Encounter UUID←EncounterID、子资源 UUID←各自表主键",
        },
        "bo_contract": {
            "说明": "查询 BO 的配置知识（EnsLib.SQL.Operation.GenericOperation，官方文档 ESQL_bo / 类参考）。"
                    "数据管道设计 Agent 生成 SQL 源管道时必须据此配置每个查询 BO，不得自创新组件/新类。",
            "组件": "EnsLib.SQL.Operation.GenericOperation（类型 SQLOperation）——BP 发查询请求，它执行并返回行集",
            "settings_Adapter": "DSN=源数据源 DSN；JGService=EnsLib.JavaGateway.Service；可配 Credentials",
            "settings_Host": "Query=参数化 SELECT（如 SELECT * FROM SQLUser.<表> WHERE <fk> = ?）；"
                             "InputParameters=*<父键列名>（值取请求对象/Stream JSON 的同名属性）",
            "请求消息": "BP 发送 Ens.StreamContainer（Stream=含父键属性的 JSON，如 {\"PatientID\":\"P001\"}）",
            "响应消息": "默认无 ResponseClass 时返回 Ens.StreamContainer，Stream=JSON，包含查询全部行；"
                        "写语句（Insert/Update/Delete）响应仅 NumRowsAffected",
            "注意": "SELECT 勿配 ResponseClass（多行只回第一行且丢行）；默认 JSON 响应保留全行；"
                    "BP 解析行集需兼容 JSON 行对象/行数组形态；DSN 需 JGService 指向 JavaGateway",
        },
        "assets": {
            "BS": "EnsLib.SQL.Service.GenericService",
            "查询BO": "EnsLib.SQL.Operation.GenericOperation（现成组件参数化，依 bo_contract）",
            "聚合BP": "Agent 生成的 demo.SqlFhirPatientTxProcess（无预置）",
            "FHIR BO": "EnsLib.HTTP.GenericOperation",
        },
        "dependencies": ["transformation-agent（Agent A 字段映射）", "C1 转换验证",
                         "C3/C3-Dx 判码 Skill（术语双 coding）", "fhir model schema 通用打包器",
                         "pipeline-validate-agent（C2）"],
        "trigger": "生成数据管道且源=SQL、目标=FHIR",
        "engine": "数据管道设计 Agent（规划师）选本 Skill 后：①依 bo_contract 输出查询 BO 的 SQLOperation "
                  "组件与参数；②输出患者聚合 BP（控制中心）的类源码作为生成产物（平台不预置 BP）；"
                  "③executor 只做注册表参数化/完整性校验/编译准入",
        "status": "designed",
        "note": "运行 BP 由 Agent 生成（generated_by_agent），实现中；查询 BO 全用现成 GenericOperation。",
    },
    {
        "id": "sql2db",
        "name": "SQL→DB 直写设计 Skill",
        "kind": "design-skill",
        "role": "SQL 源轮询 → 目标表 UPSERT",
        "purpose": "按源表逐个轮询（一个 BS 只抽一张表，N 张表 = N 个 BS），行消息经转换后"
                   "由 SQLOp_{目标表} 做 INSERT OR UPDATE 幂等写入（既有闭环行为基线）。",
        "applies_to": {"source": "SQL", "target": "DB"},
        "input": "源表清单 + 目标表映射 + 源运行契约（DSN/轮询键）+ 目标表列结构",
        "output": "管道组件拓扑（Skill executor 参数化）+ design_skill 决策记录",
        "params": [
            {"name": "source_tables", "source": "mapping 涉及的源表", "required": True,
             "note": "每表一个 BS：SQLService_{表}，Query=SELECT * FROM SQLUser.<表>"},
            {"name": "target_table_mappings", "source": "已确认映射", "required": True,
             "note": "每个目标表对应 SQLOp_{表}"},
        ],
        "topology_spec": [
            {"role": "源 BS ×N", "type": "SQLService",
             "note": "一表一 BS（SQLService_{表}），各自独立 KeyFieldName 扫描凭证"},
            {"role": "转换 BP", "type": "TransformProcess", "note": "行 JSON→目标行，按来源路由"},
            {"role": "写 BO ×N", "type": "SQLOperation", "note": "UPSERT 幂等写目标表"},
        ],
        "rules": ["源里有几张表要处理就生成几个 BS，一个 BS 只抽一张表",
                  "子表间引用完整性由上游数据保证（平台不代做关联查询）"],
        "assets": {"BS": "EnsLib.SQL.Service.GenericService",
                   "写BO": "EnsLib.SQL.Operation.GenericOperation",
                   "BP": "demo.TransformProcess"},
        "dependencies": ["transformation-agent", "C1", "pipeline-validate-agent"],
        "trigger": "生成数据管道且源=SQL、目标=DB",
        "engine": "现由 pipelines.py 既有 executor 提供（收编基线）；Skill 通道接入时保持回归",
        "status": "ready",
        "note": "行为基线=现有 SQL→DB 单/多表生成路径（回归用）。",
    },
    {
        "id": "fhir2db",
        "name": "FHIR→DB 落库设计 Skill",
        "kind": "design-skill",
        "role": "FHIR 源增量抓取 → 目标表落库",
        "purpose": "FHIRSyncService 按 _lastUpdated 游标抓取资源、FHIRService 逐条入队，"
                   "经转换（含判码双 coding）后由 SQLOp_{目标表} 幂等写入（既有闭环行为基线）。",
        "applies_to": {"source": "FHIR", "target": "DB"},
        "input": "FHIR endpoint/auth + 目标表映射 + 源运行契约（能力/增量游标）+ 目标表列结构",
        "output": "管道组件拓扑 + design_skill 决策记录",
        "params": [
            {"name": "fhir_source_config", "source": "FHIR endpoint/auth（source_type=FHIR）",
             "required": True, "note": "FHIRSyncService 连接参数"},
            {"name": "target_table_mappings", "source": "已确认映射", "required": True,
             "note": "资源→目标表（可能一资源多表并存）"},
        ],
        "topology_spec": [
            {"role": "抓取服务", "type": "FHIRSyncService", "note": "增量同步，_lastUpdated 游标"},
            {"role": "逐条服务", "type": "FHIRService", "note": "队列表逐条处理，独立会话"},
            {"role": "转换 BP", "type": "TransformProcess", "note": "FHIRRequest→目标行（含判码）"},
            {"role": "写 BO ×N", "type": "SQLOperation", "note": "UPSERT 写目标表"},
        ],
        "rules": ["FHIR 源需同时含 FHIRSyncService 与 FHIRService 两个服务组件"],
        "assets": {"BS": "demo.FHIRSyncService/demo.FHIRService",
                   "写BO": "EnsLib.SQL.Operation.GenericOperation",
                   "BP": "demo.TransformProcess"},
        "dependencies": ["transformation-agent", "C1", "C3/C3-Dx（目标为编码列时）", "pipeline-validate-agent"],
        "trigger": "生成数据管道且源=FHIR、目标=DB",
        "engine": "现由 pipelines.py 既有 executor 提供（收编基线）；Skill 通道接入时保持回归",
        "status": "ready",
        "note": "行为基线=现有 FHIR→DB 生成路径（回归用）。",
    },

    {
        "id": "sql2soap",
        "name": "SQL→SOAP 投递设计 Skill",
        "kind": "design-skill",
        "role": "SQL 源轮询 → SOAP 服务调用（WSDL 导入 BO）",
        "purpose": "SQL 源行消息经转换打包为写入型 SOAP 请求（如 AddPatient 扁平标量），"
                   "由 SOAPOp_{service}（WSDL 导入生成 BO，Adapter 指向远端）投递第三方系统（既有闭环基线）。",
        "applies_to": {"source": "SQL", "target": "SOAP"},
        "input": "源表清单 + SOAP 目标（wsdl/service/packages/endpoint）+ 映射（含目标字段=请求元素）",
        "output": "管道组件拓扑 + design_skill 决策记录",
        "params": [
            {"name": "source_tables", "source": "映射涉及的源表", "required": True,
             "note": "一表一 BS（SQLService_{表}）"},
            {"name": "soap_target_config", "source": "wsdl/service/packages/endpoint", "required": True,
             "note": "WSDL 导入生成 BO，Adapter WebServiceURL=远端"},
        ],
        "topology_spec": [
            {"role": "源 BS ×N", "type": "SQLService", "note": "一表一 BS"},
            {"role": "转换 BP", "type": "TransformProcess", "note": "行 JSON→SOAP 请求（PackSOAPRequest）"},
            {"role": "SOAP BO", "type": "SOAPOperation", "note": "WSDL 导入 BO（勿手写请求构造）"},
        ],
        "rules": ["SOAP 请求元素设计为扁平标量（复杂对象属性会被 OutboundAdapter 静默丢弃）",
                  "远端被调系统由演示侧承担（mock_soap），不改 IRIS CSP/Web Application"],
        "assets": {"BS": "EnsLib.SQL.Service.GenericService", "BP": "demo.TransformProcess",
                   "SOAPBO": "WSDL 导入生成的 BO"},
        "dependencies": ["transformation-agent", "C1", "wsdl_importer", "pipeline-validate-agent"],
        "trigger": "生成数据管道且源=SQL、目标=SOAP",
        "engine": "现由 pipelines.py 既有 executor 提供（收编基线）；Skill 通道接入时保持回归",
        "status": "ready",
        "note": "行为基线=现有 SQL→SOAP 闭环（回归用）。",
    },
    {
        "id": "fhir2soap",
        "name": "FHIR→SOAP 投递设计 Skill",
        "kind": "design-skill",
        "role": "FHIR 源增量抓取 → SOAP 服务调用",
        "purpose": "FHIR 源增量抓取/逐条入队后，经转换打包为写入型 SOAP 请求并由 SOAPOp 投递"
                   "（既有 FHIR→SOAP 闭环基线的设计收编）。",
        "applies_to": {"source": "FHIR", "target": "SOAP"},
        "input": "FHIR endpoint/auth + SOAP 目标 + 映射",
        "output": "管道组件拓扑 + design_skill 决策记录",
        "params": [
            {"name": "fhir_source_config", "source": "FHIR endpoint/auth", "required": True, "note": ""},
            {"name": "soap_target_config", "source": "wsdl/service/packages/endpoint", "required": True, "note": ""},
        ],
        "topology_spec": [
            {"role": "抓取服务", "type": "FHIRSyncService", "note": "增量同步"},
            {"role": "逐条服务", "type": "FHIRService", "note": "队列表逐条"},
            {"role": "转换 BP", "type": "TransformProcess", "note": "FHIRRequest→SOAP 请求"},
            {"role": "SOAP BO", "type": "SOAPOperation", "note": "WSDL 导入 BO"},
        ],
        "rules": ["SOAP 请求元素扁平标量", "远端被调由演示 mock 承担"],
        "assets": {"BS": "demo.FHIRSyncService/demo.FHIRService", "BP": "demo.TransformProcess",
                   "SOAPBO": "WSDL 导入生成的 BO"},
        "dependencies": ["transformation-agent", "C1", "wsdl_importer", "pipeline-validate-agent"],
        "trigger": "生成数据管道且源=FHIR、目标=SOAP",
        "engine": "现由 pipelines.py 既有 executor 提供（收编基线）；Skill 通道接入时保持回归",
        "status": "ready",
        "note": "行为基线=现有 FHIR→SOAP 路径（回归用）。",
    },
]


# 规划师决策时可以组合 Skill（如多管道逐组异构）；组合规则不属于某一 Skill 本身，
# 由 pipeline-agent 在「多管道/异构源目标」时逐组按 applies_to 匹配并合并共享组件。


def get_design_skills() -> list[dict]:
    """返回管道设计 Skill 目录全量（供规划师 prompt 注入 / Agents 页展示 / executor 查表）。"""
    return [dict(s) for s in DESIGN_SKILLS]


def match_design_skills(source_type: str, target_type: str) -> list[dict]:
    """按 (源, 目标) 对返回可用的设计 Skill（applies_to 命中）。"""
    return [dict(s) for s in DESIGN_SKILLS
            if s.get("applies_to", {}).get("source") == source_type
            and s.get("applies_to", {}).get("target") == target_type]


def design_skills_prompt_summary(source_type: str | None = None,
                                 target_type: str | None = None) -> str:
    """生成给数据管道设计 Agent（规划师）的 Skill 目录摘要文本。"""
    skills = match_design_skills(source_type, target_type) if source_type and target_type \
        else get_design_skills()
    if not skills:
        return "（当前源/目标类型无匹配的设计 Skill）"
    lines = ["可用管道设计 Skill 目录："]
    for s in skills:
        lines.append(
            f"- {s['id']}（{s['name']}）：适用于 源={s['applies_to']['source']}/目标={s['applies_to']['target']}；"
            f"状态={s['status']}；\n    用途={s['purpose']}")
        lines.append(f"    受控规则：{('；'.join(s['rules']))}")
    return "\n".join(lines)

