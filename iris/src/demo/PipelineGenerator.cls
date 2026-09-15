Class demo.PipelineGenerator Extends %RegisteredObject
{

/// 生成并启动数据管道 Production。
/// 根据用户确认的转换关系（Mapping）动态生成 demo.DataflowProduction 类
/// （XData ProductionDefinition），编译并启动。
///
/// 参数:
///   mappingsJson: 转换关系 JSON 数组字符串
///     [{"id":"M1","source":"Patient","target_table":"Patient",
///       "field_mappings":[{"source":"id","target":"ID"},
///                         {"source":"name[0].family","target":"FamilyName"},
///                         {"source":"name[0].given[0]","target":"GivenName"},
///                         {"source":"gender","target":"Gender"},
///                         {"source":"birthDate","target":"BirthDate","transform":"date"},
///                         {"source":"telecom[0].value","target":"Phone"},
///                         {"source":"address[0].line[0]","target":"Address"},
///                         {"source":"address[0].city","target":"City"}]}]
///   configJson: 可选，FHIR 数据源配置 JSON 字符串
///     {"endpoint":"http://iris:52773/csp/healthshare/fhirserver/fhir/r4/",
///      "username":"superuser","password":"SYS"}
ClassMethod Generate(mappingsJson As %String, configJson As %String = "") As %String
{
    // 结果字符串："OK" 表示成功，否则为错误描述
    Set tResult = "OK"
    Try {
        // 1. 保存 FHIR 数据源配置（endpoint / 认证）
        If configJson '= "" {
            Set tCfg = ##class(%DynamicObject).%FromJSON(configJson)
            Set ^demo.Config("fhir", "endpoint") = tCfg.endpoint
            Set ^demo.Config("fhir", "username") = tCfg.username
            Set ^demo.Config("fhir", "password") = tCfg.password
        }

        // 2. 保存转换关系（Mapping）
        Set tArr = ##class(%DynamicObject).%FromJSON(mappingsJson)
        For tI = 0:1:tArr.%Size()-1 {
            Set tM = tArr.%Get(tI)
            Set ^demo.Mapping(tM.id) = tM.%ToJSON()
        }

        // 3. 动态生成 Production 类并编译
        Set tSC = ..GenerateProduction()
        If $$$ISERR(tSC) {
            Set tResult = $system.Status.GetErrorText(tSC)
        } Else {
            // 4. 启动 Production（先 Stop 清状态：已运行需重启，未干净关闭需清 Ens 状态，
            //    否则报 ErrProductionNotShutdownCleanly）
            Do ##class(Ens.Director).StopProduction("demo.DataflowProduction", 1)
            Set tSC2 = ##class(Ens.Director).StartProduction("demo.DataflowProduction")
            If $$$ISERR(tSC2) {
                Set tResult = $system.Status.GetErrorText(tSC2)
            }
        }
    } Catch ex {
        Set tResult = $system.Status.GetErrorText(ex.AsStatus())
    }
    Quit tResult
}

/// 生成并编译 demo.DataflowProduction 类（写入 .cls 文件后编译，比动态 ClassDefinition 更可靠）
ClassMethod GenerateProduction() As %Status
{
    // 构建有效管道拓扑（阶段2：LLM 拓扑建议优先，校验 + 按 mapping 补全 SQLOp）
    Set tTopoJson = ..BuildEffectiveTopology()

    // 渲染 Production XData（按拓扑 + 组件模板）
    Set tXml = ..RenderProduction(tTopoJson)

    // 写入可写目录 /dur/generated（/shared/src 为只读挂载，不能写）
    Set tDir = "/dur/generated"
    If '##class(%File).DirectoryExists(tDir) {
        Do ##class(%File).CreateDirectory(tDir)
    }
    Set tFile = tDir _ "/DataflowProduction.cls"
    Set tBody = "Class demo.DataflowProduction Extends Ens.Production" _ $C(10)
    Set tBody = tBody _ "{" _ $C(10)
    Set tBody = tBody _ "XData ProductionDefinition" _ $C(10)
    Set tBody = tBody _ "{" _ $C(10)
    Set tBody = tBody _ tXml _ $C(10)
    Set tBody = tBody _ "}" _ $C(10)
    Set tBody = tBody _ "}" _ $C(10)

    Set tStream = ##class(%Stream.FileCharacter).%New()
    Set tSC = tStream.LinkToFile(tFile)
    If $$$ISERR(tSC) Quit tSC
    Do tStream.Write(tBody)
    Set tSC = tStream.%Save()
    If $$$ISERR(tSC) Quit tSC

    // 编译（Load 会覆盖旧类定义）
    Set tSC = $SYSTEM.OBJ.Load(tFile, "ck")
    Quit tSC
}

