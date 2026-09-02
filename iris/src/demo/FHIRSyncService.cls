Class demo.FHIRSyncService Extends Ens.BusinessService
{

/// 无输入适配器（由定时器 CallInterval 触发增量同步）
Parameter ADAPTER = "Ens.InboundAdapter";

/// 定时增量同步间隔（秒）；演示环境每 10 秒检查一次游标之后的新数据
Parameter CallInterval = 10;

/// Business Service 入口：定时触发增量同步
Method OnProcessInput(pInput As %RegisteredObject, Output pOutput As %RegisteredObject) As %Status
{
    Quit ..SyncIncremental()
}

/// 增量同步：对每个已确认转换关系的源资产，
/// 拉取 _lastUpdated > 游标 的新资源，逐条写入队列表 FHIRQueue，
/// 处理完成后推进游标到本批最大 lastUpdated。
///
/// 说明：本组件只负责「抓取 + 入队」；队列表由 FHIRService（SQL Inbound Adapter）
/// 逐条消费，从而每条数据独立会话（传统模式）。
Method SyncIncremental() As %Status
{
    Set tSC = $$$OK
    Try {
        Set tEndpoint = $Get(^demo.Config("fhir", "endpoint"))
        If tEndpoint '= "" {
            Set tUser = $Get(^demo.Config("fhir", "username"))
            Set tPass = $Get(^demo.Config("fhir", "password"))
            Set tCursor = $Get(^demo.Config("sync", "cursor"), "")
            Set tMaxCursor = tCursor
            Set tTotal = 0

            Set tId = $Order(^demo.Mapping(""))
            While tId '= "" {
                Set tMapJson = $Get(^demo.Mapping(tId))
                If tMapJson '= "" {
                    Set tMap = ##class(%DynamicObject).%FromJSON(tMapJson)
                    Set tResourceType = tMap.%Get("source")

                    // 增量拉取（Embedded Python + urllib，_lastUpdated 游标）
                    Set tResult = ##class(demo.FHIRTransformHelper).FetchIncremental(tEndpoint, tUser, tPass, tResourceType, tCursor)
                    Set tObj = ##class(%DynamicObject).%FromJSON(tResult)

                    // 逐条写入队列表
                    Set tEnqueued = ##class(demo.FHIRTransformHelper).EnqueueResources(tObj.bundle.%ToJSON(), tId)
                    Set tTotal = tTotal + tEnqueued

                    // 推进游标（取本批最大 lastUpdated）
                    // 注意：%DynamicObject 带下划线属性必须用 %Get() 访问；
                    // 字符串比较用 ]（follows，字典序），不能用 >（ObjectScript 对含数字字符串做数值比较）
                    Set tMaxLu = tObj.%Get("max_last_updated")
                    If tMaxLu '= "" {
                        If tMaxLu ] tMaxCursor {
                            Set tMaxCursor = tMaxLu
                        }
                    }
                }
                Set tId = $Order(^demo.Mapping(tId))
            }

            // 持久化游标
            If tMaxCursor '= "" {
                Set ^demo.Config("sync", "cursor") = tMaxCursor
            }
        }
    } Catch ex {
        Set tSC = ex.AsStatus()
    }
    Quit tSC
}

}
