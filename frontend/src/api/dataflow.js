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
  schemas: (id) => http.get(`/targets/${id}/schemas`),
  tables: (id, schema) => http.get(`/targets/${id}/tables`, { params: { schema } }),
  selectTables: (id, tables) => http.post(`/targets/${id}/tables`, { tables }),
  selected: (id) => http.get(`/targets/${id}/selected`),
  data: (table, limit = 50) => http.get(`/targets/${table}/data`, { params: { limit } }),
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
}

// 已封装 AI Agent 目录
export const agentApi = {
  list: () => http.get('/agents'),
}

// 数据管道
export const pipelineApi = {
  status: () => http.get('/pipelines/status'),
  generate: (data) => http.post('/pipelines/generate', data),
  run: () => http.post('/pipelines/run'),
  logs: (count = 50) => http.get('/pipelines/logs', { params: { count } }),
  mappings: () => http.get('/pipelines/mappings'),
  targetData: (table = 'Patient', limit = 50) =>
    http.get('/pipelines/target-data', { params: { table, limit } }),
  generateMock: (count = 3) => http.post('/pipelines/generate-mock', null, { params: { count } }),
}
