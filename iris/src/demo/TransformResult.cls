Class demo.TransformResult Extends Ens.Request
{

/// 转换关系 ID
Property MappingId As %String;

/// 目标表名（SQLUser schema）
Property TableName As %String;

/// 转换后的目标表行 JSON（由 TransformProcess 生成）
Property RowJson As %String(MAXLEN = 4000);

}
