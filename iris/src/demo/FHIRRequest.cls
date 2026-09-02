Class demo.FHIRRequest Extends Ens.Request
{

/// FHIR 资源类型（如 Patient）
Property ResourceType As %String;

/// FHIR 资源 ID
Property ResourceId As %String;

/// FHIR 资源 JSON 载荷（由 FHIRService 拉取后原样传递）
Property ResourceJson As %String(MAXLEN = 10000);

/// 关联的转换关系 ID（对应 ^demo.Mapping 中的键）
Property MappingId As %String;

}
