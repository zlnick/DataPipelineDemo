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
                Do ..SendRequestAsync("SOAPOp_" _ tService, tReq)
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
                        Set tOpName = $Get(^demo.Config("fhir", "operation"), "FHIR_HTTP_Operation")
                        Do ..SendRequestAsync(tOpName, tMsg)
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
                Do ..SendRequestAsync("SQLOp_" _ tTable, tStream)
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
ClassMethod BuildFHIRResource(pResourceType As %String, pRowJson As %String, pSourceJson As %String, pFieldMappingsJson As %String = "") As %String
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

    Set tId = tSrc.%Get("ID")
    If tId = "" Set tId = tRow.%Get("id")
    If tId = "" Quit ""

    Set tO = ##class(%DynamicObject).%New()
    Do tO.%Set("resourceType", pResourceType)
    Do tO.%Set("id", tId)
    Set tProfile = tSch.%Get("profile")
    If tProfile '= "" {
        Set tMeta = ##class(%DynamicObject).%New()
        Set tArr = ##class(%DynamicArray).%New()
        Do tArr.%Push(tProfile)
        Do tMeta.%Set("profile", tArr)
        Do tO.%Set("meta", tMeta)
    }

    Set tCols = tSch.%Get("columns")
    If $IsObject(tCols) {
        For tI = 0:1:tCols.%Size() - 1 {
            Set tC = tCols.%Get(tI)
            Set tName = tC.%Get("name")
            Set tVal = tRow.%Get(tName)
            If tVal = "" Continue
            Set tPath = tC.%Get("path")
            If tPath = "" Set tPath = tName
            Set tLeaf = ..LeafValue(tC, tVal, tSrc, tTermMap)
            If tLeaf '= "" Do ..SetPath(tO, tPath, tLeaf)
        }
    }
    Quit tO.%ToJSON()
}

/// 按列元数据（type/system/name）把叶子值转成最终 JSON 值：
/// Reference / CodeableConcept / identifier / quantity / 单 Coding / 原样标量。
ClassMethod LeafValue(pCol As %DynamicObject, pVal As %String, pSrc As %DynamicObject, pTermMap As %DynamicObject) As %RegisteredObject
{
    Set tType = $ZCONVERT(pCol.%Get("type"), "L")
    Set tName = pCol.%Get("name")
    Set tSys = pCol.%Get("system")
    If tType ["reference" Quit ..Reference(pVal)
    If (tType ["concept") || (tType = "codeable") {
        Set tUseSys = tSys
        If (tUseSys = "") && $IsObject(pSrc) Set tUseSys = pSrc.%Get("CodeSystem")
        Set tCC = ..CodeableConcept(pVal, tUseSys)
        ; term_map 命中且判码缓存存在 → 追加目标系统 coding（双 coding；码来自 Skill 缓存，非平台写死）
        Set tSkill = ""
        If $IsObject(pTermMap) Set tSkill = pTermMap.%Get(tName)
        If tSkill '= "" {
            Set tCoding = ..TermCacheCoding(tSkill, pVal)
            If $IsObject(tCoding) {
                Set tArr = tCC.%Get("coding")
                If $IsObject(tArr) Do tArr.%Push(tCoding)
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

/// 读取判码缓存并生成目标系统 coding 对象；无命中返回 ""。
/// 缓存节点：^demo.Config("termcache", <skill>, <源编码>) = {"code","display"}
ClassMethod TermCacheCoding(pSkill As %String, pSourceVal As %String) As %RegisteredObject
{
    Set tRaw = $Get(^demo.Config("termcache", pSkill, pSourceVal), "")
    If tRaw = "" Quit ""
    Set tObj = ##class(%DynamicObject).%FromJSON(tRaw)
    If '$IsObject(tObj) Quit ""
    Set tCode = tObj.%Get("code")
    If tCode = "" Quit ""
    Set tC = ##class(%DynamicObject).%New()
    Set tSys = ..TermSystem(pSkill)
    If tSys '= "" Do tC.%Set("system", tSys)
    Do tC.%Set("code", tCode)
    Set tDisplay = tObj.%Get("display")
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
    If $IsObject(tSrc) Set tId = tSrc.%Get("ID")
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
/// 行 JSON（转换产物，如 {PatientNo, FullName, Gender}）→ 构造 BO 请求消息类
/// （^demo.Config("soap","request_class")，如 AddPatientRequest），
/// 按请求消息属性结构递归填充：简单类型 ← 行 JSON 同名键（兜底主键 ID），
/// 复杂对象属性（实体，如 AddPatientRequest.Patient）→ new 子对象并递归填充。
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
