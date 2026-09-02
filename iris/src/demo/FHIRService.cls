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
Method ProcessOneFromQueue() As %Status
{
    Set tSC = $$$OK
    Try {
        Set tRS = ##class(%SQL.Statement).%New()
        Set tSC2 = tRS.%Prepare(
            "SELECT TOP 1 ID, ResourceType, ResourceId, ResourceJson, MappingId "
            _ "FROM FHIRQueue WHERE Status='pending' ORDER BY ID")
        If $$$ISOK(tSC2) {
            Set tRset = tRS.%Execute()
            If tRset.%Next() {
                Set tReq = ##class(demo.FHIRRequest).%New()
                Set tReq.ResourceType = tRset.%Get("ResourceType")
                Set tReq.ResourceId = tRset.%Get("ResourceId")
                Set tReq.ResourceJson = tRset.%Get("ResourceJson")
                Set tReq.MappingId = tRset.%Get("MappingId")
                Set tRowId = tRset.%Get("ID")

                // 发送到转换流程（异步）
                Do ..SendRequestAsync("TransformProcess", tReq)
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


