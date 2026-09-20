Class demo.TransformProcess Extends Ens.BusinessProcess
{

/// 处理异步响应（SendRequestAsync 的响应回调）。
/// 本场景无需处理响应内容，空实现即可（否则异步响应会报 #5003 Not implemented）。
Method OnResponse(request As %Library.Persistent, Output response As %Library.Persistent) As %Status
{
    Quit $$$OK
}

/// 处理数据转换请求（多源路由 BP）：
/// 接收来自不同源 BS 的消息（FHIRRequest / SQL 行 StreamContainer 等），
/// 解包为源数据 → 字段映射转换 → 按目标类型打包并路由到对应 Operation。
/// 注意：Business Process 的入口方法是 OnRequest（不是 OnMessage）。
Method OnRequest(request As %Library.Persistent, Output response As %Library.Persistent) As %Status
{
    Set tSC = $$$OK
    Try {
        // 0. 解析消息来源（源 BS 名）并查本管道的转换参数
        //    ① 首选：**本 BP 自己的**配置 ^demo.Config("bp", <本BP名>)
        //       —— 一条数据管道一个 BP 实例（className 复用，Item 名不同），
        //          各读各的参数，管道之间零耦合（多管道并行不会再互相覆盖）。
        //    ② 兜底：旧的按源 BS 路由表 ^demo.Config("pipe", <源BS名>)（历史管道兼容）。
        Set tSource = ..ResolveSource(request)
        Set tPipeCfg = ""
        Set tOwnJson = $Get(^demo.Config("bp", ..%ConfigName), "")
        If tOwnJson '= "" Set tPipeCfg = ##class(%DynamicObject).%FromJSON(tOwnJson)
        If '$isobject(tPipeCfg) && (tSource '= "") {
            Set tPipeJson = $Get(^demo.Config("pipe", tSource), "")
            If tPipeJson '= "" Set tPipeCfg = ##class(%DynamicObject).%FromJSON(tPipeJson)
        }

        // 0. 派发目标（**显式优先**，见下）：多管道并存时目标 BO 会按管道实例改名
        //    （如 SQLOp_PatientSource → SQLOp_PatientSource__sql2db），此时"按约定拼名"
        //    会指向不存在的主机 → ErrBusinessDispatchNameNotRegistered、目标零落地而消息看似正常。
        //    生成端为此写入本 BP 自己的派发表：
        //      items = { <目标表名(DB) | 服务名(SOAP)>: <实际 Operation 主机名> }
        //      item  = <单一目标主机名>（FHIR 目标 / 单目标组兜底）
        //    缺省（历史管道）时回落到原有的按约定拼名行为，保持向后兼容。
        Set tDispItems = ""
        Set tDispOne = ""
        If $isobject(tPipeCfg) {
            Set tDispItems = tPipeCfg.%Get("items")
            Set tDispOne = tPipeCfg.%Get("item")
        }
        If '$isobject(tDispItems) Set tDispItems = ""
        If tDispOne = $CHAR(0) Set tDispOne = ""

        // 1. 确定转换关系 MappingId（消息自带 > 管道组 mapping > 激活映射兜底）
        Set tMappingId = ""
        If request.%IsA("demo.FHIRRequest") {
            Set tMappingId = request.MappingId
        }
        If (tMappingId = "") && $isobject(tPipeCfg) && (tPipeCfg.%Get("mapping") '= "") {
            Set tMappingId = tPipeCfg.%Get("mapping")
        }
        If tMappingId = "" {
            Set tMappingId = $Get(^demo.Config("pipeline", "active_mapping"), "")
        }
        Set tMapJson = $Get(^demo.Mapping(tMappingId))
        If tMapJson = "" {
            Set tSC = $$$ERROR($$$GeneralError, "转换关系不存在: " _ tMappingId)
            Quit
        }
        Set tMap = ##class(%DynamicObject).%FromJSON(tMapJson)
        Set tFieldMappingsJson = tMap.%Get("field_mappings").%ToJSON()

        // 2. 解包源消息 → 源数据 JSON 字符串（多源适配：FHIR 资源 / SQL 行）
        Set tSourceJson = ..UnpackSource(request)
        If tSourceJson = "" {
            Set tSC = $$$ERROR($$$GeneralError, "无法解包源消息: " _ $ClassName(request))
            Quit
        }

        // 3. 字段级转换（transform_handler 已泛化：支持 FHIR 资源 JSON / SQL 行）
        Set tRowJson = ##class(demo.FHIRTransformHelper).TransformResource(tSourceJson, tFieldMappingsJson)

        // 4. 按目标类型打包 + 路由
        //    优先级：管道组(源→目标) target_type > 全局管道 target_type > mapping.target_type
        //    （mapping.target_type 可能因前端未传默认 DB，故仅兜底）
        Set tTargetType = ""
        If $isobject(tPipeCfg) && (tPipeCfg.%Get("target_type") '= "") {
            Set tTargetType = tPipeCfg.%Get("target_type")
        }
        If tTargetType = "" Set tTargetType = $Get(^demo.Config("pipeline", "target_type"), "")
        If tTargetType = "" Set tTargetType = tMap.%Get("target_type")
        If tTargetType = "" Set tTargetType = "DB"
        If tTargetType = "SOAP" {
            // SOAP 目标：构造 BO 请求消息 → SOAPOp_{service}（消息适配见 PackSOAPRequest）
            Set tService = ""
            If $isobject(tPipeCfg) && (tPipeCfg.%Get("service") '= "") {
                Set tService = tPipeCfg.%Get("service")
            }
            If tService = "" Set tService = $Get(^demo.Config("soap", "active_service"), "default")
            Set tReq = ..PackSOAPRequest(tRowJson, tMap)
            If $isobject(tReq) {
                // 派发目标：显式派发表优先（多管道改名后按约定拼名会指向不存在的 SOAPOp_{service}）
                Set tSoapOp = ""
                If tDispItems '= "" {
                    Set tSoapOp = tDispItems.%Get(tService)
                    ; 键名可能带限定前缀（如 a.b.Service）→ 退末段再试一次
                    If (tSoapOp = "") && (tService '= "") {
                        Set tSoapOp = tDispItems.%Get($PIECE(tService, ".", $LENGTH(tService, ".")))
                    }
                }
                If tSoapOp = "" Set tSoapOp = "SOAPOp_" _ tService
                Do ..SendRequestAsync(tSoapOp, tReq)
            } Else {
                Set tSC = $$$ERROR($$$GeneralError, "SOAP 目标请求消息构造失败: " _ tMap.%Get("target_table"))
            }
        } Else {
            If tTargetType = "FHIR" {
                // FHIR 目标 = 标准 REST PUT（通用 HTTP Operation）：
                // ① 按 mapping.target_table 组装 US Core 资源 JSON（字段映射为 LLM 决策，
                //    这里只做结构序列化）；② 构造 EnsLib.HTTP.GenericMessage（方法/路径/头/体）；
                // ③ 投递到生成端写入的 ^demo.Config("fhir","operation") 对应的 HTTP Operation。
                Set tRT = tMap.%Get("target_table")
                Set tBody = ..BuildFHIRResource(tRT, tRowJson, tSourceJson, tFieldMappingsJson)
                If tBody = "" {
                    Set tSC = $$$ERROR($$$GeneralError, "FHIR 目标资源组装失败（当前支持已建模资源类型的结构化组装）: " _ tRT)
                } Else {
                    Set tMsg = ..PackFHIRHTTP(tBody, tRT, tSourceJson)
                    If $IsObject(tMsg) {
                        Set tOpName = tDispOne
                        If tOpName = "" Set tOpName = $Get(^demo.Config("fhir", "operation"), "FHIR_HTTP_Operation")
                        // **同步调用并检查 HTTP 回执**：EnsLib.HTTP.GenericOperation 对 4xx/5xx
                        // 不会自动返回错误状态 —— 若不同步检查，就会出现"消息 Completed 但资源
                        // 未落地"的静默失败（实测：URL 缺 id → PUT /Patient → 目标 0 行而全绿）。
                        Set tRsp = ""
                        Set tSendSC = ..SendRequestSync(tOpName, tMsg, .tRsp)
                        If $System.Status.IsError(tSendSC) {
                            Set tSC = tSendSC
                        } Else {
                            Set tLine = ""
                            Try {
                                If $IsObject(tRsp) Set tLine = tRsp.HTTPHeaders.GetAt("StatusLine")
                            } Catch exLine {
                                Set tLine = ""
                            }
                            ; 状态码取状态行第 2 段（"HTTP/1.1 201 Created" → 201）——
                            ; 不用 $Match 正则：实测 $Match(状态行, 含 \. 与 [0-9] 的模式) 未命中
                            ; "HTTP/1.1 201 Created"，把已成功落地的请求误判为失败（消息 Error）。
                            If (tLine '= "") {
                                Set tCode = +$PIECE(tLine, " ", 2)
                                If (tCode < 200) || (tCode > 299) {
                                    Set tSC = $$$ERROR($$$GeneralError, "FHIR 目标 HTTP 回执非 2xx: " _ tLine)
                                }
                            }
                        }
                    } Else {
                        Set tSC = $$$ERROR($$$GeneralError, "FHIR HTTP 请求消息构造失败: " _ tRT)
                    }
                }
            } Else {
                // DB 目标：行 JSON → StreamContainer → SQLOp_{table}（UPSERT 幂等）
                // 目标表优先级：FHIRRequest 自带精确 MappingId → 以该 mapping.target_table 为权威
                // （同源多 mapping 各去各表，如 Patient→SQLOp_Patient / Observation→SQLOp_Observation）；
                // 管道组 table 仅兜底（如 SQL 轮询行无精确 mapping 时的单表组）。
                Set tTable = ""
                If request.%IsA("demo.FHIRRequest") {
                    Set tMapTable = tMap.%Get("target_table")
                    If tMapTable '= "" Set tTable = tMapTable
                }
                If tTable = "" {
                    If $isobject(tPipeCfg) && (tPipeCfg.%Get("table") '= "") {
                        Set tTable = tPipeCfg.%Get("table")
                    }
                }
                If tTable = "" Set tTable = tMap.%Get("target_table")
                Set tStream = ##class(Ens.StreamContainer).%New()
                Set tStream.Stream = ##class(%Stream.GlobalCharacter).%New()
                Do tStream.Stream.Write(tRowJson)
                // 派发目标：显式派发表优先（多管道改名后按约定拼名会指向不存在的 SQLOp_{table}）
                Set tDbOp = ""
                If tDispItems '= "" {
                    Set tDbOp = tDispItems.%Get(tTable)
                    ; 键名可能带限定前缀（如 SQLUser.PatientSource）→ 退末段再试一次
                    If (tDbOp = "") && (tTable '= "") {
                        Set tDbOp = tDispItems.%Get($PIECE(tTable, ".", $LENGTH(tTable, ".")))
                    }
                }
                If tDbOp = "" Set tDbOp = "SQLOp_" _ tTable
                Do ..SendRequestAsync(tDbOp, tStream)
            }
        }
    } Catch ex {
        Set tSC = ex.AsStatus()
    }
    Quit tSC
}

