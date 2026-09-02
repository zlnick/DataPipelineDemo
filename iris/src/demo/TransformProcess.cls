Class demo.TransformProcess Extends Ens.BusinessProcess
{

/// 处理异步响应（SendRequestAsync 的响应回调）。
/// 本场景无需处理响应内容，空实现即可（否则异步响应会报 #5003 Not implemented）。
Method OnResponse(request As %Library.Persistent, Output response As %Library.Persistent) As %Status
{
    Quit $$$OK
}

/// 处理 FHIR 资源：按转换关系的字段映射执行转换，输出 StreamContainer（行 JSON）路由到 SQL Operation。
/// 注意：Business Process 的入口方法是 OnRequest（不是 OnMessage）。
/// 采用 ObjectScript 编排 + Embedded Python 助手类（demo.FHIRTransformHelper）复用转换逻辑。
Method OnRequest(request As %Library.Persistent, Output response As %Library.Persistent) As %Status
{
    Set tSC = $$$OK
    Try {
        // 读取该转换关系的字段映射与目标表
        Set tMapJson = $Get(^demo.Mapping(request.MappingId))
        If tMapJson = "" {
            Set tSC = $$$ERROR($$$GeneralError, "转换关系不存在: " _ request.MappingId)
            Quit
        }
        Set tMap = ##class(%DynamicObject).%FromJSON(tMapJson)
        // 注意：%DynamicObject 带下划线的属性必须用 %Get() 访问（点语法返回未定义）
        Set tFieldMappingsJson = tMap.%Get("field_mappings").%ToJSON()
        Set tTargetTable = tMap.%Get("target_table")

        // 调用 Embedded Python 助手：执行字段级转换，返回目标表行 JSON
        Set tRowJson = ##class(demo.FHIRTransformHelper).TransformResource(request.ResourceJson, tFieldMappingsJson)

        // 输出 Ens.StreamContainer（Stream 内为行 JSON）。
        // SQL Operation（EnsLib.SQL.Operation.GenericOperation）的 InputParameters 用
        // *字段名 可直接从 StreamContainer 的 JSON 动态对象属性取值（无需预制消息结构）。
        // 注意：Ens.StreamContainer.%New() 后 Stream 为 null，必须先实例化再写入。
        Set tStream = ##class(Ens.StreamContainer).%New()
        Set tStream.Stream = ##class(%Stream.GlobalCharacter).%New()
        Do tStream.Stream.Write(tRowJson)

        // 按目标类型路由：DB→SQLOp_{表}；SOAP→SOAPOp_{服务}（WSDL 导入型 BO 组件）
        // 注：SOAP 目标的消息适配（行 JSON → BO 请求消息）为后续扩展，本处先保证组件路由命名一致。
        Set tTargetType = tMap.%Get("target_type")
        If tTargetType = "" Set tTargetType = "DB"
        Set tOpName = "SQLOp_" _ tTargetTable
        If tTargetType = "SOAP" {
            Set tService = $Get(^demo.Config("soap", "active_service"), "default")
            Set tOpName = "SOAPOp_" _ tService
        }
        Do ..SendRequestAsync(tOpName, tStream)
    } Catch ex {
        Set tSC = ex.AsStatus()
    }
    Quit tSC
}

}
