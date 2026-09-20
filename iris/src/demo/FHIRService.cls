Class demo.FHIRService Extends Ens.BusinessService
{

/// 无输入适配器（定时器触发）；每次触发只处理队列表中一条记录，
/// 从而每条数据在独立会话中处理（传统模式）。
Parameter ADAPTER = "Ens.InboundAdapter";

/// 轮询间隔（秒）；队列表有 pending 记录时逐条处理
Parameter CallInterval = 3;

/// Business Service 入口：每次定时触发处理队列表中的一条 pending 记录
Method OnProcessInput(pInput As %RegisteredObject, Output pOutput As %RegisteredObject) As %Status
{
    Quit ..ProcessOneFromQueue()
}

/// 从队列表 FHIRQueue 取一条 pending 记录，发送到转换流程（TransformProcess），
/// 随后标记为 processed。每次调用只处理一条 → 每条数据独立会话。
///
/// 队列是**全局表**：多个数据管道并存时，本 BS 只允许消费**本管道映射集**的行
/// （^demo.Config("bs_mappings", <本BS名>) = JSON 数组，生成时写入）。
/// 否则任一组都会抢走别组的行并标记 processed → 别组拿不到数据（静默丢数据），
/// 且本组 BP 会按自己的目标类型派发（实测 TransformProcess__fhir2db 派发
/// SQLOp_Observation 报 ErrBusinessDispatchNameNotRegistered）。
/// 未配置该键时保持历史行为（消费任意 pending 行，兼容单管道旧环境）。
Method ProcessOneFromQueue() As %Status
{
    Set tSC = $$$OK
    Try {
        Set tSql = "SELECT TOP 1 ID, ResourceType, ResourceId, ResourceJson, MappingId "
            _ "FROM FHIRQueue WHERE Status='pending'"
        Set tIds = $Get(^demo.Config("bs_mappings", ..%ConfigName))
        If tIds '= "" {
            Set tArr = ##class(%DynamicArray).%FromJSON(tIds)
            Set tIn = ""
            Set tIter = tArr.%GetIterator()
            While tIter.%GetNext(.tKey, .tVal) {
                Set tVal = $Get(tVal)
                // 映射 id 只允许字母/数字/下划线（防注入），其余忽略
                If $MATCH(tVal, "^[A-Za-z0-9_]+$") {
                    Set tIn = tIn _ $Select(tIn = "": "", 1: ",") _ "'" _ tVal _ "'"
                }
            }
            If tIn '= "" Set tSql = tSql _ " AND MappingId IN (" _ tIn _ ")"
        }
        Set tSql = tSql _ " ORDER BY ID"
        Set tRS = ##class(%SQL.Statement).%New()
        Set tSC2 = tRS.%Prepare(tSql)
        If $$$ISOK(tSC2) {
            Set tRset = tRS.%Execute()
            If tRset.%Next() {
                Set tReq = ##class(demo.FHIRRequest).%New()
                Set tReq.ResourceType = tRset.%Get("ResourceType")
                Set tReq.ResourceId = tRset.%Get("ResourceId")
                Set tReq.ResourceJson = tRset.%Get("ResourceJson")
                Set tReq.MappingId = tRset.%Get("MappingId")
                Set tRowId = tRset.%Get("ID")

                // 发送到转换流程（异步）——目标 BP 由本 BS 的配置决定，
                // 以支持「一条数据管道一个 BP」：^demo.Config("bp_target", <本BS名>) = <BP名>；
                // 缺省回退 "TransformProcess"（旧单管道历史行为）。
                Set tBpName = $Get(^demo.Config("bp_target", ..%ConfigName), "TransformProcess")
                If tBpName = "" Set tBpName = "TransformProcess"
                Do ..SendRequestAsync(tBpName, tReq)
                // 标记该记录已处理（用 %SQL.Statement 执行 UPDATE，更可靠）
                Set tUpd = ##class(%SQL.Statement).%New()
                Do tUpd.%Prepare("UPDATE FHIRQueue SET Status='processed' WHERE ID=?")
                Set tUpdRset = tUpd.%Execute(tRowId)
            }
        }
    } Catch ex {
        Set tSC = ex.AsStatus()
    }
    Quit tSC
}

}