/// 解析当前消息的来源（源业务服务配置名，如 SQLService / FHIRService），
/// 用于多管道路由（每个源对应一组映射与目标）。
/// 优先取消息关联的 Header.SourceConfigName；失败时按消息类型兜底。
Method ResolveSource(request As %Library.Persistent) As %String
{
    Set tSource = ""
    Try {
        If $isobject(..%PrimaryRequestHeader) {
            Set tSource = ..%PrimaryRequestHeader.SourceConfigName
        }
    } Catch ex {
        Set tSource = ""
    }
    If tSource = "" {
        // 兜底：按消息类型推断源（IRIS 无 ElseIf 命令，用独立 If）
        If request.%IsA("demo.FHIRRequest") Set tSource = "FHIRService"
        If request.%IsA("Ens.StreamContainer") Set tSource = "SQLService"
    }
    Quit tSource
}

/// 解包源消息为源数据 JSON 字符串（多源适配）。
/// - demo.FHIRRequest：FHIR 资源 JSON（request.ResourceJson）
/// - Ens.StreamContainer：SQL 行 JSON（EnsLib.SQL.Service.GenericService 轮询行）
/// - %DynamicObject：直接转 JSON
ClassMethod UnpackSource(request As %Library.Persistent) As %String
{
    If request.%IsA("demo.FHIRRequest") {
        Quit request.ResourceJson
    }
    If request.%IsA("Ens.StreamContainer") {
        Quit request.Stream.Read()
    }
    If request.%IsA("%Library.DynamicObject") {
        Quit request.%ToJSON()
    }
    Quit ""
}

