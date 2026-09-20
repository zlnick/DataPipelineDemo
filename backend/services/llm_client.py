"""OpenAI 兼容 LLM 客户端：两个 AI Agent 的服务封装。"""

import json
import logging
import time

from openai import OpenAI, Timeout

from backend.config import LLMConfig
from backend.services import pipeline_design_skills
from backend.services import transform_directives

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
    "5. 术语判定决策（Skill 执行，禁止编造标准码）：源字段若携带编码/术语"
    "（如国标 ICD-10 诊断码、中文药品码，通常列名含 Code/DiagnosisCode/MedicationCode 或存在 CodeSystem 列），"
    "而目标 FHIR 字段（如 Condition.code / Encounter.reason_code / MedicationRequest.medication 等"
    "CodeableConcept 列，其 note 含『建议双 coding』/『经术语对照』）希望保留源码 + 补充目标标准体系时，"
    "给该 field_mapping 设置 transform 为受控指令："
    "  药品→RxNorm 用 term_map:cn2rx；诊断/症状（国标 ICD-10 或中文诊断）→SNOMED 用 term_map:cn2snomed；"
    "不需要术语转换的普通映射 transform 保持 null（或原 date/concat 表达式）。"
    "⚠ **优先级（硬约束）**：`code` 只是『目标为 code/codeableConcept』的类型提示，**不产生任何目标体系 coding**；"
    "当目标列 note 已写明『建议双 coding』/『经术语对照』（保留源码 + 补充目标标准体系）时，"
    "该列 transform **必须是 term_map:<skill_id>**，不得写 `code`/null —— "
    "若你的 reason 里写了『双 coding / 补充标准体系 / 经术语对照』而 transform 却是 code/null，即为输出不合规。"
    "（`code` 只用于**没有**术语转换诉求的 codeableConcept/Reference 旁列，如 clinicalStatus、固定状态列。）"
    "6. 给出 term_map 时必须在 reason 中说明『调用哪个判定 Skill、源术语与目标体系』（如："
    "国标ICD-10→SNOMED，调用诊断映射判定Skill(cn2snomed) 双 coding）；不要把标准码写进转换结果。"
    "7. 目标资源为 FHIR 且无字段模型（开放类型）时，target 字段用 FHIRPath 表达并按需套用上述术语规则。"
    "8. transform **只能取受控指令集内写法**（见 user 内容的 transform_directives 目录）：普通映射用 null 或 "
    "\"direct\"；日期用 \"date\"；**无源列可映射的必填字段用 \"constant:<值>\" 且 source=null**；术语用 "
    "\"term_map:<skill_id>\"；不得发明新指令、不得把常量值放进 source。"
    "9. DB/SQL 目标表的主键列是**硬事实**（见 user 内容 target_models[].key_columns，如 IRIS 表 Patient 的主键 ID）："
    "目标侧 INSERT 要求主键列有非空值，转换结果缺主键值会在运行期报 \"<列名> is required\" 并使该目标零落地。"
    "因此为 DB 目标生成映射时，**必须为每个主键列各产出一条 field_mapping**——"
    "通常直接映射源表主键或语义等价的业务键（如源 SQLUser.Patient.ID → 目标 ID），"
    "源侧确实没有可用业务键时才用 transform=\"constant:<稳定值>\" 且 source=null。"
    "注意别只把源主键映射到某个业务列（如 ID→MRN）而**漏掉目标主键列本身**。"
    "10. field_mapping.source 的**规范写法**：源为 SQL 表时写 \"<源表名>.<列名>\"（如 Patient.FamilyName），"
    "源为 FHIR 资源时写路径本身（如 name[0].family）或 \"<资产名>.<路径>\"；"
    "**不要写 schema/库前缀**（禁止 \"SQLUser.Patient.FamilyName\"、\"CLINIC.Patient.ID\" 这类三段全限定名）——"
    "运行期源数据是扁平列名，三段写法会让取值落到空值（平台虽会自动剥前缀，但请按规范输出）；"
    "concat 表达式的每个参数同样遵循该写法（如 concat(Patient.FamilyName, ' ', Patient.GivenName)）。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"recommendations":[{"asset":"PatientTable","target_table":"PatientEntity","confidence":0.90,'
    '"reason":"根据 ID 和姓名字段精准匹配","field_mappings":[{"source":"PatientTable.ID","target":"PatientNo","transform":null},'
    '{"source":"PatientTable.GivenName","target":"FullName","transform":null}]}],'
    '"transformation_plan":{"source_models":[],"target_models":[],"mappings":[{"source":"PatientTable","target":"PatientEntity","field_mappings":[]}],"status":"draft"}}'
)


