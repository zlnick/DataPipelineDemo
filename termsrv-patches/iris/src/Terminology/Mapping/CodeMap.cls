Class Terminology.Mapping.CodeMap Extends %Persistent [ DdlAllowed, SqlTableName = CodeMap ]
{

/// 源编码体系（如 urn:cn-nhsa:icd10-gbt2016、urn:cn-nhsa:drug-nrdl）
Property SourceSystem As %String(MAXLEN = 200) [ Required ];

/// 源编码（**原样大小写**保存，运行期按此键取用）
Property SourceCode As %String(MAXLEN = 100) [ Required ];

/// 源编码归一形式（大写）——仅用于大小写不敏感匹配/唯一键（IRIS 默认排序规则大小写不敏感，
/// 但运行期 global 查键大小写敏感，故必须同时保留原样值与归一值）
Property SourceCodeU As %String(MAXLEN = 100);

/// 源编码显示名（可选，如"2型糖尿病"）
Property SourceDisplay As %String(MAXLEN = 500);

/// 目标编码体系（如 http://snomed.info/sct、http://www.nlm.nih.gov/research/umls/rxnorm）
Property TargetSystem As %String(MAXLEN = 200) [ Required ];

/// 目标编码（status=negative 时为空，表示"已判定无可用映射"）
Property TargetCode As %String(MAXLEN = 100);

/// 目标编码显示名
Property TargetDisplay As %String(MAXLEN = 500);

/// 产生方式：manual（人工核对）| llm（判定 Agent 产出）| derived（由既有词表推导）| import
Property Method As %String(MAXLEN = 20) [ InitialExpression = "manual" ];

/// 状态：active（可用）| negative（已判定无映射，不阻断、不追加 coding）| pending（待复核）| retired
Property Status As %String(MAXLEN = 20) [ InitialExpression = "active" ];

/// 置信度（0~1，llm 产出时填写）
Property Confidence As %Numeric;

/// 证据（召回来源/人工说明，便于复核与审计）
Property Evidence As %String(MAXLEN = 2000);

/// 备注
Property Note As %String(MAXLEN = 500);

Property CreatedAt As %TimeStamp;

Property UpdatedAt As %TimeStamp;

/// 唯一键：同一（源体系, 源码, 目标体系）只有一条
Index PK On (SourceSystem, SourceCodeU, TargetSystem) [ Unique ];

/// 目录/覆盖率查询用
Index IXPair On (SourceSystem, TargetSystem, Status);

Index IXTargetCode On (TargetSystem, TargetCode);

ClassMethod Normalize(pCode As %String) As %String
{
    // 归一：去首尾空白 + 大写（大小写不敏感匹配口径）
    Quit $ZCONVERT($ZSTRIP($Get(pCode), "<>W"), "U")
}

Method %OnBeforeSave(pInsert As %Boolean) As %Status [ Private, ServerOnly = 1 ]
{
    Set ..SourceCodeU = ..Normalize(..SourceCode)
    Set ..SourceCode = $ZSTRIP(..SourceCode, "<>W")
    Set tNow = $ZDATETIME($HOROLOG, 3)
    Set ..UpdatedAt = tNow
    // ⚠ 不可写 $Get(..CreatedAt)：属性引用不是变量，运行期报 "must be MultiDimensional"
    If ..CreatedAt = "" Set ..CreatedAt = tNow
    Quit $$$OK
}

}