/// 按目标资源类型组装 FHIR 资源 JSON —— 通用 schema 驱动（不再 per-type 手写）：
/// 组装元数据来自生成端写入的 ^demo.Config("fhir","schema",<type>)
/// （fhir_target_model 的 path/system/type，属平台结构知识）；字段值来自 LLM 决策的行 JSON。
/// 返回资源 JSON 字符串；无 schema/数据异常返回 ""（调用方报明确错误）。
///
/// ⚠ 2026-09-18：由 ClassMethod 改为**实例方法** —— 术语转换改走**共享 BO**
/// （`demo.TerminologyOperation`，见 `TermCoding`），而 `..SendRequestSync` 只能在业务主机实例上下文调用
/// （静态方法没有 %Process）。因此本方法及 `LeafValue` 必须是实例方法；AI 生成的聚合 BP
/// （`demo.SqlFhirPatientTxProcess`）**继承本类**（`Extends demo.TransformProcess`）后即可 `..BuildFHIRResource(...)`。
Method BuildFHIRResource(pResourceType As %String, pRowJson As %String, pSourceJson As %String, pFieldMappingsJson As %String = "") As %String
{
    Set tSchJson = $Get(^demo.Config("fhir", "schema", pResourceType), "")
    If tSchJson = "" Quit ""
    Set tSch = ##class(%DynamicObject).%FromJSON(tSchJson)
    If '$IsObject(tSch) Quit ""
    Set tRow = ##class(%DynamicObject).%FromJSON(pRowJson)
    If '$IsObject(tRow) Quit ""
    Set tSrc = ##class(%DynamicObject).%FromJSON(pSourceJson)
    If '$IsObject(tSrc) Set tSrc = tRow

    ; 解析 LLM 决策的 term_map 指令：target 列名 → 判码 Skill
    Set tTermMap = ##class(%DynamicObject).%New()
    If pFieldMappingsJson '= "" {
        Set tFMs = ##class(%DynamicObject).%FromJSON(pFieldMappingsJson)
        If $IsObject(tFMs) {
            For tJ = 0:1:tFMs.%Size() - 1 {
                Set tFM = tFMs.%Get(tJ)
                Set tTr = ""
                If $IsObject(tFM) Set tTr = tFM.%Get("transform")
                If ($EXTRACT(tTr, 1, 9) = "term_map:") {
                    Set tSkill = $PIECE(tTr, ":", 2)
                    If tSkill '= "" Do tTermMap.%Set(tFM.%Get("target"), tSkill)
                }
            }
        }
    }

    ; 目标资源 id：① SQL 行主键（ID）② 源 FHIR 资源自身 id ③ 转换行里的 ID/id
    ; （FHIR→FHIR 映射按 schema 把 id 映射到 identifier 等业务列，"id" 不会出现在转换行里，
    ;   缺失会导致整份资源组装失败 → 故按上述顺序回退，仍取不到才明确失败）
    Set tId = tSrc.%Get("ID")
    If tId = "" Set tId = tSrc.%Get("id")
    If tId = "" Set tId = tRow.%Get("ID")
    If tId = "" Set tId = tRow.%Get("id")
    If tId = "" Quit ""

    Set tO = ##class(%DynamicObject).%New()
    Do tO.%Set("resourceType", pResourceType)
    Do tO.%Set("id", tId)
    Set tProfile = tSch.%Get("profile")
    ; meta：① 模型 profile（结构登记）② 术语"未映射"标记（见下，收集于 tUnmapped）
    Set tMeta = ""
    If tProfile '= "" {
        Set tMeta = ##class(%DynamicObject).%New()
        Set tArr = ##class(%DynamicArray).%New()
        Do tArr.%Push(tProfile)
        Do tMeta.%Set("profile", tArr)
    }
    ; 术语降级登记：term_map 决策存在，但术语服务器**尚无该源编码的判定**或调用失败
    ; → 保留源 coding 继续落地（默认降级，不阻断），并在 meta.tag 显式标注，绝不静默
    Set tUnmapped = ##class(%DynamicArray).%New()

    Set tCols = tSch.%Get("columns")
    If $IsObject(tCols) {
        For tI = 0:1:tCols.%Size() - 1 {
            Set tC = tCols.%Get(tI)
            Set tName = tC.%Get("name")
            Set tVal = tRow.%Get(tName)
            If tVal = "" Continue
            Set tPath = tC.%Get("path")
            If tPath = "" Set tPath = tName
            Set tLeaf = ..LeafValue(tC, tVal, tSrc, tTermMap, tUnmapped)
            If tLeaf '= "" Do ..SetPath(tO, tPath, tLeaf)
        }
    }
    Set tUnmappedCnt = tUnmapped.%Size()
    If tUnmappedCnt > 0 {
        If '$IsObject(tMeta) Set tMeta = ##class(%DynamicObject).%New()
        Set tTags = ##class(%DynamicArray).%New()
        For tU = 0:1:tUnmappedCnt - 1 {
            Set tTag = ##class(%DynamicObject).%New()
            Do tTag.%Set("system", "urn:cn-nhsa:term-map")
            Do tTag.%Set("code", "unmapped")
            Do tTag.%Set("display", tUnmapped.%Get(tU))
            Do tTags.%Push(tTag)
        }
        Do tMeta.%Set("tag", tTags)
    }
    If $IsObject(tMeta) Do tO.%Set("meta", tMeta)
    Quit tO.%ToJSON()
}

/// 读取某条映射的字段映射（`^demo.Mapping(id).field_mappings`），返回 JSON 数组字符串。
///
/// 平台提供的**稳定 API**（2026-09-18 起）：AI 生成的聚合 BP 直接用 `..GetMappingFms(tMappingID)`
/// 即可，**不要**在自己的类里重复声明同名助手（实测缺陷：新一版 BP 调用了 `..GetMappingFms` 但没定义 →
/// 编译报 `MPP5376 Method or Property 'GetMappingFms' does not exist` → 整个 sql2fhir 组生成失败）。
/// 取不到（id 为空 / 映射不存在 / 无 field_mappings）统一返回 `"[]"`，调用方无需再判空。
ClassMethod GetMappingFms(pMappingID As %String) As %String
{
    Set tFms = "[]"
    If (pMappingID = "") {
        Quit tFms
    }
    Set tMapStr = $Get(^demo.Mapping(pMappingID))
    If (tMapStr = "") {
        Quit tFms
    }
    Set tMap = ##class(%DynamicObject).%FromJSON(tMapStr)
    If '$IsObject(tMap) {
        Quit tFms
    }
    Set tF = tMap.%Get("field_mappings")
    If $IsObject(tF) {
        Set tFms = tF.%ToJSON()
    }
    Quit tFms
}

/// 归一字段映射 JSON：合法 JSON（数组/对象）原样返回，空/不可解析统一返回 `"[]"`。
///
/// 同 `GetMappingFms` 属**平台提供的稳定 API**（AI 生成的 BP 直接调用，不要自造同名助手）。
ClassMethod ToFmsJSON(pFms As %String) As %String
{
    Set tOut = "[]"
    If (pFms = "") {
        Quit tOut
    }
    Set tObj = ##class(%DynamicAbstractObject).%FromJSON(pFms)
    If $IsObject(tObj) {
        Set tOut = tObj.%ToJSON()
    }
    Quit tOut
}

