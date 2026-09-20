Class demo.TerminologyOperation Extends Ens.BusinessOperation
{

Parameter SETTINGS = "TermServer:Basic,TermPort:Basic,Timeout:Basic";

/// 术语服务器主机（缺省 = compose 内容器名，与 backend 的 TERMSRV_BASE 同一目标）
Property TermServer As %String [ InitialExpression = "iris-terminology" ];

/// 术语服务器 Web 端口（容器内 IRIS 超级服务器端口）
Property TermPort As %Integer [ InitialExpression = 52773 ];

/// 单次查询超时（秒）
Property Timeout As %Integer [ InitialExpression = 5 ];

/// **共享术语转换 BO**（IRIS 上术语能力的唯一出口）。
///
/// 作用：任何数据管道的 Business Process 都通过
///   `Set tSC=..SendRequestSync("TerminologyOperation", tReq, .tResp, 5)`
/// 取得"源编码 → 目标体系编码"的判定结果 —— 各管道不再各自连 HTTP，也不在本地保留码表副本。
///
/// 事实源：术语服务器 `Terminology.Mapping.CodeMap`（`/terminology/mapping/lookup`）。
/// 本 BO 只做**检索**，不做判定、不复制码表（判定归 C3/C3-Dx，与项目红线一致）。
///
/// ⚠ ObjectScript 事实：`Ens.BusinessOperation` **没有 `%New()`**（实测
/// `<METHOD DOES NOT EXIST> *%New,Ens.BusinessOperation`）→ 业务主机只能由 Ensemble 框架实例化，
/// 脚本里 `%New()` 会得到无效 OREF。故实现全部落在 **ClassMethod `Resolve()`**（可独立测试），
/// `OnMessage` 只做薄封装 —— 需要脚本自测时直接调 `##class(demo.TerminologyOperation).Resolve(...)`。
Method OnMessage(pRequest As demo.TermLookupRequest, Output pResponse As demo.TermLookupResponse) As %Status
{
    Set pResponse = ##class(demo.TerminologyOperation).Resolve(pRequest.Skill, pRequest.SourceCode,
        pRequest.SourceSystem, pRequest.TargetSystem, ..TermServer, +..TermPort, +..Timeout)
    Quit $$$OK
}