/// 构建默认管道拓扑（FHIR 源 → DB 目标），返回完整拓扑 JSON。
/// 拓扑结构 = {"production": "...", "components": [{"type","name","className","comment",
///             "settings": [{"target","name","value"}]}]}
/// 兜底路径：Python（pipelines.py build_pipeline_topology）构建的完整拓扑优先；
/// 本方法仅当 ^demo.Config 无拓扑时兜底。
ClassMethod BuildDefaultTopology() As %String
{
    Set tArr = []
    // 固定组件：FHIR 增量同步 + 队列表逐条处理 + 字段映射转换（无设置）
    Do tArr.%Push({"type":"FHIRSyncService","name":"FHIRSyncService","className":"demo.FHIRSyncService","comment":"定时增量同步抓取","settings":[]})
    Do tArr.%Push({"type":"FHIRService","name":"FHIRService","className":"demo.FHIRService","comment":"队列表逐条处理（定时器，每条独立会话）","settings":[]})
    Do tArr.%Push({"type":"TransformProcess","name":"TransformProcess","className":"demo.TransformProcess","comment":"字段映射转换","settings":[]})
    // 动态组件：每个已确认目标表生成一个 SQL Operation（写入侧，JDBC，UPSERT 幂等）
    Set tId = $Order(^demo.Mapping(""))
    While tId '= "" {
        Set tMapJson = $Get(^demo.Mapping(tId))
        If tMapJson '= "" {
            Set tMap = ##class(%DynamicObject).%FromJSON(tMapJson)
            Set tTable = tMap.%Get("target_table")
            Do tArr.%Push(..BuildSQLOpComponent(tTable))
        }
        Set tId = $Order(^demo.Mapping(tId))
    }
    // 基础组件：Java 网关（SQL Operation JDBC 连接）
    Do tArr.%Push({"type":"JavaGateway","name":"EnsLib.JavaGateway.Service","className":"EnsLib.JavaGateway.Service","comment":"Java 网关（SQL Operation JDBC 连接）",
        "settings":[{"target":"Host","name":"%gatewayName","value":"%Java Server"}]})
    Set tTopo = {"production":"demo.DataflowProduction","components":(tArr)}
    Quit tTopo.%ToJSON()
}

/// 按目标表构建 SQL Operation 组件（className + settings，UPSERT 幂等）。
ClassMethod BuildSQLOpComponent(pTable As %String) As %DynamicObject
{
    Set tCols = ..GetTableColumns(pTable)
    If tCols = "" {
        Quit {"type":"SQLOperation","name":("SQLOp_"_pTable),"className":"EnsLib.SQL.Operation.GenericOperation","comment":("写入目标表 "_pTable),"settings":[]}
    }
    Set tColsList = $ListFromString(tCols, ",")
    Set tColsSql = "", tParams = "", tQ = ""
    For tI = 1:1:$ListLength(tColsList) {
        Set tCol = $ListGet(tColsList, tI)
        If tI > 1 {
            Set tColsSql = tColsSql _ ", "
            Set tParams = tParams _ ","
            Set tQ = tQ _ ","
        }
        Set tColsSql = tColsSql _ tCol
        Set tParams = tParams _ "*" _ tCol
        Set tQ = tQ _ "?"
    }
    Quit {"type":"SQLOperation","name":("SQLOp_"_pTable),"className":"EnsLib.SQL.Operation.GenericOperation","comment":("写入目标表 "_pTable),
        "settings":[{"target":"Adapter","name":"DSN","value":"localTarget"},
                    {"target":"Adapter","name":"JGService","value":"EnsLib.JavaGateway.Service"},
                    {"target":"Host","name":"Query","value":("INSERT OR UPDATE INTO "_pTable_" ("_tColsSql_") VALUES ("_tQ_")")},
                    {"target":"Host","name":"InputParameters","value":(tParams)}]}
}