/// 按列元数据（type/system/name）把叶子值转成最终 JSON 值：
/// Reference / CodeableConcept / identifier / quantity / 单 Coding / 原样标量。
/// pTermMap：target 列名 → 判定 Skill（来自 LLM 的 field_mappings.transform=term_map:<skill>）；
/// pUnmapped：**降级登记**数组（term_map 命中但术语服务器无判定/调用失败时追加 "<skill>:<源码>"）。
Method LeafValue(pCol As %DynamicObject, pVal As %String, pSrc As %DynamicObject, pTermMap As %DynamicObject, pUnmapped As %DynamicArray) As %RegisteredObject
{
    Set tType = $ZCONVERT(pCol.%Get("type"), "L")
    Set tName = pCol.%Get("name")
    Set tSys = pCol.%Get("system")
    If tType ["reference" Quit ..Reference(pVal)
    If (tType ["concept") || (tType = "codeable") {
        Set tUseSys = tSys
        ; 2026-09-19 实测缺陷修复：原先无条件取源行的硬编码 `CodeSystem` 列 —— 该列是**整行共享**的
        ; （如 MedicationOrder 行 = urn:cn-nhsa:drug-nrdl）→ 同一行的 route（“口服”）被误挂药品体系；
        ; 而 Encounter 表无该列 → 国标码 reasonCode 反而没有 system。现改为**只认平台在 schema 里
        ; 声明的列名**（`system_from_row`，由 backend/services/fhir_schema_facts.py 按事实注入）。
        ; ⚠ 旧 schema 兼容：由 `coded` 标记是否存在区分"新/旧 schema"——旧管道（生成于本修复之前）
        ; 未重生成时仍沿用原兜底，避免把已有 source coding 的 system 丢光（重生成后自动用新口径）。
        Set tFromRow = ""
        Set tHasFact = 0
        If $IsObject(pCol) {
            Set tFromRow = pCol.%Get("system_from_row")
            If pCol.%Get("coded", "?") '= "?" Set tHasFact = 1
        }
        If (tUseSys = "") && $IsObject(pSrc) {
            If tFromRow '= "" {
                Set tUseSys = pSrc.%Get(tFromRow)
            } ElseIf (tHasFact = 0) {
                Set tUseSys = pSrc.%Get("CodeSystem")
            }
        }
        Set tCC = ..CodeableConcept(pVal, tUseSys)
        ; term_map 决策存在 → 经**共享术语 BO** 取目标体系 coding（双 coding；码来自术语服务器，非平台写死）
        Set tSkill = ""
        If $IsObject(pTermMap) Set tSkill = pTermMap.%Get(tName)
        If tSkill '= "" {
            Set tTermStatus = ""
            Set tCoding = ..TermCoding(tSkill, pVal, .tTermStatus)
            If $IsObject(tCoding) {
                Set tArr = tCC.%Get("coding")
                If $IsObject(tArr) Do tArr.%Push(tCoding)
            } Else {
                ; 默认降级：missing（服务器尚无判定）/ error（不可达等）→ 保留源 coding + 登记未映射（不阻断、不静默）；
                ; negative 不登记（那是服务器已判定"池内无匹配"的正常结论）
                If (tTermStatus = "missing") || (tTermStatus = "error") {
                    If $IsObject(pUnmapped) Do pUnmapped.%Push(tSkill _ ":" _ pVal)
                }
            }
        }
        Quit tCC
    }
    If tType = "identifier" {
        Set tId = ##class(%DynamicObject).%New()
        If tSys '= "" Do tId.%Set("system", tSys)
        Do tId.%Set("value", pVal)
        Quit tId
    }
    If ((tType = "datetime") || (tType = "date")) {
        ; 空/控制字符（如 JDBC 空日期返回 $CHAR(0)）→ 返回空，调用方跳过该字段
        Set tClean = $TRANSLATE(pVal, $CHAR(0), "")
        If $ZSTRIP(tClean, "<>W") = "" Quit ""
        ; FHIR dateTime 必须带时区（否则 MalformedValue）；无时区时补默认时区（平台参数化）
        If tType = "datetime" {
            Set tTz = $Get(^demo.Config("fhir", "timezone"), "+08:00")
            If tTz = "" Set tTz = "+08:00"
            Set tTimePart = $PIECE(tClean, "T", 2)
            If tTimePart '= "" {
                If ('((tTimePart [ "Z") || (tTimePart [ "+") || (tTimePart [ "-"))) {
                    Set tClean = tClean _ tTz
                }
            }
        }
        Quit tClean
    }
    If tType = "quantity" {
        Set tQ = ##class(%DynamicObject).%New()
        Set tNum = $PIECE(pVal, " ", 1)
        If (tNum '= "") && ($IsValidNum(tNum)) {
            Set tQ.value = +tNum
        } Else {
            Set tQ.value = 0
        }
        Set tUnit = $PIECE(pVal, " ", 2, $LENGTH(pVal, " "))
        If tUnit '= "" Do tQ.%Set("unit", tUnit)
        Quit tQ
    }
    If (tType = "code") && (tName = "class_code") {
        Set tC = ##class(%DynamicObject).%New()
        If tSys '= "" Do tC.%Set("system", tSys)
        Do tC.%Set("code", pVal)
        Quit tC
    }
    Quit pVal
}

/// 经**共享术语 BO** 取目标体系 coding 对象；非 active 返回 ""（pStatus 回传服务器判定状态）。
///
/// 架构（2026-09-18 起）：术语转换能力 = IRIS 上的**共享 BO** `demo.TerminologyOperation`
/// （事实源 = 术语服务器 `Terminology.Mapping.CodeMap`）。任何管道的 BP 都通过它取判码，
/// **运行期不再有本地码表副本**（旧 `^demo.Config("termcache")` 机制已废弃）。
///
/// pStatus 取值与调用方处置：
///   · active   → 追加目标体系 coding（双 coding）；
///   · negative → 不追加（服务器已判定"池内无匹配"，正常结论，非失败）；
///   · missing  → 服务器尚无该源编码的判定 → 调用方**默认降级**（保留源 coding + meta.tag 打未映射标记）；
///   · error    → BO 调用失败 / 服务器不可达 / 未登记 Skill → 同上降级（并写 Error 日志，不静默）。
Method TermCoding(pSkill As %String, pSourceVal As %String, Output pStatus As %String) As %RegisteredObject
{
    Set pStatus = ""
    If (pSkill = "") || (pSourceVal = "") Quit ""
    Set tReq = ##class(demo.TermLookupRequest).%New()
    Set tReq.Skill = pSkill
    Set tReq.SourceCode = pSourceVal
    Set tResp = ""
    Set tSC = ..SendRequestSync("TerminologyOperation", tReq, .tResp, 5)
    If $$$ISERR(tSC) {
        Set pStatus = "error"
        $$$LOGERROR("术语查询 BO 调用失败（skill=" _ pSkill _ " code=" _ pSourceVal _ "）：" _ $System.Status.GetErrorText(tSC))
        Quit ""
    }
    If '$IsObject(tResp) {
        Set pStatus = "error"
        $$$LOGERROR("术语查询 BO 未返回响应（skill=" _ pSkill _ " code=" _ pSourceVal _ "）")
        Quit ""
    }
    Set pStatus = tResp.Status
    If pStatus = "error" {
        $$$LOGERROR("术语查询失败（skill=" _ pSkill _ " code=" _ pSourceVal _ "）：" _ tResp.ErrorText)
        Quit ""
    }
    If pStatus '= "active" Quit ""
    Set tCode = tResp.TargetCode
    If tCode = "" Quit ""
    Set tC = ##class(%DynamicObject).%New()
    ; 体系 URI 取 BO 回传值（术语服务器登记的事实），空缺时回退平台静态登记
    Set tSys = tResp.TargetSystem
    If tSys = "" Set tSys = ..TermSystem(pSkill)
    If tSys '= "" Do tC.%Set("system", tSys)
    Do tC.%Set("code", tCode)
    Set tDisplay = tResp.TargetDisplay
    If tDisplay '= "" Do tC.%Set("display", tDisplay)
    Quit tC
}

