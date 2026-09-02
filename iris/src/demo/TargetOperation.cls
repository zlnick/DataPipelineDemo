Class demo.TargetOperation Extends Ens.BusinessOperation
{

/// 处理投放请求：将转换后的行写入目标表（模拟远端数据库）。
/// 注意：Business Operation 的入口方法是 OnMessage。
Method OnMessage(pRequest As %Library.Persistent, Output pResponse As %Library.Persistent) As %Status
{
    Set tSC = $$$OK
    Try {
        // 调用 Embedded Python 助手写入目标表（IRIS SQL）
        Set tResult = ##class(demo.FHIRTransformHelper).PersistRow(pRequest.TableName, pRequest.RowJson)
        Set tObj = ##class(%DynamicObject).%FromJSON(tResult)
        If tObj.status '= "ok" {
            Set tSC = $$$ERROR($$$GeneralError, "写入目标表失败: " _ tObj.message)
        }
    } Catch ex {
        Set tSC = ex.AsStatus()
    }
    Quit tSC
}

}
