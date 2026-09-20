Class demo.TermLookupRequest Extends Ens.Request
{

/// 术语判定 Skill（对应平台 term_map:<skill>，如 cn2snomed / cn2rx）
Property Skill As %String;

/// 源编码值（如国标 ICD-10 E11.900 / 医保药品码 NRDL-xxxx）
Property SourceCode As %String;

/// 源体系 URI（留空则由 demo.TerminologyOperation 按 Skill 推导）
Property SourceSystem As %String;

/// 目标体系 URI（留空则由 demo.TerminologyOperation 按 Skill 推导）
Property TargetSystem As %String;

}