/// skill → 目标 coding 系统（平台静态知识，非码决策）。
ClassMethod TermSystem(pSkill As %String) As %String
{
    If pSkill = "cn2snomed" Quit "http://snomed.info/sct"
    If pSkill = "cn2rx" Quit "http://www.nlm.nih.gov/research/umls/rxnorm"
    Quit ""
}

/// 通用 JSON 路径写入：支持 a.b / a[0].b / a[0].c[0] 形态，自动创建对象/数组并复用元素。
ClassMethod SetPath(pRoot As %DynamicObject, pPath As %String, pVal As %RegisteredObject) As %Status
{
    Set tCur = pRoot
    Set tN = $LENGTH(pPath, ".")
    For tI = 1:1:tN {
        Set tPart = $PIECE(pPath, ".", tI)
        Set tIsLast = (tI = tN)
        Set tName = $PIECE(tPart, "[", 1)
        Set tIsArr = ($FIND(tPart, "[") '= 0)
        Set tIdx = 0
        If tIsArr {
            Set tOpen = $FIND(tPart, "[")
            Set tClose = $FIND(tPart, "]")
            Set tIdx = $EXTRACT(tPart, tOpen, tClose - 2)
        }
        If tIsArr {
            Set tArr = tCur.%Get(tName)
            If '$IsObject(tArr) {
                Set tArr = ##class(%DynamicArray).%New()
                Do tCur.%Set(tName, tArr)
            }
            If tIsLast {
                Do tArr.%Set(+tIdx, pVal)
            } Else {
                Set tElem = tArr.%Get(+tIdx)
                If '$IsObject(tElem) {
                    Set tElem = ##class(%DynamicObject).%New()
                    Do tArr.%Set(+tIdx, tElem)
                }
                Set tCur = tElem
            }
        } Else {
            If tIsLast {
                Do tCur.%Set(tName, pVal)
            } Else {
                Set tSub = tCur.%Get(tName)
                If '$IsObject(tSub) {
                    Set tSub = ##class(%DynamicObject).%New()
                    Do tCur.%Set(tName, tSub)
                }
                Set tCur = tSub
            }
        }
    }
    Quit $$$OK
}

/// 构造 codeableConcept：{coding:[{system?, code}]}（system 为空时不输出该键）。
ClassMethod CodeableConcept(pCode As %String, pSystem As %String) As %DynamicObject
{
    Set tC = ##class(%DynamicObject).%New()
    If pSystem '= "" Do tC.%Set("system", pSystem)
    Do tC.%Set("code", pCode)
    Set tCoding = ##class(%DynamicArray).%New()
    Do tCoding.%Push(tC)
    Set tCC = ##class(%DynamicObject).%New()
    Do tCC.%Set("coding", tCoding)
    Quit tCC
}

/// 构造 Reference 对象：{"reference": "Patient/P001"}。
ClassMethod Reference(pValue As %String) As %DynamicObject
{
    Set tR = ##class(%DynamicObject).%New()
    Do tR.%Set("reference", pValue)
    Quit tR
}

/// 构造 EnsLib.HTTP.GenericMessage（FHIR REST PUT 语义，契约见 GenericOperation.OnMessage 源码）：
/// - 方法=HTTPRequest 头；路径=URL 头（base_path/ResourceType[/id]，base_path 由生成端写入配置）；
/// - body=资源 JSON Stream；Content-Type/Accept=application/fhir+json；认证=Basic（superuser/SYS，可配置）。
ClassMethod PackFHIRHTTP(pBody As %String, pResourceType As %String, pSourceJson As %String) As EnsLib.HTTP.GenericMessage
{
    Set tId = ""
    Set tSrc = ##class(%DynamicObject).%FromJSON(pSourceJson)
    ; resource id：源 FHIR 资源是 `id`（小写）；SQL 行是主键 `ID` —— 两者都要认
    ; （只认 "ID" 时 FHIR 源拿不到 id → URL 退化成 PUT /Patient → 目标静默 0 落地，实测）
    If $IsObject(tSrc) {
        Set tId = tSrc.%Get("ID")
        If tId = "" Set tId = tSrc.%Get("id")
    }
    If tId = "" Quit ""
    Set tBasePath = $Get(^demo.Config("fhir", "base_path"), "/csp/healthshare/fhirserver/fhir/r4")
    Set tUser = $Get(^demo.Config("fhir", "username"), "superuser")
    Set tPwd = $Get(^demo.Config("fhir", "password"), "SYS")
    Set tUrl = tBasePath _ "/" _ pResourceType
    If tId '= "" Set tUrl = tUrl _ "/" _ tId

    // 关键：GenericMessage 的 body 是 %RawString（原样字节）——必须写 UTF-8 字节流，
    // 否则字符流会在发送时逐字符按单字节输出，中文变成 "?"
    Set tStream = ##class(%Stream.GlobalBinary).%New()
    Do tStream.Write($ZConvert(pBody, "O", "UTF8"))
    Set tMsg = ##class(EnsLib.HTTP.GenericMessage).%New(tStream)
    If '$IsObject(tMsg) Quit ""
    Set tB64 = $system.Encryption.Base64Encode(tUser _ ":" _ tPwd)
    Do tMsg.HTTPHeaders.SetAt("PUT", "HTTPRequest")
    Do tMsg.HTTPHeaders.SetAt(tUrl, "URL")
    Do tMsg.HTTPHeaders.SetAt("application/fhir+json", "Content-Type")
    Do tMsg.HTTPHeaders.SetAt("application/fhir+json", "Accept")
    Do tMsg.HTTPHeaders.SetAt("Basic " _ tB64, "Authorization")
    Quit tMsg
}
/// 事务 Bundle 消息构造（POST 到 FHIR base，携带 Basic Auth；UTF-8 字节流避免中文变 `?`）。
/// 与 `PackFHIRHTTP`（单资源 PUT）并列：聚合管道用本方法投递 `type=transaction` 的 Bundle。
ClassMethod PackFHIRBundle(pBundleJson As %String) As EnsLib.HTTP.GenericMessage
{
    If pBundleJson = "" Quit ""
    Set tBasePath = $Get(^demo.Config("fhir", "base_path"), "/csp/healthshare/fhirserver/fhir/r4")
    Set tUser = $Get(^demo.Config("fhir", "username"), "superuser")
    Set tPwd = $Get(^demo.Config("fhir", "password"), "SYS")
    Set tStream = ##class(%Stream.GlobalBinary).%New()
    Do tStream.Write($ZConvert(pBundleJson, "O", "UTF8"))
    Set tMsg = ##class(EnsLib.HTTP.GenericMessage).%New(tStream)
    If '$IsObject(tMsg) Quit ""
    Set tB64 = $system.Encryption.Base64Encode(tUser _ ":" _ tPwd)
    Do tMsg.HTTPHeaders.SetAt("POST", "HTTPRequest")
    Do tMsg.HTTPHeaders.SetAt(tBasePath, "URL")
    Do tMsg.HTTPHeaders.SetAt("application/fhir+json", "Content-Type")
    Do tMsg.HTTPHeaders.SetAt("application/fhir+json", "Accept")
    Do tMsg.HTTPHeaders.SetAt("Basic " _ tB64, "Authorization")
    Quit tMsg
}