/// 术语查询实现（**无实例依赖**，可直接脚本调用 / 复用）：
/// 调术语服务器 `/terminology/mapping/lookup` 并归一为 `demo.TermLookupResponse`。
/// 参数 pServer/pPort/pTimeout 留空时回落 `^demo.Config("termsrv", …)`（生成端写入）与内置缺省。
ClassMethod Resolve(pSkill As %String, pSourceCode As %String,
                    pSourceSystem As %String = "", pTargetSystem As %String = "",
                    pServer As %String = "", pPort As %Integer = 0,
                    pTimeout As %Integer = 0) As demo.TermLookupResponse
{
    Set tRsp = ##class(demo.TermLookupResponse).%New()
    Try {
        Set tSkill = pSkill
        Set tCode = pSourceCode
        If (tSkill = "") || (tCode = "") {
            Set tRsp.Status = "error"
            Set tRsp.ErrorText = "术语查询参数不完整（skill / sourceCode 必填）"
            Quit
        }
        Set tSrc = pSourceSystem
        If tSrc = "" Set tSrc = ..SourceSystemFor(tSkill)
        Set tTgt = pTargetSystem
        If tTgt = "" Set tTgt = ..TargetSystemFor(tSkill)
        If (tSrc = "") || (tTgt = "") {
            Set tRsp.Status = "error"
            Set tRsp.ErrorText = "未登记的术语判定 Skill: " _ tSkill
            Quit
        }
        // 服务器地址：优先 Production 配置的类属性，留空时回落 ^demo.Config("termsrv")（生成端写入）
        Set tHost = pServer
        If tHost = "" Set tHost = $Get(^demo.Config("termsrv", "host"), "iris-terminology")
        Set tPort = +pPort
        If tPort <= 0 Set tPort = +$Get(^demo.Config("termsrv", "port"), 52773)
        Set tTimeout = +pTimeout
        If tTimeout <= 0 Set tTimeout = 5

        Set tHttp = ##class(%Net.HttpRequest).%New()
        Set tHttp.Server = tHost
        Set tHttp.Port = tPort
        Set tHttp.Timeout = tTimeout
        Set tUrl = "/terminology/mapping/lookup?sourceSystem=" _ $ZCONVERT(tSrc, "O", "URL")
        Set tUrl = tUrl _ "&targetSystem=" _ $ZCONVERT(tTgt, "O", "URL")
        Set tUrl = tUrl _ "&sourceCode=" _ $ZCONVERT(tCode, "O", "URL")
        Do tHttp.Get(tUrl)
        Set tHttpStatus = tHttp.HttpResponse.StatusCode
        If tHttpStatus '= 200 {
            Set tRsp.Status = "error"
            Set tRsp.ErrorText = "术语服务器 HTTP " _ tHttpStatus _ "（" _ tHost _ ":" _ tPort _ "）"
            Quit
        }
        Set tBody = tHttp.HttpResponse.Data.Read()
        Set tObj = ##class(%DynamicObject).%FromJSON(tBody)
        If '$IsObject(tObj) {
            Set tRsp.Status = "error"
            Set tRsp.ErrorText = "术语服务器返回体不是 JSON"
            Quit
        }
        Set tFound = +tObj.%Get("found")
        If tFound = 0 {
            // 服务器尚无该源编码的判定 → 平台默认降级（保留源 coding + 未映射标记），不视为失败
            Set tRsp.Status = "missing"
            Quit
        }
        Set tRsp.Status = tObj.%Get("status")
        Set tRsp.Method = tObj.%Get("method")
        Set tRsp.TargetSystem = tObj.%Get("targetSystem")
        Set tRsp.TargetCode = tObj.%Get("targetCode")
        Set tRsp.TargetDisplay = tObj.%Get("targetDisplay")
        // 顶层字段缺失时从 items[0] 兜底（服务器两处都会回填，取其一即可）
        Set tItems = tObj.%Get("items")
        If $IsObject(tItems) {
            Set tSize = tItems.%Size()
            If tSize > 0 {
                Set tIt = tItems.%Get(0)
                If $IsObject(tIt) {
                    If tRsp.TargetSystem = "" Set tRsp.TargetSystem = tIt.%Get("TargetSystem")
                    If tRsp.TargetCode = "" Set tRsp.TargetCode = tIt.%Get("TargetCode")
                    If tRsp.TargetDisplay = "" Set tRsp.TargetDisplay = tIt.%Get("TargetDisplay")
                    If tRsp.Method = "" Set tRsp.Method = tIt.%Get("Method")
                    If tRsp.Confidence = "" Set tRsp.Confidence = tIt.%Get("Confidence")
                }
            }
        }
    } Catch ex {
        Set tRsp.Status = "error"
        Set tRsp.ErrorText = ex.DisplayString()
    }
    Quit tRsp
}

/// skill → 源体系 URI（平台静态登记，与 backend/services/transform_directives.py::TERM_SKILLS 同源；
/// ⚠ 两处必须同步，新增判定 Skill 时同时登记）。
ClassMethod SourceSystemFor(pSkill As %String) As %String
{
    If pSkill = "cn2snomed" Quit "urn:cn-nhsa:icd10-gbt2016"
    If pSkill = "cn2rx" Quit "urn:cn-nhsa:drug-nrdl"
    Quit ""
}

/// skill → 目标体系 URI（与 TERM_SKILLS 的 target_system 同步）。
ClassMethod TargetSystemFor(pSkill As %String) As %String
{
    If pSkill = "cn2snomed" Quit "http://snomed.info/sct"
    If pSkill = "cn2rx" Quit "http://www.nlm.nih.gov/research/umls/rxnorm"
    Quit ""
}

}
