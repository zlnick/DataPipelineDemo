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

            // 只拉取「FHIR 数据源的资产」（DataAsset.type=FHIR_RESOURCE）。
            // SQL 表资产（如 PatientEntity 的 mapping 是给 SQL 源轮询的，不应被 FHIR 同步拉取）
            Kill tFhirType
            Set tAid = $Order(^demo.DataAsset(""))
            While tAid '= "" {
                Set tAJson = $Get(^demo.DataAsset(tAid))
                If tAJson '= "" {
                    Set tA = ##class(%DynamicObject).%FromJSON(tAJson)
                    If tA.%Get("type") = "FHIR_RESOURCE" {
                        Set tAtype = tA.%Get("name")
                        If tAtype '= "" Set tFhirType(tAtype) = 1
                    }
                }
                Set tAid = $Order(^demo.DataAsset(tAid))
            }

            Set tId = $Order(^demo.Mapping(""))
            While tId '= "" {
                Set tMapJson = $Get(^demo.Mapping(tId))
                If tMapJson '= "" {
                    Set tMap = ##class(%DynamicObject).%FromJSON(tMapJson)
                    Set tResourceType = tMap.%Get("source")
                    // SQL 源 mapping（source 不是 FHIR 资源类型）跳过
                    If tResourceType '= "" && $Data(tFhirType(tResourceType)) {
                        // 单个资源类型隔离 try：某类型拉取失败只跳过该类型并记录，
                        // 不中断整轮同步（保证游标仍按成功批次推进，避免重复全量拉取）。
                        Try {
                            // 增量拉取（Embedded Python + urllib，_lastUpdated 游标）
                            Set tResult = ##class(demo.FHIRTransformHelper).FetchIncremental(tEndpoint, tUser, tPass, tResourceType, tCursor)
                            Set tObj = ##class(%DynamicObject).%FromJSON(tResult)

                            // 逐条写入队列表（Python 侧幂等：同资源已入队则跳过）
                            Set tEnqueued = ##class(demo.FHIRTransformHelper).EnqueueResources(tObj.bundle.%ToJSON(), tId)
                            Set tTotal = tTotal + tEnqueued

                            // 推进游标（取本批最大 lastUpdated）
                            Set tMaxLu = tObj.%Get("max_last_updated")
                            If tMaxLu '= "" {
                                If tMaxLu ] tMaxCursor {
                                    Set tMaxCursor = tMaxLu
                                }
                            }
                        } Catch e2 {
                            // 记录单类型失败并继续下一个 mapping（可诊断；游标不停滞）
                            Set tErrText = $System.Status.GetErrorText(e2.AsStatus())
                            Set ^demo.Config("sync", "last_error") = $ZDT($H, 3) _ " " _ tMapJson _ " :: " _ tErrText
                            Set tSC = $$$OK
                        }
                    }
                }
                Set tId = $Order(^demo.Mapping(tId))
            }

            // 持久化游标（仅在确有推进时写，避免回退）
            If tMaxCursor ] tCursor {
                Set ^demo.Config("sync", "cursor") = tMaxCursor
            }
        }
    } Catch ex {
        Set tSC = ex.AsStatus()
    }
    Quit tSC
}

}