/// 由「资源类型 + id」派生确定性 UUID（同输入同输出 → 重投幂等）。
/// ⚠ FHIR 的 `urn:uuid` 只接受**小写**十六进制且必须 8-4-4-4-12 共 32 位 → 需逐字节零填充 + 转小写
/// （实测 2026-09-19：大写 UUID 被 FHIR 服务器判 `MalformedRelativeReference`）。
ClassMethod MakeResourceUuid(pResourceType As %String, pId As %String) As %String
{
    Set tHash = $system.Encryption.SHA1Hash(pResourceType _ ":" _ pId)
    Set tHex = ""
    For i=1:1:$LENGTH(tHash) {
        Set tHex = tHex _ $TRANSLATE($JUSTIFY($ZHEX($ASCII(tHash, i)), 2), " ", "0")
    }
    Set tHex = $ZCONVERT(tHex, "L")
    Quit $EXTRACT(tHex, 1, 8) _ "-" _ $EXTRACT(tHex, 9, 12) _ "-" _ $EXTRACT(tHex, 13, 16) _ "-" _
         $EXTRACT(tHex, 17, 20) _ "-" _ $EXTRACT(tHex, 21, 32)
}

/// 构造事务 Bundle 的 entry（`fullUrl` + `resource` + `request`）——纯函数，可单测。
ClassMethod BundleEntry(pResourceType As %String, pResourceObj As %DynamicObject, pUuid As %String) As %DynamicObject
{
    Set tEntry = ##class(%DynamicObject).%New()
    Do tEntry.%Set("fullUrl", "urn:uuid:" _ pUuid)
    Do tEntry.%Set("resource", pResourceObj)
    Set tReq = ##class(%DynamicObject).%New()
    Do tReq.%Set("method", "POST")
    Do tReq.%Set("url", pResourceType)
    Do tEntry.%Set("request", tReq)
    Quit tEntry
}

