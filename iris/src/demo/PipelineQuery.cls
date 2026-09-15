Class demo.PipelineQuery Extends %RegisteredObject
{

/// 保存"最近一次编译/验证通过的 Agent BP 源码"（`^demo.Config` 存档，供生成失败时自动回滚）。
ClassMethod SaveBpLastGood(pSource As %String) As %String
{
    If pSource = "" Quit "empty"
    Set ^demo.Config("bp", "last_good") = pSource
    Set ^demo.Config("bp", "last_good_at") = $ZDateTime($Horolog, 3)
    Quit "saved:"_$Length(pSource)
}

/// 读取"最近一次编译/验证通过的 Agent BP 源码"（空=无存档）。
ClassMethod GetBpLastGood() As %String
{
    Quit $Get(^demo.Config("bp", "last_good"))
}


ClassMethod FindDSNByUrl(pUrl As %String) As %String
{
    If pUrl = "" Quit ""
    Set rs = ##class(%SQL.Statement).%ExecDirect(,"SELECT Connection_Name FROM %Library.sys_SQLConnection WHERE DSN = ? OR URL = ?", pUrl, pUrl)
    If $IsObject(rs), rs.%Next() Quit rs.%Get("Connection_Name")
    Quit ""
}


/// 幂等确保 JDBC DSN 存在（SQL 源轮询用）：不存在则按 jdbc url 创建。
/// 背景：数据源的 jdbc_url 指向某个 namespace（如 USER）时，平台按 URL 推导出的 DSN 名
/// 未必真实存在（系统里只有 localTarget / CLINIC），BS 会退化成 ODBC 或报 Access Denied。
/// 返回 "exists" / "created" / "failed:<原因>" / "skipped"。
ClassMethod EnsureJDBCDSN(pName As %String, pUrl As %String, pUser As %String = "superuser", pPwd As %String = "SYS") As %String
{
    If (pName = "") || (pUrl = "") Quit "skipped"
    Set stmt = ##class(%SQL.Statement).%New()
    Set sc0 = stmt.%Prepare("SELECT connection_name FROM %Library.sys_SQLConnection WHERE connection_name = ?")
    If $System.Status.IsOK(sc0) {
        Set r = stmt.%Execute(pName)
        If r.%Next() Quit "exists"
    }
    Set c = ##class(%SQLConnection).%New()
    Set c.Name = pName
    Set c.DSN = pUrl
    Set c.URL = pUrl
    Set c.Usr = pUser
    Set c.pwd = pPwd
    Set c.driver = "com.intersystems.jdbc.IRISDriver"
    Set c.isJDBC = 1
    Set sc = c.%Save()
    If $System.Status.IsOK(sc) Quit "created"
    Quit "failed:"_$System.Status.GetErrorText(sc)
}

/// 目标表白名单（防止表名注入，Demo 固定目标表 + SOAP 远端落库演示表）
ClassMethod IsAllowedTable(pTable As %String) As %Boolean
{
    if (pTable = "Patient") || (pTable = "Observation") || (pTable = "PatientEntity") || (pTable = "PatientSource") quit 1
    quit 0
}

/// 将消息状态码映射为可读状态（权威来源 = Ens.DataType.MessageStatus，勿手写码值）
/// 码值对照（IRIS 定义）：1=Created 2=Queued 3=Delivered 4=Discarded 5=Suspended
///                       6=Deferred 7=Aborted 8=Error 9=Completed
/// 历史 bug：曾手写成 7=Cancelled / 6=Submitted / 其余 Other，导致真实 Aborted 被降级、
/// Completed 被误读（pipeline_validator 的 smoke 判定随之失真），现统一走 LogicalToDisplay()。
ClassMethod MapStatus(pStatus As %String) As %String
{
    Set tText = ##class(Ens.DataType.MessageStatus).LogicalToDisplay(+pStatus)
    If tText '= "" Quit tText
    Quit "Other("_pStatus_")"
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

/// 把「数据管道设计 Agent 生成的 BP 类源码」落地为 /dur/generated/<类名>.cls 并 Load 编译。
/// 用途：BP 是 Agent 生成产物（平台不预置任何 BP）；本方法只是编译准入工具。
/// 返回 JSON {"ok":bool, "message":str}（编译失败带 IRIS 错误文本，供回 feed Agent 修复）。
ClassMethod AddGeneratedBpSource(pClassName As %String, pSource As %String) As %String
{
    Set tOk = 0
    Set tMsg = ""
    Try {
        If pSource = "" {
            Set tMsg = "BP 源码为空"
            Quit
        }
        If pClassName = "" Set pClassName = "demo.SqlFhirPatientTxProcess"
        Set tShort = $PIECE(pClassName, ".", $LENGTH(pClassName, "."))
        If (tShort '= "SqlFhirPatientTxProcess") {
            Set tMsg = "类名白名单不符（仅 demo.SqlFhirPatientTxProcess）: " _ pClassName
            Quit
        }
        If pSource [ "xecute" || (pSource [ "$zf") || (pSource [ "EnsPortal") {
            Set tMsg = "BP 源码含被禁内容（xecute/$zf/Web Application），拒绝准入"
            Quit
        }
        Set tPath = "/dur/generated/" _ tShort _ ".cls"
        Set tFs = ##class(%Stream.FileCharacter).%New()
        Set tSC = tFs.LinkToFile(tPath)
        If $$$ISERR(tSC) {
            Set tMsg = "无法打开写入文件: " _ $system.Status.GetErrorText(tSC)
            Quit
        }
        Do tFs.Write(pSource)
        Set tSC = tFs.%Save()
        Do tFs.%Close()
        If $$$ISERR(tSC) {
            Set tMsg = "写文件失败: " _ $system.Status.GetErrorText(tSC)
            Quit
        }
        Set tSC = $system.OBJ.Load(tPath, "ck")
        If $$$ISOK(tSC) {
            Set tOk = 1
            Set tMsg = "编译成功: " _ tShort
        } Else {
            Set tMsg = $system.Status.GetErrorText(tSC)
        }
    } Catch ex {
        Set tErr = ex.AsStatus()
        If $EXTRACT(tErr) = "<" {
            Set tMsg = $system.Status.GetErrorText(tErr)
        } Else {
            Set tMsg = tErr
        }
    }
    Set tO = ##class(%DynamicObject).%New()
    Do tO.%Set("ok", tOk)
    Do tO.%Set("message", tMsg)
    Quit tO.%ToJSON()
}

/// 读取 Agent 生成的 BP 类源码文本（供平台做方法级受控替换）。
ClassMethod GetGeneratedBpSource(pClassName As %String = "demo.SqlFhirPatientTxProcess") As %String
{
    Set tShort = $PIECE(pClassName, ".", $LENGTH(pClassName, "."))
    Set tPath = "/dur/generated/" _ tShort _ ".cls"
    If '##class(%File).Exists(tPath) Quit ""
    Set tStream = ##class(%Stream.FileCharacter).%New()
    Set tSC = tStream.LinkToFile(tPath)
    If $$$ISERR(tSC) Quit ""
    Set tText = tStream.Read()
    Do tStream.%Close()
    Quit tText
}

/// 读取 Production 组件清单（名称/类名/启用状态），返回 JSON 数组。
/// 用途：许可预算（社区版 KeyLicenseUnits=8，每个 Ens 业务主机进程占 1 个）
/// 与前端「组件启停」面板。
ClassMethod GetItems(pProduction As %String = "demo.DataflowProduction") As %String
{
    Set tArr = []
    Set tProd = ##class(Ens.Config.Production).%OpenId(pProduction)
    If '$IsObject(tProd) Quit "[]"
    For i = 1:1:tProd.Items.Count() {
        Set tItem = tProd.Items.GetAt(i)
        Continue:'$IsObject(tItem)
        Set tObj = {}
        Do tObj.%Set("name", tItem.Name)
        Do tObj.%Set("className", tItem.ClassName)
        Do tObj.%Set("enabled", +tItem.Enabled)
        Do tObj.%Set("category", tItem.Category)
        Do tArr.%Push(tObj)
    }
    Quit tArr.%ToJSON()
}

/// 批量设置组件启用/禁用（离线改 Production 定义并保存；生成管道前调用）。
/// 返回 "changed:<数量>:ok" 或错误文本。
ClassMethod SetItemsEnabled(pProduction As %String, pNamesCsv As %String, pEnabled As %Integer = 1) As %String
{
    Set tProd = ##class(Ens.Config.Production).%OpenId(pProduction)
    If '$IsObject(tProd) Quit "no-production"
    Set tNames = ",", tCount = 0
    For p = 1:1:$Length(pNamesCsv, ",") {
        Set tN = $ZStrip($Piece(pNamesCsv, ",", p), "<>W")
        If tN '= "" Set tNames = tNames _ tN _ ","
    }
    If tNames = "," Quit "changed:0:ok"
    For i = 1:1:tProd.Items.Count() {
        Set tItem = tProd.Items.GetAt(i)
        Continue:'$IsObject(tItem)
        If $Find(tNames, "," _ tItem.Name _ ",") > 0 {
            If +tItem.Enabled '= pEnabled {
                Set tItem.Enabled = pEnabled
                Set tCount = tCount + 1
            }
        }
    }
    Set tSC = tProd.%Save()
    If $$$ISERR(tSC) Quit "failed:"_$System.Status.GetErrorText(tSC)
    Quit "changed:"_tCount_":ok"
}

/// 让不在「本次激活名单」内的业务主机让出许可（许可预算）。
/// 说明：IRIS 社区版 KeyLicenseUnits=8，每个 Ens 业务主机（BS/BP/BO）常驻 1 个许可单元；
/// 多管道 Production 整体启动会耗尽许可，导致后端无法连接、管道生成校验全部失败。
/// 等效于用户手工「生成新管道前先关闭旧管道的业务组件」——这里自动化：
///   - 保留基础设施组件（JavaGateway 等，ClassName 含 JavaGateway）；
///   - 保留 pKeepCsv 中的组件（本次管道要用的）；
///   - 其余业务主机 Enabled=0。
/// 返回被让路的组件名（逗号分隔；空串=无需变更）。
ClassMethod DisableNonActiveItems(pProduction As %String = "demo.DataflowProduction", pKeepCsv As %String = "") As %String
{
    Set tProd = ##class(Ens.Config.Production).%OpenId(pProduction)
    If '$IsObject(tProd) Quit ""
    Set tKeep = ",", tChanged = ""
    For p = 1:1:$Length(pKeepCsv, ",") {
        Set tN = $ZStrip($Piece(pKeepCsv, ",", p), "<>W")
        If tN '= "" Set tKeep = tKeep _ tN _ ","
    }
    For i = 1:1:tProd.Items.Count() {
        Set tItem = tProd.Items.GetAt(i)
        Continue:'$IsObject(tItem)
        Continue:tItem.ClassName["JavaGateway"
        Continue:+tItem.Enabled=0
        Continue:$Find(tKeep, ","_tItem.Name_",")>0
        Set tItem.Enabled = 0
        Set tChanged = tChanged _ $Select(tChanged="":"",1:",") _ tItem.Name
    }
    If tChanged '= "" Do tProd.%Save()
    Quit tChanged
}

/// 按「数据管道类别」批量启停业务主机（许可预算的按管道 keep）。
/// 规则：Category 非空且不等于 "shared"（共享组件永不因管道切换停用）且不在 pKeepCsv 中
/// 的组件 → Enabled=pEnabled；返回被改动的组件名（逗号分隔）。
/// 与 DisableNonActiveItems（按名字 keep）配套使用：本方法负责「整条管道让路」，
/// 名字 keep 负责本次拓扑内的精确启用，两者结果合并即许可预算。
ClassMethod SetItemsEnabledByCategory(pProduction As %String = "demo.DataflowProduction", pKeepCsv As %String = "", pEnabled As %Integer = 0) As %String
{
    Set tProd = ##class(Ens.Config.Production).%OpenId(pProduction)
    If '$IsObject(tProd) Quit ""
    Set tKeep = ","
    For p = 1:1:$Length(pKeepCsv, ",") {
        Set tN = $ZStrip($Piece(pKeepCsv, ",", p), "<>W")
        If tN '= "" Set tKeep = tKeep _ tN _ ","
    }
    Set tChanged = ""
    For i = 1:1:tProd.Items.Count() {
        Set tItem = tProd.Items.GetAt(i)
        Continue:'$IsObject(tItem)
        Continue:tItem.ClassName["JavaGateway"
        Set tCat = tItem.Category
        If (tCat = "") || (tCat = "shared") Continue
        If $Find(tKeep, ","_tCat_",")>0 Continue
        If +tItem.Enabled = pEnabled Continue
        Set tItem.Enabled = pEnabled
        Set tChanged = tChanged _ $Select(tChanged="":"",1:",") _ tItem.Name
    }
    If tChanged '= "" Do tProd.%Save()
    Quit tChanged
}

/// 读取 Production 组件明细（含 Settings），返回 JSON 数组。
/// 用途：诊断/展示「共享组件被谁使用」——TargetConfigNames（谁给它派发）与
/// JGService（谁依赖 JavaGateway），支撑「共享组件使用方」诊断与前端展示。
ClassMethod GetItemsDetail(pProduction As %String = "demo.DataflowProduction") As %String
{
    Set tArr = []
    Set tProd = ##class(Ens.Config.Production).%OpenId(pProduction)
    If '$IsObject(tProd) Quit "[]"
    For i = 1:1:tProd.Items.Count() {
        Set tItem = tProd.Items.GetAt(i)
        Continue:'$IsObject(tItem)
        Set tObj = {}
        Do tObj.%Set("name", tItem.Name)
        Do tObj.%Set("className", tItem.ClassName)
        Do tObj.%Set("enabled", +tItem.Enabled)
        Do tObj.%Set("category", tItem.Category)
        Set tSet = {}
        For j = 1:1:tItem.Settings.Count() {
            Set tS = tItem.Settings.GetAt(j)
            Continue:'$IsObject(tS)
            Set tVal = tS.Value
            If tVal '= "" Do tSet.%Set(tS.Name, tVal)
        }
        Do tObj.%Set("settings", tSet)
        Do tArr.%Push(tObj)
    }
    Quit tArr.%ToJSON()
}

}
