Class demo.PipelineQuery Extends %RegisteredObject
{

/// 目标表白名单（防止表名注入，Demo 固定两个目标表）
ClassMethod IsAllowedTable(pTable As %String) As %Boolean
{
    if (pTable = "Patient") || (pTable = "Observation") quit 1
    quit 0
}

/// 将消息状态码映射为可读状态
ClassMethod MapStatus(pStatus As %String) As %String
{
    if pStatus = "9" quit "Completed"
    if pStatus = "8" quit "Error"
    if pStatus = "7" quit "Cancelled"
    if pStatus = "6" quit "Submitted"
    if pStatus = "5" quit "Suspended"
    quit "Other"
}

/// 判断是否为业务消息（排除 Ens 系统组件产生的消息）
ClassMethod IsBusinessMessage(pSource As %String, pTarget As %String) As %Boolean
{
    if pSource = "" quit 0
    if $extract(pSource, 1, 4) = "Ens." quit 0
    if $extract(pTarget, 1, 4) = "Ens." quit 0
    quit 1
}

/// 返回最近 count 条消息流转日志的 JSON 数组（来自 Ens.MessageHeader 真实消息历史）
ClassMethod GetLogs(count As %Integer = 50) As %String
{
    set tArr = []
    set tId = $order(^Ens.MessageHeaderD(""), -1)
    set tN = 0
    while (tId '= "") && (tN < count) {
        set tHdr = ##class(Ens.MessageHeader).%OpenId(tId)
        if $isobject(tHdr) && ..IsBusinessMessage(tHdr.SourceConfigName, tHdr.TargetConfigName) {
            set tObj = {}
            do tObj.%Set("id", tId)
            do tObj.%Set("timestamp", tHdr.TimeCreated)
            do tObj.%Set("source", tHdr.SourceConfigName)
            do tObj.%Set("target", tHdr.TargetConfigName)
            do tObj.%Set("status", ..MapStatus(tHdr.Status))
            if (tHdr.TimeProcessed '= "") && (tHdr.TimeCreated '= "") {
                do tObj.%Set("duration_ms", $system.SQL.DATEDIFF("ms", tHdr.TimeCreated, tHdr.TimeProcessed))
            }
            do tArr.%Push(tObj)
        }
        set tId = $order(^Ens.MessageHeaderD(tId), -1)
        set tN = tN + 1
    }
    quit tArr.%ToJSON()
}

/// 返回目标表数据的 JSON 数组（表名白名单校验 + Embedded Python 查询）
ClassMethod GetTargetData(pTable As %String, pLimit As %Integer = 100) As %String
{
    If '..IsAllowedTable(pTable) Quit "[]"
    Quit ##class(demo.FHIRTransformHelper).QueryTable(pTable, pLimit)
}

/// 返回已配置的转换关系列表的 JSON 数组
ClassMethod GetMappings() As %String
{
    set tArr = []
    set tId = $order(^demo.Mapping(""))
    while tId '= "" {
        set tMap = $get(^demo.Mapping(tId))
        if tMap '= "" {
            set tObj = ##class(%DynamicObject).%FromJSON(tMap)
            set tObj.id = tId
            do tArr.%Push(tObj)
        }
        set tId = $order(^demo.Mapping(tId))
    }
    quit tArr.%ToJSON()
}

/// 请求触发一次转换（FHIRSyncService 定时增量同步中，队列表逐条处理）。
/// 返回字符串："OK" 表示运行正常，否则为错误描述。
ClassMethod TriggerFetch() As %String
{
    If '##class(Ens.Director).IsProductionRunning() Quit "Production 未运行，请先生成管道"
    Quit "OK（FHIRSyncService 定时增量同步中，队列表逐条处理）"
}

/// 返回 Production 运行状态（是否在运行）
ClassMethod ProductionStatus() As %Boolean
{
    quit ##class(Ens.Director).IsProductionRunning()
}

/// 启动 Production（若已运行返回 ALREADY_RUNNING）。
/// 供验证-修复 Agent 的「start」动作使用。
/// 注意：容器强制停止（如冻结重启）会留下「未干净关闭」状态，Ens 拒绝直接启动，
/// 必须先 Stop 清除状态再 Start。
ClassMethod StartProduction(pProduction As %String = "demo.DataflowProduction") As %String
{
    If ##class(Ens.Director).IsProductionRunning() Quit "ALREADY_RUNNING"
    Do ##class(Ens.Director).StopProduction(pProduction, 1)
    Set tSC = ##class(Ens.Director).StartProduction(pProduction)
    If $$$ISOK(tSC) Quit "OK"
    Quit $system.Status.GetErrorText(tSC)
}

/// 返回增量同步游标（上次同步的 _lastUpdated 时间戳）
ClassMethod GetSyncCursor() As %String
{
    quit $Get(^demo.Config("sync", "cursor"), "")
}

/// 重置增量同步游标（清空后下次同步从头抓取；Demo 一般不使用）
ClassMethod ResetSyncCursor() As %String
{
    kill ^demo.Config("sync", "cursor")
    quit "OK"
}

/// 编译状态检查：重新编译 demo.DataflowProduction（幂等），返回 JSON
///   {"ok":true} 或 {"ok":false,"message":"错误详情"}
ClassMethod CheckCompile() As %String
{
    Set tSC = $system.OBJ.Compile("demo.DataflowProduction", "-d")
    Set tObj = {"ok":($$$ISOK(tSC))}
    If '$$$ISOK(tSC) {
        Do tObj.%Set("message", $system.Status.GetErrorText(tSC))
    }
    Quit tObj.%ToJSON()
}

/// 沉淀验证问题模式与解决方案到 ^demo.ValidationIssue（供后续验证 Agent prompt 注入）
ClassMethod SaveValidationIssue(pPattern As %String, pResolution As %String, pSource As %String = "generate") As %String
{
    Set tId = $Increment(^demo.ValidationIssue)
    Set ^demo.ValidationIssue(tId) = pPattern _ "|" _ pResolution _ "|" _ pSource
    Quit "OK"
}

/// 返回最近 limit 条验证问题经验 JSON 数组
///   [{"id":1,"pattern":"...","resolution":"...","source":"..."}]
ClassMethod GetValidationIssues(pLimit As %Integer = 5) As %String
{
    Set tArr = []
    Set tId = $Order(^demo.ValidationIssue(""), -1)
    Set tN = 0
    While (tId '= "") && (tN < pLimit) {
        Set tVal = $Get(^demo.ValidationIssue(tId))
        If tVal '= "" {
            Do tArr.%Push({"id":(tId),
                           "pattern":($Piece(tVal, "|", 1)),
                           "resolution":($Piece(tVal, "|", 2)),
                           "source":($Piece(tVal, "|", 3))})
        }
        Set tId = $Order(^demo.ValidationIssue(tId), -1)
        Set tN = tN + 1
    }
    Quit tArr.%ToJSON()
}

}
