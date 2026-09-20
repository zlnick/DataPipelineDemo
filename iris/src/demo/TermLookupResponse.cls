Class demo.TermLookupResponse Extends Ens.Response
{

/// 判定结果（服务器事实）：
///   active   = 可用映射 → 调用方追加目标体系 coding（双 coding）；
///   negative = 服务器已判定"池内无匹配" → 不追加、非失败；
///   missing  = 服务器尚无该源编码的判定 → 调用方**默认降级**（保留源 coding + 打未映射标记）；
///   error    = 调用失败（服务器不可达 / HTTP 非 200 / 返回体不可解析）→ 同上降级。
Property Status As %String;

/// 目标体系 URI（active 时有效）
Property TargetSystem As %String;

/// 目标编码（active 时有效）
Property TargetCode As %String;

/// 目标显示名
Property TargetDisplay As %String;

/// 置信度（服务器登记）
Property Confidence As %String;

/// 判定方式（llm / manual / seed）
Property Method As %String;

/// 失败原因（Status=error 时有值）
Property ErrorText As %String;

}
