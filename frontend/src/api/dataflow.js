// 数据流 API 封装（数据源 / 目标 / AI / 转换关系 / 管道 / Agents）
import http from './index'

// 数据源管理（FHIR 分析 / SQL 选表发现）
export const datasourceApi = {
  list: () => http.get('/datasources'),
  create: (data) => http.post('/datasources', data),
  analyze: (id) => http.post(`/datasources/${id}/analyze`),
  assets: (id) => http.get(`/datasources/${id}/assets`),
  // SQL 源：JDBC 连通 + schema/表发现 + 选表
  test: (id) => http.post(`/datasources/${id}/test`),
  schemas: (id) => http.get(`/datasources/${id}/schemas`),
  tables: (id, schema) => http.get(`/datasources/${id}/tables`, { params: { schema } }),
  selectTables: (id, tables) => http.post(`/datasources/${id}/tables`, { tables }),
  seed: (id, data) => http.post(`/datasources/${id}/seed`, data),
}

// 独立领域模型（源资产模型 / 目标接口模型 / 转换计划）
export const modelApi = {
  sourceAssets: (sourceId) => http.get('/source-assets', { params: sourceId ? { source_id: sourceId } : {} }),
  targetInterfaces: () => http.get('/target-interfaces'),
  plans: () => http.get('/transformation-plans'),
  createPlan: (plan) => http.post('/transformation-plans', plan),
  verifyPlan: (id) => http.post(`/transformation-plans/${id}/verify`),
}

// 转换目标（数据目标 JDBC 分步发现 / SOAP WSDL 导入）
export const targetApi = {
  list: () => http.get('/targets'),
  manage: () => http.get('/targets/manage'),
  create: (data) => http.post('/targets', data),
  test: (id) => http.post(`/targets/${id}/test`),
  import: (id) => http.post(`/targets/${id}/import`),
  refreshResources: (id) => http.post(`/targets/${id}/refresh-resources`),
  schemas: (id) => http.get(`/targets/${id}/schemas`),
  tables: (id, schema) => http.get(`/targets/${id}/tables`, { params: { schema } }),
  selectTables: (id, tables) => http.post(`/targets/${id}/tables`, { tables }),
  selected: (id) => http.get(`/targets/${id}/selected`),
  data: (table, limit = 50) => http.get(`/targets/${table}/data`, { params: { limit } }),
  // FHIR 已建模资源的结构约束（choice/数组/引用目标，供映射与展示共用）
  fhirConstraints: (resource) => http.get('/targets/fhir-constraints',
    { params: resource ? { resource } : {} }),
}

// AI 智能推荐
export const aiApi = {
  recommend: (data) => http.post('/ai/recommend', data),
  verify: (plan) => http.post('/ai/verify', { transformation_plan: plan }),
}

// 转换关系
export const mappingApi = {
  list: () => http.get('/mappings'),
  save: (mappings) => http.post('/mappings', { mappings }),
  // 术语映射（术语服务器 = 唯一事实源）：只读目录+预检摘要；迁移为本地物化缓存（运行期零网络）
  termCatalog: () => http.get('/mapping/term-catalog'),
}

// 已封装 AI Agent 目录
export const agentApi = {
  list: () => http.get('/agents'),
  // Skill 目录：管道设计 Skill（Agent B 选用）+ 术语判码 Skill（A/C1 受控指令），含实际使用次数
  skills: () => http.get('/agents/skills'),
}

// 数据管道
export const pipelineApi = {
  status: () => http.get('/pipelines/status'),
  generate: (data) => http.post('/pipelines/generate', data),
  run: () => http.post('/pipelines/run'),
  logs: (count = 50) => http.get('/pipelines/logs', { params: { count } }),
  mappings: () => http.get('/pipelines/mappings'),
  viewTables: () => http.get('/pipelines/view-tables'),
  targetData: (table, limit = 50) =>
    http.get('/pipelines/target-data', { params: { table, limit } }),
  generateMock: (count = 3) => http.post('/pipelines/generate-mock', null, { params: { count } }),
  // 组件清单与许可容量（社区版每业务主机占 1 个许可单元；生成前自动做许可预算）
  items: () => http.get('/pipelines/items'),
  toggleItem: (data) => http.post('/pipelines/items/toggle', data),
  // 数据管道实体（受管理持久对象）：按 (源,目标,设计 Skill) 判定身份，重复生成只更新不新增
  instances: () => http.get('/pipelines/instances'),
  instance: (id) => http.get(`/pipelines/instances/${id}`),
  enableInstance: (id) => http.post(`/pipelines/instances/${id}/enable`),
  disableInstance: (id) => http.post(`/pipelines/instances/${id}/disable`),
  deleteInstance: (id) => http.delete(`/pipelines/instances/${id}`),
  syncInstances: () => http.post('/pipelines/instances/sync'),
}
