"""Pydantic 请求/响应数据模型（统一 API 结构）。"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ConnectionRuntime(BaseModel):
    """数据源/目标运行契约（Connection Contract）——参数归一单一事实源。

    见 docs/ConnectionContract-设计.md。记录以 dict 存储，本模型用于结构约定/校验。
    """

    kind: str = ""
    role: str = ""  # source | target
    connection: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    assets: list = Field(default_factory=list)
    poll: dict = Field(default_factory=dict)
    delivery: dict = Field(default_factory=dict)
    health: dict = Field(default_factory=lambda: {"ok": False, "checked_at": "", "detail": ""})


class AuthConfig(BaseModel):
    """数据源认证配置（方案 A：Basic Auth，IRIS FHIR Server 默认要求认证）。"""

    type: str = Field("basic", description="认证类型（示例仅 basic）")
    username: str = Field("superuser", description="用户名")
    password: str = Field("SYS", description="密码")


class DataSourceIn(BaseModel):
    """注册数据源请求。"""

    name: str = Field(..., description="数据源名称")
    type: str = Field("FHIR", description="数据源类型（FHIR/SQL）")
    endpoint: str = Field("", description="FHIR endpoint 基础 URL（SQL 源可空）")
    config: Optional[dict] = Field(None, description="类型相关配置（SQL: dsn/query/key_field）")
    auth: AuthConfig = Field(default_factory=AuthConfig, description="认证配置")


class AssetDiscovery(BaseModel):
    """数据资产结构描述（供 AI 推荐输入）。"""

    name: str = Field(..., description="资产名（资源类型）")
    fields: list[str] = Field(default_factory=list, description="字段/路径列表")


class SourceAssetModel(BaseModel):
    """可供转换计划使用的源资产模型。"""

    id: Optional[str] = None
    name: str
    type: str = "SQL_TABLE"
    source_id: Optional[str] = None
    fields: list[str] = Field(default_factory=list)
    structure: Optional[dict] = None
    description: Optional[str] = None
    status: Optional[str] = None
    tables: list[dict] = Field(default_factory=list)
    relationships: list[dict] = Field(default_factory=list)


class TargetInterfaceModel(BaseModel):
    """目标接口模型（数据库表、SOAP 服务或其他投递端）。"""

    id: Optional[str] = None
    name: str
    type: str = "DB"
    target_id: Optional[str] = None
    endpoint: Optional[str] = None
    table: Optional[str] = None
    schema_name: Optional[str] = Field(None, alias="schema")
    columns: list = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    connection: Optional[dict] = None
    tables: list[dict] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class TransformationPlan(BaseModel):
    """可持久化的转换计划。"""

    id: Optional[str] = None
    name: Optional[str] = None
    source_models: list[SourceAssetModel] = Field(default_factory=list)
    target_models: list[TargetInterfaceModel] = Field(default_factory=list)
    mappings: list[dict] = Field(default_factory=list)
    status: str = "draft"
    metadata: dict = Field(default_factory=dict)


class RecommendRequest(BaseModel):
    """AI 推荐请求。"""

    assets: list[AssetDiscovery] = Field(default_factory=list, description="兼容旧版源资产列表")
    targets: list[dict] = Field(default_factory=list, description="兼容旧版目标表列表")
    source_models: Optional[list[SourceAssetModel]] = None
    target_models: Optional[list[TargetInterfaceModel]] = None


class FieldMapping(BaseModel):
    """字段映射项。"""

    source: Optional[str] = Field(None, description="源路径，如 name[0].family；constant 指令时为 null")
    target: str = Field(..., description="目标列名")
    transform: Optional[str] = Field(None, description="受控转换指令（direct/date/constant:<值>/term_map:<skill> 等）")


class MappingItem(BaseModel):
    """转换关系项。

    ⚠ 2026-09-19（P0 修复）：本模型曾是**有损 DTO** —— 缺 `source_id`/`status` 等字段，
    而单管道路径会用 `model_dump()` 的结果 **再存一次** `^demo.Mapping` → 已登记的
    「数据源维度」被抹掉 → `repository.save_mappings` 判定身份不同 → 每次生成**派生重复映射**
    （实测 `M714233` → `_2` → `_3`…；也是历史重复映射的成因）。现补字段 + `extra="allow"`
    （任何附加字段都原样透传，杜绝同类静默丢字段）。
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., description="转换关系 ID，如 M1")
    source: str = Field("", description="兼容旧版的单一源资产名称")
    source_id: Optional[str] = Field(None, description="源数据源 id（身份维度，必须透传不丢）")
    target_id: Optional[str] = Field(None, description="目标 id（身份维度，必须透传不丢）")
    status: Optional[str] = Field(None, description="确认状态（confirmed/draft…）")
    target_table: str = Field("", description="兼容数据库目标的表名")
    target_type: str = Field("DB", description="目标类型（DB/SOAP/FHIR）")
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    source_assets: list[str] = Field(default_factory=list)
    target_model_id: Optional[str] = None
    operation: Optional[str] = None
    request_class: Optional[str] = None
    strategy: dict = Field(default_factory=dict)


class PipelineGenerateRequest(BaseModel):
    """生成数据管道请求。"""

    mappings: list[MappingItem] = Field(..., description="已确认的转换关系列表")
    config: Optional[dict] = Field(None, description="FHIR 数据源配置 {endpoint, username, password}")
    transformation_plan: Optional[TransformationPlan] = None
    source_models: list[SourceAssetModel] = Field(default_factory=list)
    target_models: list[TargetInterfaceModel] = Field(default_factory=list)
    source_id: Optional[str] = None
    target_id: Optional[str] = None
