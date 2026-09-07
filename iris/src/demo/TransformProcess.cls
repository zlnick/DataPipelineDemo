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
        // 0. 解析消息来源（源 BS 名）并查「管道组」配置（多管道：不同源→不同目标）
        //    管道组 config：^demo.Config("pipe", <源组件名>) = {"mapping","target_type","service","table"}
        Set tSource = ..ResolveSource(request)
        Set tPipeCfg = ""
        If tSource '= "" {
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

/// 按 SOAP 目标打包请求消息（写入型 / 查询型通用）：
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