/// 按布局 refs 注入引用（父级 urn:uuid）——纯函数，可单测。
/// refs 形如 [{target_resource,target_field,parent_resource,to}]（平台插件生成的实例级事实）。
ClassMethod InjectBundleRefs(pResObj As %DynamicObject, pResType As %String,
                             pParentType As %String, pParentUuid As %String,
                             pPatientUuid As %String, pRefs As %DynamicArray) As %Status
{
    If '$IsObject(pResObj) Quit $$$OK
    If '$IsObject(pRefs) Quit $$$OK
    For i=0:1:pRefs.%Size()-1 {
        Set tRef = pRefs.%Get(i)
        If '$IsObject(tRef) Continue
        If (tRef.%Get("target_resource") '= pResType) Continue
        Set tField = tRef.%Get("target_field")
        If tField = "" Set tField = tRef.%Get("field")
        If tField = "" Continue
        Set tWant = tRef.%Get("parent_resource")
        Set tUse = ""
        If ((tWant '= "") && (tWant = pParentType)) {
            Set tUse = pParentUuid
        } Else {
            If ((tWant '= "") && (tWant = "Patient") && (pResType '= "Patient")) {
                Set tUse = pPatientUuid
            }
        }
        If tUse = "" Continue
        Do pResObj.%Set(tField, ..Reference("urn:uuid:" _ tUse))
    }
    Quit $$$OK
}

/// 校验 FHIR 回执：**非 2xx 必须失败**，且**体内若是 OperationOutcome(error) 也判失败**
/// （`GenericOperation` 对 4xx/5xx 不报错；只查状态会漏判，实测 2026-09-19 目标 0 落地却消息全 Completed）。
ClassMethod BundleHttpOk(pResponse As %RegisteredObject, Output pDetail As %String) As %Status
{
    Set pDetail = ""
    If '$IsObject(pResponse) Quit $$$ERROR($$$GeneralError, "FHIR 无回执（响应为空）")
    Set tLine = ""
    Set tBody = ""
    Try {
        If $IsObject(pResponse.HTTPHeaders) Set tLine = $Get(pResponse.HTTPHeaders.GetAt("StatusLine"))
    } Catch e {
        Set tLine = ""
    }
    Try {
        Set tS = $PROPERTY(pResponse, "Stream")
        If $IsObject(tS) {
            Do tS.Rewind()
            Set tBody = tS.Read()
        }
    } Catch e {
        Set tBody = ""
    }
    Set pDetail = tLine _ " | " _ $EXTRACT(tBody, 1, 300)
    Set tCode = +$PIECE(tLine, " ", 2)
    If (tCode '= 0) && ((tCode < 200) || (tCode > 299)) {
        Quit $$$ERROR($$$GeneralError, "FHIR 目标回执非 2xx（" _ tLine _ "）：" _ $EXTRACT(tBody, 1, 600))
    }
    If (tBody [ "OperationOutcome") && (tBody [ "severity"":""error") {
        Quit $$$ERROR($$$GeneralError, "FHIR 目标返回 OperationOutcome 错误：" _ $EXTRACT(tBody, 1, 600))
    }
    Quit $$$OK
}

/// **通用聚合引擎**（sql2fhir-patient-tx）：按布局把「患者 + 子资源」组装成**事务 Bundle** 并投递。
///
/// 布局契约（`^demo.Config("sql2fhir","layout", <本BP实例名>)`，平台按本管道写入）：
/// `patient_table/patient_id_col`、`query_bos[]`(source_table/target_resource/bo_name/fk_col/depth/depends_on/mapping_id)、
/// `bundle.entries[]`(source_table/target_resource/id_col/parent_kind/mapping_id)、`bundle.refs[]`、`http_bo`。
///
/// 为什么在父类（2026-09-19 实测）：原先把「遍历 / 子表派发 / 层级记账 / 引用注入 / 事务组装 / 回执校验」
/// 交给 LLM 生成整份 BP —— 单次 completion ≈34.5k、30~60 分钟、频繁 timeout 且细节易错；下移到平台后
/// AI 只需决定拓扑，BP 变薄（编排决策仍归 AI）。
Method ProcessFHIRBundle(pRowJson As %String, pMappingId As %String = "") As %Status
{
    Set tLayoutStr = $Get(^demo.Config("sql2fhir", "layout", ..%ConfigName))
    If tLayoutStr = "" Set tLayoutStr = $Get(^demo.Config("sql2fhir", "layout"))
    If tLayoutStr = "" Quit $$$ERROR($$$GeneralError, "缺少布局契约（^demo.Config(""sql2fhir"",""layout"")）")
    Set tLayout = ##class(%DynamicObject).%FromJSON(tLayoutStr)
    If '$IsObject(tLayout) Quit $$$ERROR($$$GeneralError, "布局不是 JSON 对象")
    Set tRow = ##class(%DynamicObject).%FromJSON(pRowJson)
    If '$IsObject(tRow) Quit $$$ERROR($$$GeneralError, "源行不是 JSON 对象")

    Set tHttpBO = tLayout.%Get("http_bo")
    If tHttpBO = "" Set tHttpBO = $Get(^demo.Config("fhir", "operation"))
    If tHttpBO = "" Quit $$$ERROR($$$GeneralError, "缺少 FHIR Operation 实例名（http_bo）")
    Set tBundleCfg = tLayout.%Get("bundle")
    If '$IsObject(tBundleCfg) Quit $$$ERROR($$$GeneralError, "布局缺少 bundle（entries/refs）")
    Set tEntriesCfg = tBundleCfg.%Get("entries")
    Set tRefs = tBundleCfg.%Get("refs")
    Set tQBs = tLayout.%Get("query_bos")
    Set tPidCol = tLayout.%Get("patient_id_col")
    Set tPatientId = tRow.%Get(tPidCol)
    If tPatientId = "" Set tPatientId = tRow.%Get("ID")
    If tPatientId = "" Quit $$$ERROR($$$GeneralError, "源行缺少患者主键列 " _ tPidCol)

    ; ---- level 0：患者（层级记账与布局 depth 对齐：患者层 = level 0）----
    Set tPCfg = ""
    Set i = 0
    While (i < tEntriesCfg.%Size()) {
        Set tE = tEntriesCfg.%Get(i)
        If (tE.%Get("parent_kind") = "root") Set tPCfg = tE
        Set i = i + 1
    }
    If '$IsObject(tPCfg) Quit $$$ERROR($$$GeneralError, "布局缺少患者（parent_kind=root）条目")
    Set tPFms = ..GetMappingFms(tPCfg.%Get("mapping_id"))
    Set tPTrans = ##class(demo.FHIRTransformHelper).TransformResource(pRowJson, tPFms)
    Set tPJson = ..BuildFHIRResource("Patient", tPTrans, pRowJson, tPFms)
    If tPJson = "" Quit $$$ERROR($$$GeneralError, "患者资源组装失败（BuildFHIRResource 返回空）")
    Set tPObj = ##class(%DynamicObject).%FromJSON(tPJson)
    Do tPObj.%Set("resourceType", "Patient")
    Set tPUuid = ..MakeResourceUuid("Patient", tPatientId)
    Set tEntries = ##class(%DynamicArray).%New()
    Do tEntries.%Push(..BundleEntry("Patient", tPObj, tPUuid))

    Kill tLevelCnt, tLevelIds, tLevelUuid, tLevelTypes
    Set tLevelCnt(0) = 1
    Set tLevelIds(0, 0) = tPatientId
    Set tLevelUuid(0, 0) = tPUuid
    Set tLevelTypes(0, 0) = "Patient"

    ; ---- 逐层子资源（按 depth 升序；父层取 level=depth-1 的集合）----
    ; ⚠ 2026-09-19 实测缺陷：单表布局（query_bos=[] → tMaxDepth=0）时循环体从不执行，
    ;   原先未预置 tSC → 循环后的 `If $$$ISERR(tSC)` 报 <UNDEFINED>*tSC（ErrBPTerminated，
    ;   表现为"消息 Error、FHIR 0 落地"）。故 tSC 必须先置为 $$$OK。
    Set tSC = $$$OK
    Set tMaxDepth = 0
    Set i = 0
    While (i < tQBs.%Size()) {
        Set tD = +tQBs.%Get(i).%Get("depth")
        If tD > tMaxDepth Set tMaxDepth = tD
        Set i = i + 1
    }
    Set tDepth = 1
    While (tDepth <= tMaxDepth) {
        Set i = 0
        While (i < tQBs.%Size()) {
            Set tQB = tQBs.%Get(i)
            If (+tQB.%Get("depth") = tDepth) {
                Set tSC = ..ProcessChildLevel(tQB, pRowJson, tRefs, tEntries, .tLevelCnt,
                                              .tLevelIds, .tLevelUuid, .tLevelTypes, tPUuid, tDepth)
                If $$$ISERR(tSC) Quit
            }
            Set i = i + 1
        }
        If $$$ISERR(tSC) Quit
        Set tDepth = tDepth + 1
    }
    If $$$ISERR(tSC) Quit tSC

    ; ---- 事务 Bundle 投递 + 回执校验（4xx/5xx 必须显式失败）----
    Set tBundle = ##class(%DynamicObject).%New()
    Do tBundle.%Set("resourceType", "Bundle")
    Do tBundle.%Set("type", "transaction")
    Do tBundle.%Set("entry", tEntries)
    Set tMsg = ..PackFHIRBundle(tBundle.%ToJSON())
    If '$IsObject(tMsg) Quit $$$ERROR($$$GeneralError, "Bundle 消息构造失败（PackFHIRBundle）")
    Set tResp = ""
    Set tSC = ..SendRequestSync(tHttpBO, tMsg, .tResp)
    If $$$ISERR(tSC) Quit tSC
    Set tDetail = ""
    Quit ..BundleHttpOk(tResp, .tDetail)
}
/// （^demo.Config("soap","request_class")，如 AddPatientRequest），
/// 按请求消息属性结构递归填充：简单类型 ← 行 JSON 同名键（兜底主键 ID），
/// 复杂对象属性（实体，如 AddPatientRequest.Patient）→ new 子对象并递归填充。
/// 处理某一层的子资源：按父层集合派发子表查询 → 逐行转换/组装/注入引用 → 追加 entry。
Method ProcessChildLevel(pQB As %DynamicObject, pRowJson As %String, pRefs As %DynamicArray,
                         pEntries As %DynamicArray, ByRef pLevelCnt, ByRef pLevelIds,
                         ByRef pLevelUuid, ByRef pLevelTypes, pPatientUuid As %String,
                         pDepth As %Integer) As %Status
{
    Set tBoName = pQB.%Get("bo_name")
    Set tRes = pQB.%Get("target_resource")
    Set tFkCol = pQB.%Get("fk_col")
    Set tIdCol = pQB.%Get("id_col")
    If tIdCol = "" Set tIdCol = "ID"
    If (tBoName = "") || (tRes = "") {
        Quit $$$ERROR($$$GeneralError, "布局的查询 BO 缺少 bo_name/target_resource")
    }
    Set tFms = ..GetMappingFms(pQB.%Get("mapping_id"))
    Set tParentDepth = pDepth - 1
    Set tParentCnt = +$Get(pLevelCnt(tParentDepth))
    Set tChildIdx = 0
    For k=0:1:tParentCnt-1 {
        Set tParentKey = $Get(pLevelIds(tParentDepth, k))
        Set tParentUuid = $Get(pLevelUuid(tParentDepth, k))
        Set tParentType = $Get(pLevelTypes(tParentDepth, k), "Patient")
        ; 子表查询：请求体 = {fk_col: 父键}（平台 SQL 查询 BO 的约定）
        Set tReqObj = ##class(%DynamicObject).%New()
        Do tReqObj.%Set(tFkCol, tParentKey)
        Set tReqStream = ##class(%Stream.GlobalCharacter).%New()
        Do tReqStream.Write(tReqObj.%ToJSON())
        Set tReq = ##class(Ens.StreamContainer).%New()
        Set tReq.Stream = tReqStream
        Set tResp = ""
        Set tSC = ..SendRequestSync(tBoName, tReq, .tResp)
        If $$$ISERR(tSC) {
            $$$LOGERROR("子表查询失败 " _ tBoName _ ": " _ $System.Status.GetErrorText(tSC))
            Quit
        }
        Set tRowsJson = ""
        Try {
            Set tRS = $PROPERTY(tResp, "Stream")
            If $IsObject(tRS) {
                Do tRS.Rewind()
                Set tRowsJson = tRS.Read()
            }
        } Catch e {
            Set tRowsJson = ""
        }
        If tRowsJson = "" Continue
        Set tRows = ##class(%DynamicObject).%FromJSON(tRowsJson)
        If '$IsObject(tRows) Continue
        Set tIter = tRows.%GetIterator()
        While (tIter.%GetNext(.tKey, .tRowObj)) {
            If '$IsObject(tRowObj) Continue
            Set tRowJson = tRowObj.%ToJSON()
            Set tTrans = ##class(demo.FHIRTransformHelper).TransformResource(tRowJson, tFms)
            Set tResJson = ..BuildFHIRResource(tRes, tTrans, tRowJson, tFms)
            If tResJson = "" {
                $$$LOGWARNING("资源组装为空，跳过：" _ tRes _ " 行 " _ tRowJson)
                Continue
            }
            Set tResObj = ##class(%DynamicObject).%FromJSON(tResJson)
            Do tResObj.%Set("resourceType", tRes)
            Set tRowId = tRowObj.%Get(tIdCol)
            If tRowId = "" Set tRowId = tRowObj.%Get("ID")
            Set tUuid = ..MakeResourceUuid(tRes, tRowId)
            Do ..InjectBundleRefs(tResObj, tRes, tParentType, tParentUuid, pPatientUuid, pRefs)
            Do pEntries.%Push(..BundleEntry(tRes, tResObj, tUuid))
            Set pLevelIds(pDepth, tChildIdx) = tRowId
            Set pLevelUuid(pDepth, tChildIdx) = tUuid
            Set pLevelTypes(pDepth, tChildIdx) = tRes
            Set tChildIdx = tChildIdx + 1
        }
    }
    Set pLevelCnt(pDepth) = tChildIdx
    Quit $$$OK
}

ClassMethod PackSOAPRequest(pRowJson As %String, pMap As %DynamicObject) As %Library.Persistent
{
    Set tReqClass = $Get(^demo.Config("soap", "request_class"), "")
    If tReqClass = "" Quit ""
    Set tReq = $ClassMethod(tReqClass, "%New")
    If '$isobject(tReq) Quit ""
    Set tRow = ##class(%DynamicObject).%FromJSON(pRowJson)
    If '$isobject(tRow) Quit tReq
    Do ..FillEntity(tReq, tRow)
    Quit tReq
}

/// 从行 JSON 递归填充实体/请求消息对象属性：
/// - 简单类型属性：行 JSON 同名键值（兜底取行主键 ID）→ $property 反射赋值
/// - 复杂对象属性（XML 实体对象，如 Patient）：new 子对象并递归填充其字段
ClassMethod FillEntity(pObj As %RegisteredObject, pRow As %DynamicObject)
{
    Set tRS = ##class(%SQL.Statement).%New()
    Set tSC = tRS.%Prepare(
        "SELECT Name, Type FROM %Dictionary.PropertyDefinition WHERE parent=? ORDER BY Name")
    If $$$ISOK(tSC) {
        Set tRset = tRS.%Execute($classname(pObj))
        While tRset.%Next() {
            Set tProp = tRset.%Get("Name")
            If $extract(tProp, 1) = "%" Continue  // 跳过系统内部属性
            Set tType = tRset.%Get("Type")
            If ..IsScalarType(tType) {
                // 简单类型：行 JSON 同名键值（兜底取行主键 ID）→ $property 反射赋值
                Set tVal = pRow.%Get(tProp)
                If tVal = "" Set tVal = pRow.%Get("ID")  // 兜底：查询键场景取行主键
                If tVal '= "" {
                    Set $property(pObj, tProp) = tVal
                }
            } Else {
                // 复杂对象属性（实体对象，如 Patient）：new 子对象并递归填充其字段
                Set tSub = $ClassMethod(tType, "%New")
                If $isobject(tSub) {
                    Do ..FillEntity(tSub, pRow)
                    Set $property(pObj, tProp) = tSub
                }
            }
        }
    }
}

/// 判断属性类型是否为标量（简单赋值），否则视为实体对象类型需递归填充。
ClassMethod IsScalarType(tType As %String) As %Boolean
{
    Set tScalars = " %Library.String %Library.Integer %Library.Numeric %Library.Float"
        _ " %Library.Boolean %Library.Date %Library.Time %Library.TimeStamp %Library.DateTime"
        _ " %Library.Char %Library.VarString %String %Integer %Numeric %Float %Boolean"
        _ " %Date %Time %TimeStamp %DateTime %Library.Text %Library.ArrayOfDataTypes "
    Quit ($find(tScalars, " "_tType_" ") '= 0)
}


}