/// 构建「有效」管道拓扑：
///   1. 优先使用 Python 构建的完整拓扑（^demo.Config("pipeline","topology")），否则用默认拓扑兜底；
///   2. production 字段兜底为 demo.DataflowProduction。
///   组件组成/className/settings 由 Python（类型注册表 + mapping 列结构）权威决定，
///   本方法不再按 type 校验/补全（渲染器为通用渲染）。
ClassMethod BuildEffectiveTopology() As %String
{
    Set tTopoJson = $Get(^demo.Config("pipeline", "topology"))
    If tTopoJson = "" {
        Set tTopoJson = ..BuildDefaultTopology()
    }
    Set tTopo = ##class(%DynamicObject).%FromJSON(tTopoJson)
    If tTopo = "" Quit tTopoJson
    Set tProd = tTopo.production
    If tProd = "" {
        Set tTopo.production = "demo.DataflowProduction"
        Quit tTopo.%ToJSON()
    }
    Quit tTopoJson
}


/// 按拓扑 JSON 渲染 Production XData（遍历组件 → 模板分发 → 拼接）。
ClassMethod RenderProduction(pTopoJson As %String) As %String
{
    Set tTopo = ##class(%DynamicObject).%FromJSON(pTopoJson)
    // 兜底：production 字段缺失时使用默认名称
    Set tProd = tTopo.production
    If tProd = "" Set tProd = "demo.DataflowProduction"
    Set tXml = "<Production Name=""" _ tProd _ """ LogGeneralTraceEvents=""false"">"
            _ "<Description>AI 数据自动化转换 Demo - 数据管道</Description>"
            _ "<ActorPoolSize>2</ActorPoolSize>"
    Set tArr = tTopo.components
    For tI = 0:1:tArr.%Size()-1 {
        Set tXml = tXml _ ..RenderComponent(tArr.%Get(tI))
    }
    Set tXml = tXml _ "</Production>"
    Quit tXml
}

/// 按组件配置通用渲染单个组件 XData 片段（不再按 type 分发）。
/// 组件结构 = {"type","name","className","comment","category",
///             "settings":[{"target":"Adapter|Host","name","value"}]}
/// className 必须非空（由 Python 类型注册表保证；缺失则跳过该组件）。
/// category = 所属「数据管道」类别（如 sql2fhir-patient-tx / sql2soap；共享组件为 shared），
/// 渲染为 Ens 业务主机的 Category，供 UI/许可预算按管道分组（为空则不输出该属性）。
ClassMethod RenderComponent(pCfg As %DynamicObject) As %String
{
    Set tClass = pCfg.className
    If tClass = "" Quit ""
    Set tName = pCfg.name
    Set tComment = pCfg.comment
    Set tCategory = pCfg.category
    ; enabled 字段（缺省 true）：许可调度下放不下的管道分组渲染为 Enabled="false"
    ; → 组件生成但处于停用状态（可一键切换），避免 Production 启动即耗尽许可单元
    Set tEnabled = "true"
    If 'pCfg.%Get("enabled", 1) Set tEnabled = "false"
    Set tXml = "<Item Name=""" _ tName _ """ ClassName=""" _ tClass _ """ PoolSize=""1"" Enabled=""" _ tEnabled _ """ Foreground=""false"""
    If tCategory '= "" Set tXml = tXml _ " Category=""" _ tCategory _ """"
    Set tXml = tXml _ " Comment=""" _ tComment _ """"
    Set tSettings = pCfg.settings
    If $isobject(tSettings) && (tSettings.%Size() > 0) {
        Set tXml = tXml _ ">"
        For tI = 0:1:tSettings.%Size()-1 {
            Set tS = tSettings.%Get(tI)
            Set tXml = tXml _ "<Setting Target=""" _ tS.target _ """ Name=""" _ tS.name _ """>" _ tS.value _ "</Setting>"
        }
        Set tXml = tXml _ "</Item>"
    } Else {
        Set tXml = tXml _ "/>"
    }
    Quit tXml
}


/// 查询目标表的列名列表（逗号分隔，SQLUser schema；供动态生成 SQL Operation）
ClassMethod GetTableColumns(pTable As %String) As %String
{
    Set tRS = ##class(%SQL.Statement).%New()
    Set tSC = tRS.%Prepare(
        "SELECT column_name FROM information_schema.columns "
        _ "WHERE table_schema='SQLUser' AND table_name='" _ pTable _ "' ORDER BY ordinal_position")
    If $$$ISERR(tSC) Quit ""
    Set tRset = tRS.%Execute()
    Set tCols = ""
    While tRset.%Next() {
        Set tCols = tCols _ "," _ tRset.%Get("column_name")
    }
    Quit $Extract(tCols, 2, *)
}

}