# ===== Agent B：数据管道设计 Agent（规划师：选 Skill + 编排，kind=planner） =====
SYSTEM_PROMPT_PIPELINE = (
    "你是数据管道设计 Agent（规划师）。给定源/目标类型、已确认映射、运行契约、组件注册表枚举与管道设计 Skill 目录，"
    "你的职责：① 从 design_skills 目录为当前源/目标对选择适用的设计 Skill，在顶层输出 design_skill=<skill_id>"
    "（决策记录，ai.driven 可审计）；② 生成完整组件拓扑——组件构成与顺序遵循所选 Skill 的规则，"
    "type 必须从 available_components 枚举中选择（系统只做参数补全与完整性校验）。"
    "要求：1. 组件 type 必须从 available_components 枚举中选择（不要自创组件类型），并为每个组件给出合适的 name"
    "（SQLOperation 建议 SQLOp_<表名>，SOAPOperation 建议 SOAPOp_<service>）。"
    "2. 组件必须完整：覆盖源类型所需的全部服务组件（FHIR 源需同时包含 FHIRSyncService 与 FHIRService 两个服务；"
    "SQL 源为 SQLService）、TransformProcess、目标操作组件（DB 目标：为每个 mapping 的 target_table 各生成一个 "
    "SQLOperation 并在其 table 字段填表名；SOAP 目标：一个 SOAPOperation）以及 JavaGateway。"
    "3. 数据流顺序：service（抓取）→service（处理）→process（转换）→operation（写入）→gateway。"
    "4. SQLOperation 组件的 table 填目标表名；不要输出 SQL、字段映射或代码。"
    "5. SQL 源多表规则（design_skill=sql2db/sql2soap/sql2fhir-patient-tx）：源里有几张表要处理就生成几个 "
    "SQLService（命名建议 SQLService_<表>），一个 BS 只抽一张表，各自独立轮询。"
    "sql2fhir-patient-tx 时，子表数据不由 BS 直抽：每个参与子表配置一个查询 BO——复用现成 "
    "SQLOperation（EnsLib.SQL.Operation.GenericOperation）并按 design_skills 里该 Skill 的 "
    "bo_contract 给出 Adapter(DSN/JGService) 与 Host(Query=SELECT * FROM SQLUser.<表> WHERE <fk> = ?、"
    "InputParameters=*<父键列>) 参数，不得自创组件类型或新类。"
    "6. FHIR 目标组织知识（design_skill=sql2fhir-patient-tx 时遵循）：以患者为单位整户一个 "
    "Bundle(type=transaction) 推送；每个资源先按源主键派生确定性 UUID（重扫/重推同源行得同一 UUID，幂等）；"
    "全部 entry 同构用 PUT（fullUrl=urn:uuid:<UUID>、resource.id=同 UUID、request.url=<ResourceType>/<UUID>）；"
    "子资源（Encounter.subject/Condition/MedicationRequest 的 subject 与 encounter）引用 bundle 内父资源 UUID。"
    "该 Skill 的规范运行组件（子表查询 BO/聚合 BP）若尚未出现在 available_components，则只能按现有枚举表达"
    "可执行拓扑，不得自创 type；design_skill 决策仍如实保留。"
    "7. 若所选 Skill 标记 status=designed 且当前注册表无法完整表达其规范组件，如实保留 design_skill 决策，"
    "不得把未注册/未实现组件伪装成已实现（由平台侧 Skill 接入后补齐）。"
    "8. 仅当 design_skill=sql2fhir-patient-tx 时，顶层额外输出 \"generated_bp\"：{\"class_name\":\"demo.SqlFhirPatientTxProcess\","
    "\"source\":\"<完整可编译 ObjectScript 类源码>\"}——该 BP 是患者事务聚合控制中心，**类源码由你（Agent）生成**，"
    "平台不提供任何预置 BP。其职责与实现规范："
    "a) demo.SqlFhirPatientTxProcess **Extends demo.TransformProcess**"
    "（必须继承平台基类：`..BuildFHIRResource` / `..TermCoding` 在其中，术语转换由平台**共享 BO** 承载），"
    "实现 Method OnRequest(request As %Library.Persistent,"
    "Output response As %Library.Persistent) As %Status 与空 OnResponse；"
    "b) 布局在 IRIS：**优先按 BP 实例名读** ^demo.Config(\"sql2fhir\",\"layout\",..%ConfigName)"
    "（平台按本管道写入；多管道并存时每个聚合 BP 实例各有一份布局，只读全局键会与别的管道串线），"
    "取不到再回退全局 ^demo.Config(\"sql2fhir\",\"layout\")；推荐写法（照抄）："
    "Set tBP=..%ConfigName Set tLayoutStr=$Get(^demo.Config(\"sql2fhir\",\"layout\",tBP)) "
    "If tLayoutStr=\"\" { Set tLayoutStr=$Get(^demo.Config(\"sql2fhir\",\"layout\")) }；"
    "布局字段：patient_id_col / query_bos[](bo_name/source_table/fk_col/depth/depends_on/"
    "target_resource/**mapping_id**) / bundle.entries[](source_table/target_resource/mapping_id) / bundle.refs[](target_resource/parent_resource/"
    "field)；**每个子资源的字段映射必须按 `query_bos[i].mapping_id` 取**"
    "（`Set tFms=..GetMappingFms(tQB.%Get(\"mapping_id\"))`；平台已为 query_bos 与 bundle.entries 同时注入同一个 id）。"
    "取不到就是空映射 → 转换行是 `{}` → 子资源只带 resourceType/id → FHIR 报 "
    "`MissingRequiredProperty`（如 Encounter 缺 class/status）→ **整个 Bundle 事务回滚（Patient 也不落地）**；"
    "**query_bos[].depends_on 是父「源表名」**（depth=1 为 \"Patient\"）：取父行主键集合时，"
    "若没有维护该名称对应的 ID 集合（根表 Patient 即如此，患者主键只存在于上一层集合），**必须回退用上一层 ID 集合**"
    "（tLevel(depth-1)）——否则子表查询会被整体跳过，导致只有 Patient、零子资源；"
    "**层级记账必须与 depth 对齐**：患者层 = **level 0**（`Set tLevelCnt(0)=1`、`Set tLevelIds(0,0)=患者ID`、"
    "父层取 `tLevelCnt(depth-1)`）、depth=1 的子表（如 Encounter）产出记到 **level 1**、depth=2 记到 level 2；"
    "实测 2026-09-18 缺陷：把患者写成 `tLevelCnt(1)`（整体上移一层）→ depth=1 的 Encounter 取 `tLevelCnt(0)`=0 "
    "**被整体跳过**，depth=2 的 Diagnosis/MedicationOrder 又把患者 ID 当 EncounterID 查询 → 0 行，"
    "结果 Bundle 只有 Patient 一条 entry，**消息全 Completed、零错误**（静默失败）；"
    "c) 患者行=..UnpackSource(request)；患者 UUID=SHA1('Patient:'_患者ID) 取32hex分8-4-4-4-12；"
    "d) 子表查询：向 bo_name 组件（现成 EnsLib.SQL.Operation.GenericOperation）发送 Ens.StreamContainer，"
    "其 Stream 为 JSON {fk_col:父key值}（depth=1 用患者ID、depth=2 先得 Encounter 行 UUID 再用其 ID）；"
    "响应 Stream 为 JSON 行集，实测 body 与官方文档口径一致：**一个序号键对象** "
    "{\"1\":{列名:值},\"2\":{列名:值},…}（键为字符串、从 1 起，非数组；行内值为字符串；"
    "NULL 表现为 \"\\u0000\"），按该事实逐行处理（遍历 API 见 h）；"
    "e) 每行资源组装：mapping=^demo.Mapping(mapping_id) 的 field_mappings；转换="
    "##class(demo.FHIRTransformHelper).TransformResource(行JSON, fmsJSON)；组装="
    "**..BuildFHIRResource(target_resource, 转换后JSON, 行JSON, fmsJSON)**"
    "（继承自 demo.TransformProcess 的实例方法，故必须用 `..`；**术语转换无需你实现**——"
    "`field_mappings[].transform=term_map:<skill>` 的列由平台共享 BO 自动追加目标体系 coding，"
    "服务器尚无判定时平台降级为「保留源 coding + meta.tag 打 unmapped」，不阻断、不静默）；"
    "f) 资源 id/fullUrl=确定性 UUID；request={method:PUT,url:Resource/UUID}；"
    "g) 引用按 layout.bundle.refs 注入：field={\"reference\":\"urn:uuid:父UUID\"}（Encounter 父UUID="
    "确定性UUID('Encounter:'_EncounterID 列值)；子资源父=所在就诊的 UUID）；"
    "**引用清单必须从 bundle 取**：`Set tBundle=tLayout.%Get(\"bundle\")` 再 `tBundle.%Get(\"refs\")`；"
    "**禁止** `tLayout.%Get(\"refs\")`（顶层没有该键；再配 `$IsObject` 兜底成空数组会让引用注入被**静默跳过**，"
    "Bundle 里 subject/encounter 仍是裸源键 → FHIR 报 `<HSFHIRErr>MalformedRelativeReference`，实测 2026-09-18）；"
    "`refs` 取不到时应直接 `Quit $$$ERROR(...)` 显式失败，不得用空数组兜底；"
    "h) Bundle: resourceType=Bundle type=transaction entry=[...]；POST 用 EnsLib.HTTP.GenericMessage："
    "**body 必须是 UTF-8 字节流**——GenericMessage 的 body 是 %RawString（原样字节），若写字符流会在发送时"
    "逐字符按单字节输出，**中文会变成 \"?\"**；正确写法：Set tS=##class(%Stream.GlobalBinary).%New() "
    "Do tS.Write($ZCONVERT(tJSON,\"O\",\"UTF8\")) Set tMsg.Stream=tS（**禁止** %Stream.GlobalCharacter / "
    "%GlobalCharacterStream 作为 HTTP body）；HTTPHeaders：HTTPRequest=POST、URL=^demo.Config(\"fhir\",\"base_path\")、"
    "Content-Type/Accept=application/fhir+json、Authorization=Basic base64(user:pass)（user/pass 取自 "
    "^demo.Config(\"fhir\",\"username\"/\"password\")）；**发往 layout.http_bo 给出的 Operation 主机名**"
    "（多管道并存时每个 sql2fhir 组各有自己的 FHIR Operation 实例，布局里就是它的实际名；"
    "读法与布局同源：`Set tOp=tLayout.%Get(\"http_bo\")`，仅当为空时才回落 "
    "^demo.Config(\"fhir\",\"operation\")——按全局键派发会把 Bundle 发给**别的管道**的 Operation）。"
    "9. generated_bp.source 只允许类 demo.SqlFhirPatientTxProcess（Extends demo.TransformProcess）；"
    "禁止 xecute/$zf/^Ens 直接 global 写/Web Application 操作/读密钥；禁止输出类源码之外的其它代码。"
    "10. existing_pipelines 是平台已登记的数据管道事实（id/category/design_skill/组件/状态）："
    "同一 (源,目标) 的管道由平台**更新**而非新增，组件 Category 自动写为该管道的 design_skill，"
    "源 BS 同名跨管道时平台按类别改名（{原名}__{类别}，与组顺序无关）。请据此判断本次是新增管道还是"
    "复用/扩展既有管道，并在 pipeline.note 中说明（决策仍由你作出，平台只提供事实）。"
    "ObjectScript 硬约束（违反会编译失败）：a) **禁止在 TRY/CATCH 块内使用带参数 QUIT**（#1043；"
    "实测 2026-09-18 复发：`Try { … Quit tSC … }` 直接编译失败，白烧修复轮次）——"
    "方法返回值一律用方法体**顶层** Quit（Try 内只置变量/记日志，不做控制流转移）；"
    "a2) 形参/返回值类型必须是**真实存在**的类（`%Library.Persistent` / `%Library.DynamicObject` / "
    "`%Library.DynamicArray` / `%Stream.GlobalCharacter` / `%String` / `%Integer` 等）："
    "`%Library.Object` **不存在**（实测 2026-09-18 编译报 #5373 Class '%Library.Object' … does not exist）；"
    "b) 工具函数（取值/查找等）写 ClassMethod，"
    "方法体顶层 Quit 值合法；c) 禁止 ##class(EnsLib.HTTP.Headers).%New()——HTTP 头一律用 "
    "msg.HTTPHeaders.SetAt(值, 头名)；d) SendBundle 必须校验同步响应（HTTPHeaders 的 \"Status\" 非 2xx "
    "或响应文本含 OperationOutcome 时，返回 $$$ERROR 并在消息中可见）；e) 禁止在 FOR 循环体内用带参数 "
    "QUIT 返回方法值（需要返回时先置顶层变量，循环外统一 Quit）；f) 调用 "
    "demo.FHIRTransformHelper.TransformResource / ..BuildFHIRResource 时"
    "**必须传 JSON 字符串**（用 行对象.%ToJSON()），禁止直接传 %DynamicObject（会触发 Python TypeError）；"
    "**两者都返回 JSON 字符串**（BuildFHIRResource 返回类型是 %String）——返回值当对象用时必须 "
    "`Set tRes=##class(%DynamicObject).%FromJSON(返回值)` 且先用 `$IsObject` 判断，"
    "否则 `Do tRes.%Set(...)` 会报 INVALID OREF；"
    "读取 ^demo.Config(\"sql2fhir\",\"layout\",..%ConfigName)（缺省回退 ^demo.Config(\"sql2fhir\",\"layout\")）与 ^demo.Mapping 后须 %FromJSON 再取值，写响应/请求头用字符串；"
    "g) 调用 ..SendRequestSync / ..SendRequestAsync 的方法**必须声明为 Method（实例方法）**，"
    "不得放在 ClassMethod 内（否则 MPP5377）；纯工具函数（UUID/取值/查找/组装 entry）才用 ClassMethod；"
    "h) 访问 %DynamicObject **必须用 %Get(\"key\") / %Set(\"key\",值) / %Size() / %Get(n)**，"
    "**枚举一个 JSON 对象的全部键用 %GetIterator() 取迭代器 + it.%GetNext(.k,.v)**"
    "（数组用 %Size()/%Get(i)，i 从 0 起）；"
    "事实：%Library.DynamicObject / %DynamicArray 的方法清单里**没有 %Next()、没有 %GetData()**——"
    "那两个属于结果集 API（%SQL.StatementResult / %SQL.IResultSet / EnsLib.SQL.Snapshot），"
    "而 BP 从查询 BO 只拿得到 Ens.StreamContainer（Stream 为 %Stream.Object 字符流），拿不到结果集；"
    "在动态对象上写 %Next(tKey) 编译不报错、运行期报 "
    "<METHOD DOES NOT EXIST>...*%Next,%Library.DynamicObject；"
    "**禁止点号属性访问**（如 layout.patient_id_col、patient.ID 会 #UNDEFINED）；"
    "**也禁止多维下标访问**（如 layout(\"patient_id_col\")、obj(\"k\") 会报 INVALID CLASS ... does not support "
    "MultiDimensional）；数组用 %Get(i) 且下标从 0 起；"
    "i) **JSON 字面量 {…}/[…] 只能包含常量**，含变量时必须用 "
    "##class(%DynamicArray).%New() + %Push(值) 或 ##class(%DynamicObject).%New() + %Set(\"k\",值) 构造"
    "（例如 Set vals=[pid] 会报 #1033 Expected literal）；"
    "j) **..UnpackSource(request) 返回 JSON 字符串**（不是对象）——"
    "必须先 `Set row=##class(%DynamicObject).%FromJSON(tSrc)` 再 %Get；"
    "^demo.Config/^demo.Mapping 的取值同样是字符串，需 %FromJSON；禁止对字符串用 %Get；"
    "k) 确定性 UUID 必须是**可打印十六进制文本**：对种子取 SHA1 后先转十六进制"
    "（`$ZCONVERT(hash,\"O\",\"H\")` 或逐字节 `$ZHEX(...)`；也可 `$ZCONVERT(hash,\"O\",\"H\")` 得大写再 $ZCVT(...,\"L\")），"
    "取前 32 位按 8-4-4-4-12 分段；**禁止把二进制哈希字节直接拼入字符串**（会产生控制字符 → Bundle 非法 JSON）；"
    "l) SendBundle 的响应校验必须真实读取响应体：HTTP 状态取 HTTPHeaders 的 \"Status\"；响应流属性是 "
    "$PROPERTY(tResp,\"Stream\")（EnsLib.HTTP.GenericMessage 的响应体就在 Stream）；"
    "**取不存在的属性会抛异常而不是返回空**，因此每次取属性都必须**各自 Try/Catch 包裹**后再判断 $IsObject；"
    "（读前 Rewind）；**响应体为空或读取失败一律视为失败**（返回 $$$ERROR），"
    "并把响应原文写入 ^demo.Trace(\"bp_resp\",患者UUID) 便于诊断；"
    "m) global 赋值必须用 `Set ^global(sub)=值` 语句；**$Set 只能作为表达式取值，不能 `Do $Set(...)`**"
    "（会报 #1026 Invalid command）；"
    "n) **demo.FHIRTransformHelper.TransformResource(行JSON, fmsJSON) 与 "
    "..BuildFHIRResource(资源类型, 转换后JSON, 行JSON, fmsJSON) 的 fms 参数必须是 "
    "field_mappings 数组 JSON**：fmsJSON = ##class(%DynamicObject).%FromJSON(^demo.Mapping(id)).%Get(\"field_mappings\").%ToJSON()；"
    "禁止把整个 mapping 对象 JSON 当 fms 传入（会导致 Python 'str' object has no attribute 'get'）；"
    "o) **While/If 的条件必须用圆括号**（`While (cond) { … }`、`If (cond) { … }`），"
    "省略括号会报 #1026 Invalid command（如 `While $Length(tHex)<32 …`）；"
    "p) `$System.Encryption.SHA1Hash(seed)` **返回的已经是 40 位十六进制字符串**——直接取前 32 位"
    "（必要时 $ZCVT(h,\"L\") 转小写）再分段即可；**禁止再对它做 $ZCONVERT(h,\"O\",\"H\")**（文本非流，会报 ILLEGAL VALUE）；"
    "q) 确定性 UUID **必须照此实现**（可直接套用；注意 $System.Encryption.SHA1Hash 返回 **20 字节二进制**，"
    "必须先逐字节转十六进制文本）：\n"
    "    ClassMethod MakeUUID(pSeed As %String) As %String\n"
    "    {\n"
    "        Set h=$System.Encryption.SHA1Hash(pSeed)\n"
    "        Set hex=\"\",chars=\"0123456789abcdef\"\n"
    "        For i=1:1:$LENGTH(h) {\n"
    "            Set b=$ASCII(h,i)\n"
    "            Set hex=hex_$EXTRACT(chars,(b\\16)+1)_$EXTRACT(chars,(b#16)+1)\n"
    "        }\n"
    "        Quit $EXTRACT(hex,1,8)_\"-\"_$EXTRACT(hex,9,12)_\"-\"_$EXTRACT(hex,13,16)_\"-\"_$EXTRACT(hex,17,20)_\"-\"_$EXTRACT(hex,21,32)\n"
    "    }"
    "r) 响应校验**必须基于响应体结构**（响应体在 EnsLib.HTTP.GenericMessage 的 Stream 属性；HTTP 的 \"Status\" 头可能为空，不要依赖）："
    "1) tB 为空 → 失败；2) tB 含 \"OperationOutcome\" → 失败（截取 diagnostics）；"
    "3) 否则 %FromJSON 解析：若为 transaction-response，逐 entry 取 response.status，全部以 \"2\" 开头才算成功，"
    "任一非 2xx → 失败并带上该 status/location（取子对象分步赋值，避免链式调用）；"
    "4) 无论成败都把 tB 写入 ^demo.Trace(\"bp_resp\",患者UUID)。参考实现：\n"
    "    Set tB=\"\"\n"
    "    Try { Set tRS=$PROPERTY(tResp,\"Stream\") If $IsObject(tRS) { Do tRS.Rewind() Set tB=tRS.Read() } } Catch e { Set tB=\"\" }\n"
    "    Set ^demo.Trace(\"bp_resp\",pPatientUUID)=tB\n"
    "    If (tB=\"\") Quit $$$ERROR($$$GeneralError,\"FHIR empty response\")\n"
    "    If (tB[\"OperationOutcome\") Quit $$$ERROR($$$GeneralError,\"FHIR rejected: \"_$EXTRACT(tB,1,400))\n"
    "    Set tObj=##class(%DynamicObject).%FromJSON(tB),tArr=tObj.%Get(\"entry\"),tOk=1,tBad=\"\",i=0\n"
    "    If $IsObject(tArr) { While (i<tArr.%Size()) { Set tOne=tArr.%Get(i),tRO=tOne.%Get(\"response\"),tSt=tRO.%Get(\"status\") If ($EXTRACT(tSt,1,1)'=\"2\") { Set tOk=0 Set tBad=tSt_\" \"_tRO.%Get(\"location\") } Set i=i+1 } }\n"
    "    If ('tOk) Quit $$$ERROR($$$GeneralError,\"FHIR entry failed: \"_tBad)"
    "r2) **OnRequest/方法的 `Output response` 必须是持久消息类**（本环境实测）：可写 "
    "`Set response=##class(Ens.Response).%New()`（Ens.Response 是持久消息类）或自定义 %Persistent 消息类；"
    "**绝不能** `Set response=##class(%DynamicObject).%New()` 或把 %Stream/%DynamicArray 赋给它——"
    "框架收尾时（Ens.BusinessProcess.%responseGet）会调 %OpenId 并报 "
    "`<METHOD DOES NOT EXIST>%OpenId,%Library.DynamicObject` → `ErrBPTerminated`，"
    "**入站消息被标 Error（而 Bundle 其实已成功投递、目标数据已落地）**，会被误判为管道失败。"
    "摘要信息写 `$$$LOGINFO`，或放进 Ens.Response 子类的持久属性。\n"
    "s) **不要定义 OnResponse 方法**（Ens.BusinessProcess 父类已实现；本 BP 用 SendRequestSync 同步调用，无需覆盖）。"
    "如确要覆盖，必须使用完整签名：Method OnResponse(request As %Library.Persistent, ByRef response As %Library.Persistent, "
    "callrequest As %Library.Persistent, callresponse As %Library.Persistent, pCompletionKey As %String) As %Status "
    "（否则报 #5478 Keyword signature error）；"
    "t) **修复模式（当 user_content.bp_patch_request=true 时）**：generated_bp 只输出 "
    "{\"class_name\":\"demo.SqlFhirPatientTxProcess\",\"method_updates\":[{\"name\":\"方法名\","
    "\"source\":\"该方法的完整源码（含 Method/ClassMethod 声明与结束花括号）\"}]}——只包含需要修复的方法，"
    "未列出的方法由平台保持原样；**不要输出整类 source**；每个方法的 source 必须自身可编译。"
    "非 sql2fhir-patient-tx 场景不要输出 generated_bp。"
    "u) **ObjectScript 空值与数值比较事实（本环境实测；违反会静默少查数据且不报错）**："
    "$Get(未定义节点) 返回 \"\"（空串）；`(\"\"=0)`、`(0<\"\")`、`(\"\"<0)`、`(0=\"\")` 全部为 **FALSE**"
    "（任一侧是空串时按**字符串比较**），而 `(+\"\"=0)` 为 TRUE。所以：① 判空写 `If (tX=\"\")`；"
    "② 参与数值比较/算术前必须显式转数字 `Set tN=+$Get(节点)`（或 `$Get(节点,0)` 后 `Set tN=+tN`）；"
    "③ **禁止** `If (tCnt=0)` / `While (i<tCnt)` 这类未先转数字的空值比较——空值会让分支/循环"
    "**一次都不进**且无任何错误（表现为\\\"子表 0 行 / Bundle 缺资源\\\"）；正例（父级计数兜底按此写）：\\n"
    "    Set tCnt=+$Get(tIdsCnt(tParentTable)) If (tCnt=0) { Set tCnt=+$Get(tLevelCnt(tDepth-1)) }\\n"
    "    Set i=0 While (i<tCnt) { Set tKey=$Get(tLevelIds(tDepth-1,i)) … Set i=i+1 }\\n"
    "v) **子查询/发送失败禁止静默吞错**：每次 ..SendRequestSync/..SendRequestAsync 之后必须"
    "`If ($$$ISERR(tSC)) { $$$LOGERROR(\"QueryChild 失败 \"_tQBName_\": \"_$System.Status.GetErrorText(tSC)) Quit tSC }`"
    "（或在 OnRequest 顶层 `Quit $$$ERROR(...)`）；**禁止** `Set tBody=\"\"` 之类的吞错写法"
    "（空结果与失败必须可区分，失败要么上抛、要么至少写 `^demo.Trace` 与事件日志）；"
    "发 Bundle 前核对 `layout.bundle.entries` 声明的资源是否都已产出，缺失时 `$$$LOGWARNING`"
    "（勿静默返回成功）；"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"design_skill":"fhir2db","pipeline":{"components":[{"type":"FHIRSyncService","name":"FHIRSyncService"},'
    '{"type":"FHIRService","name":"FHIRService"},'
    '{"type":"TransformProcess","name":"TransformProcess"},'
    '{"type":"SQLOperation","name":"SQLOp_patient","table":"Patient"},'
    '{"type":"JavaGateway","name":"EnsLib.JavaGateway.Service"}]}}'
)


# ===== Plan 层（2026-09-19）：先出"怎么做"的计划（小输出），再按方法逐个生成 =====
# 背景：整类 BP 生成 ≈ 34.5k completion（含大量模型推理 token）、单次 30~60 分钟且频繁 timeout。
# 拆成 plan（≤4k token）+ 每方法一次小调用（≤8k）后，单次响应小、可编译可单测、可断点续跑。
SYSTEM_PROMPT_BP_PLAN = (
    "你是 IRIS Ensemble 数据管道编排规划师。给定**布局契约**（layout：query_bos / bundle.entries / bundle.refs / "
    "http_bo）、字段映射（含 term_map 决策）、管道组件清单、以及**父类 demo.TransformProcess 已提供的稳定 API 清单**，"
    "为聚合 BP（继承父类）产出**执行计划**。**只输出计划，不要写任何代码**。要求："
    "1. methods 只列**必要**方法（≤8 个）且必须含入口 OnRequest；每个方法职责**单一**（便于逐个生成与验证）；"
    "2. 每个方法给 purpose（一句话）、steps（≤6 条要点）、verification（compile/unit/runtime 子集）；"
    "**每个方法保持短小（建议 ≤40 行 ObjectScript）**：大逻辑继续拆成更多小方法（总数 ≤8），"
    "便于逐个生成与验证（实测：单方法输出过大时模型会输出超长内容并被截断）；"
    "3. calls_parent **只能**引用给定父类 API 清单里的方法名（不许发明）；calls_self 只能引用本计划的方法名；"
    "4. 计划必须说明**如何读布局的关键键**：query_bos[].mapping_id（子资源字段映射）、bundle.entries[].mapping_id、"
    "bundle.refs（在 bundle 里，顶层没有）、http_bo（本管道 FHIR Operation 的实例名）；"
    "5. **签名里的类型必须是 IRIS 类型**（`%String`/`%Integer`/`%Boolean`/`%Library.DynamicObject`/"
    "`%Library.Persistent`/`%Status`/`%Library.DynamicArray`…）：**不要**写 `String`/`Integer`/`Object` 这类"
    "非 IRIS 名（编译会报 `#5500 method formal argument type … is invalid`）；"
    "6. 对**纯函数**方法给 `unit` 样例：{args:[...], expect_contains:\"...\"}（`args` 传字符串/JSON 文本）；"
    "样例必须与方法行为**一致**（平台会用该样例真实调用并断言；不一致会被判失败）；"
    "**只在显而易见、能确定返回值时**给样例，不确定就 null；"
    "**可独立验证的纯逻辑请单独列为 ClassMethod 方法**（无实例依赖），便于平台在生成期直接单测；"
    "依赖业务主机上下文（SendRequestSync/%Process）的方法 unit 给 null；"
    "**术语判码不要自己实现**（父类经共享 BO `demo.TerminologyOperation` 处理，Plan 里说明『交给父类』即可）；"
    "7. 给出 verification 总表、risks（≤3 条）、fallback（失败时怎么办，例如『整类生成（显式标注）』）。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"plan":{"schema":1,"class_name":"demo.SqlFhirPatientTxProcess","parent":"demo.TransformProcess",'
    '"summary":"…","methods":[{"name":"OnRequest","signature":"Method OnRequest(request As %Library.Persistent, '
    'Output response As %Library.Persistent) As %Status","purpose":"入口：…","steps":["…"],'
    '"calls_parent":["GetMappingFms"],"calls_self":["ProcessPatient"],"verification":["compile","runtime"],'
    '"unit":null}],"verification":[{"step":"compile","tool":"OBJ.Load","expect":"ok"}],'
    '"risks":["…"],"fallback":"整类生成（显式标注）"}}'
)

# 单方法生成的事实块（今天/前几天实测踩过的坑，全部为**事实**约束）
BP_METHOD_FACTS = (
    "1) 只输出**一个方法**的完整源码（含方法头与花括号），不要类头/其它方法/Markdown 代码围栏；",
    "2) 调用父类能力用 `..Foo(...)`（如 `..GetMappingFms(id)`/`..BuildFHIRResource(...)`/`..TermCoding(...)`）；"
    "**禁止** `##class(demo.TransformProcess).Foo(...)` 静态调用实例方法；",
    "3) `Try { … }` 块内**禁止带参数 Quit**（编译报 #1043）：块内只赋值，返回值在方法体顶层 `Quit tSC`；",
    "4) `Output response` 必须是 `%Library.Persistent`（如 `Set response=##class(Ens.Response).%New()`）；"
    "**禁止**把 `%DynamicObject`/`%Stream` 赋给它（运行期 ErrBPTerminated）；",
    "5) 只调用**存在**的助手：父类 API 清单里的方法，或本类计划里的方法（否则编译 MPP5376）；",
    "6) `$Get()` 未定义返回 \"\"，与 0 比较必须显式归一：`Set tCnt=+$Get(x)`；判空用 `= \"\"`（`(\"\"=0)` 为 FALSE）；",
    "7) 子表查询/HTTP 发送失败**不许静默**：`If $$$ISERR(tSC) { $$$LOGERROR($System.Status.GetErrorText(tSC)) ... }` 或上抛；",
    "8) 引用注入取 `Set tBundle=tLayout.%Get(\"bundle\")` 再 `tBundle.%Get(\"refs\")`（顶层没有 refs），取不到要显式失败；",
    "9) 布局里的 `query_bos[].mapping_id` / `bundle.entries[].mapping_id` / `http_bo` 是**平台注入的实例级事实**，"
    "必须读它们（别按名字拼装、别读全局兜底键）；读不到要显式失败；",
    "10) 术语双 coding 由父类 `..BuildFHIRResource` + 共享 BO 完成，本方法**不要**自己查术语；",
    "11) **只实现这一步计划里的方法**，保持短小（建议 ≤60 行 ObjectScript）：把循环/组装拆到计划里的"
    "其它方法；不要把别的方法的逻辑塞进来（输出过大易被截断、也会让后续步骤重复实现）；",
    "12) 给 `%DynamicObject`/`%DynamicArray` 设值必须用方法：`Do tObj.%Set(\"key\", v)`、`Do tArr.%Push(v)`、"
    "`Do tArr.%Set(idx, v)`；**禁止** `Set tObj.key=v` / `Set tArr.idx=v`（编译报 `#1027 Error in SET command`）；"
    "取值 `tObj.%Get(\"key\")`、`tArr.%Get(idx)`；",
    "13) 字符串拼接用 `_`（`Set tS=tA_\"-\"_tB`）；不要把 `+` 当拼接；比较用 `=`，不等用 `'=`；"
    "**后置条件不能带空格**：写 `Quit:x=\"\"`（能编译），写 `Quit:x = \"\"` 会报 `#1054 Invalid expression : '='`；",
    "14) 若你确认**计划里的 unit 样例写错了**（与方法正确行为不符），可在返回 JSON 里额外给 "
    "`unit_fix:{\"args\":[...],\"expect_contains\":\"…\"}`：平台会采纳新样例后重测（**别为了让断言过而"
    "把方法改成错误实现**）；",
)

SYSTEM_PROMPT_BP_METHOD = (
    "你是 IRIS ObjectScript 工程师。按给定**计划**只生成**一个方法**的源码。硬性事实（必须遵守）："
    + "".join(BP_METHOD_FACTS) +
    "输出严格 JSON（不要输出其他文字），格式："
    '{"method":{"name":"<方法名>","source":"Method <方法名>(<参数>) As <返回类型>\\n{\\n    …\\n}"}}'
)


# ===== 公共调用逻辑（token 日志 + 一次重试） =====
def _call_llm(system_prompt: str, user_content: str, agent_name: str,
              max_tokens: int | None = None) -> dict:
    """调用 LLM 一次并解析 JSON 返回。

    超时策略（缺陷 N9 修复）：单次读超时 + 墙钟总预算双重兜底。
    实测一次生成请求在上游"连接存活但长时间不吐数据"时挂了 4.5 小时，
    最后只回 "Connection error."，页面全程无反馈；现在最坏情况会被
    总预算截断为可解释的失败。
    """
    if not LLMConfig.API_KEY or LLMConfig.API_KEY.startswith("sk-xxxx"):
        raise AgentError("未配置 LLM_API_KEY，请在 .env 中设置（OpenAI 兼容服务）")
    client = OpenAI(
        base_url=LLMConfig.BASE_URL,
        api_key=LLMConfig.API_KEY,
        timeout=Timeout(LLMConfig.TIMEOUT, connect=LLMConfig.CONNECT_TIMEOUT),
        max_retries=LLMConfig.MAX_RETRIES,
    )
    deadline = time.monotonic() + LLMConfig.TOTAL_TIMEOUT
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 5:
            raise AgentError(
                f"AI 调用超出总预算 {LLMConfig.TOTAL_TIMEOUT:.0f}s（上游长时间无响应），请稍后重试")
        per_attempt = min(LLMConfig.TIMEOUT, remaining)
        started = time.monotonic()
        logger.info("[%s] 调用 LLM（第 %d 次，单次读超时 %.0fs，剩余预算 %.0fs）",
                    agent_name, attempt + 1, per_attempt, remaining)
        try:
            resp = client.with_options(
                timeout=Timeout(per_attempt, connect=LLMConfig.CONNECT_TIMEOUT)
            ).chat.completions.create(
                model=LLMConfig.MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=max_tokens or LLMConfig.MAX_TOKENS,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                logger.info("[%s] token 用量: prompt=%s completion=%s total=%s",
                            agent_name, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
            # 截断即显式失败（长输出被 max_tokens 截断会产出不完整内容/坏 JSON）
            finish = getattr(resp.choices[0], "finish_reason", "") or ""
            if finish == "length":
                raise AgentError(
                    f"LLM 输出被截断（finish_reason=length，completion={getattr(usage, 'completion_tokens', '?')}）；"
                    f"请提高 LLM_MAX_TOKENS 或缩短输出后重试")
            text = resp.choices[0].message.content or ""
            return json.loads(text)
        except json.JSONDecodeError as exc:
            if attempt == 0:
                logger.warning("[%s] LLM 返回非 JSON（耗时 %.1fs），重试一次: %s",
                               agent_name, time.monotonic() - started, exc)
                continue
            raise AgentError(f"LLM 返回内容解析失败: {exc}") from exc
        except Exception as exc:
            elapsed = time.monotonic() - started
            # 截断属确定性失败（同一请求再试仍会截断），不重试以省成本
            if isinstance(exc, AgentError) and "截断" in str(exc):
                raise
            if attempt == 0:
                logger.warning("[%s] LLM 调用失败（耗时 %.1fs），重试一次: %s", agent_name, elapsed, exc)
                continue
            raise AgentError(
                f"AI 调用失败（耗时 {elapsed:.0f}s，{type(exc).__name__}: {exc}）；"
                f"单次读超时 {LLMConfig.TIMEOUT:.0f}s、总预算 {LLMConfig.TOTAL_TIMEOUT:.0f}s，请稍后重试"
            ) from exc
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
    from backend.services.agents import AGENTS
    term_skills = []
    for a in AGENTS:
        if a.get("term_id"):
            term_skills.append({
                "term_id": a["term_id"],
                "name": a.get("name", ""),
                "role": a.get("role", ""),
                "input": a.get("input", ""),
                "output": a.get("output", ""),
                "trigger": a.get("trigger", ""),
            })
    user_content = json.dumps(
        {"assets": assets, "targets": targets,
         "source_models": source_models or assets,
         "target_models": target_models or targets,
         # 可用判定 Skill 目录（agents.py 注册表单一事实源；term_map:<term_id> 引用）
         "term_skills": term_skills,
         # 受控转换指令目录（transform_directives 注册表单一事实源）
         "transform_directives": transform_directives.list_directives()},
        ensure_ascii=False, indent=2)
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
                       target_runtime: dict | None = None,
                       existing_pipelines: list[dict] | None = None,
                       bp_compile_feedback: str | None = None,
                       bp_patch_mode: bool = False,
                       bp_plan_mode: bool = False) -> dict:
    """数据管道设计 Agent（规划师）：选 Skill + 生成组件拓扑。

    `bp_plan_mode=True`（2026-09-19 Plan 层）：**只出拓扑**——明确要求**不要输出整份
    `generated_bp` 源码**（平台随后用"计划 + 逐方法生成"产出聚合 BP）。动机：整类源码生成
    ≈34.5k completion（含模型推理 token）、单次 30~60 分钟且频繁 timeout。
    """
    # 注入当前 (源,目标) 可匹配的管道设计 Skill 目录（id/status/rules/bo_contract），供 planner 选型决策
    matched_skills = [
        {"id": s["id"], "name": s.get("name", ""), "status": s.get("status", ""),
         "applies_to": s.get("applies_to", {}), "rules": s.get("rules", []),
         "bo_contract": s.get("bo_contract", {})}
        for s in pipeline_design_skills.match_design_skills(source_type, target_type)
    ]
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
            "design_skills": matched_skills,
            # 平台已登记的管道事实（id/category/组件/状态）：供规划时判断新增 vs 复用既有管道
            "existing_pipelines": existing_pipelines or [],
            "bp_compile_feedback": bp_compile_feedback or "",
            "bp_patch_request": bool(bp_patch_mode),
            # Plan 层：本次**不要**输出整份 BP 源码（平台按计划逐方法生成）——避免 34.5k completion 大调用
            "bp_plan_request": bool(bp_plan_mode),
        }, ensure_ascii=False, indent=2)
    prompt = SYSTEM_PROMPT_PIPELINE
    if bp_plan_mode:
        prompt = (SYSTEM_PROMPT_PIPELINE
                  + "【本次特别要求（Plan 层）】只输出 design_skill 与 pipeline.components；"
                  "**不要**输出 generated_bp / method_updates 源码——平台会先用计划（方法编排）"
                  "再逐个方法生成聚合 BP。输出 JSON 里不要包含 generated_bp 字段。")
    result = _call_llm(prompt, user_content, "数据管道设计Agent")
    pipeline = result.get("pipeline")
    if not isinstance(pipeline, dict):
        raise AgentError("pipeline 必须是对象")
    return result


# ===== Plan 层入口（小输出：计划 / 单方法） =====
def plan_bp_source(*, layout: dict, mappings: list[dict], components: list[dict],
                   design_skill: str, bp_class: str, parent_api: list[str],
                   facts: dict | None = None, max_tokens: int = 4000) -> dict:
    """Plan 层：请 Agent **只产出计划**（方法编排/调用关系/验证策略），不写代码。

    为什么：整类生成 ≈34.5k completion（含模型推理 token）→ 单次 30~60 分钟、易 timeout；
    计划输出 ≤4k token，落库可审计，并为"逐方法生成 + 断点续跑"提供依据。
    失败抛 `AgentError`（显式，不静默）。
    """
    user_content = json.dumps({
        "design_skill": design_skill,
        "bp_class": bp_class,
        "parent_class_api": parent_api,
        "layout": layout,
        "mappings": mappings,
        "components": components,
        "facts": facts or {},
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_BP_PLAN, user_content, "数据管道设计Agent(计划)",
                       max_tokens=max_tokens)
    if not isinstance(result.get("plan"), dict):
        raise AgentError("BP 计划输出不合规：缺 plan 对象")
    return result


def generate_bp_method(*, plan_step: dict, bp_class: str, skeleton: str, layout: dict,
                       parent_api: list[str], facts: dict | None = None,
                       feedback: str = "", max_tokens: int = 16000) -> dict:
    """按计划生成**一个方法**的源码（小输出）。返回 {"method":{"name","source"}}。

    参数:
        plan_step: 该方法的计划项（name/purpose/steps/calls_parent/calls_self/verification/unit）
        skeleton: 已生成方法的**签名清单**（避免重复定义、便于互相调用）
        feedback: 上一轮编译/单测失败原文（只针对这一步重写）
    """
    user_content = json.dumps({
        "bp_class": bp_class,
        "parent_class_api": parent_api,
        "plan": plan_step,
        "layout": layout,
        "class_skeleton": skeleton,
        "compile_feedback": feedback or "",
        "facts": facts or {},
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_BP_METHOD, user_content, "数据管道设计Agent(方法)",
                       max_tokens=max_tokens)
    m = result.get("method")
    if not isinstance(m, dict) or not str(m.get("source") or "").strip():
        raise AgentError("方法输出不合规：缺 method.source")
    return result
