// =====================================================================
// 类型枚举常量（预留其他数据源 / 数据目标的界面元素）
// enabled: 是否已实现。未实现的类型在前端渲染为禁用态 + 「预留」提示，
//          后续启用时只需将对应 enabled 置为 true 并补充实现。
// =====================================================================

// 数据源类型：FHIR / SQL 已实现，其余（REST / SOAP）预留
// labelEn / descEn：英文页（/en）显示用（前端常量，不入后端接口）
export const DATA_SOURCE_TYPES = [
  { value: 'FHIR', label: 'FHIR 接口', labelEn: 'FHIR endpoint', enabled: true,
    desc: 'HL7 FHIR R4 标准接口（增量同步抓取）',
    descEn: 'HL7 FHIR R4 endpoint (incremental sync)' },
  { value: 'SQL', label: 'SQL 数据库', labelEn: 'SQL database', enabled: true,
    desc: 'SQL 轮询查询（DSN + Query，逐行入队）',
    descEn: 'SQL polling (DSN + Query, row-by-row enqueue)' },
  { value: 'REST', label: 'REST 接口', labelEn: 'REST API', enabled: false,
    desc: 'HTTP REST API（预留）', descEn: 'HTTP REST API (planned)' },
  { value: 'SOAP', label: 'SOAP 接口', labelEn: 'SOAP service', enabled: false,
    desc: 'Web Service SOAP（预留）', descEn: 'Web Service SOAP (planned)' },
]

// 转换目标类型：DB / SOAP / FHIR 已实现，其余（REST）预留
export const TARGET_TYPES = [
  { value: 'DB', label: '数据库', labelEn: 'Database', enabled: true,
    desc: '远端数据库表（JDBC，示例：模拟远端库）',
    descEn: 'Remote DB tables via JDBC (demo: mock remote DB)' },
  { value: 'SOAP', label: 'SOAP 接口', labelEn: 'SOAP service', enabled: true,
    desc: 'Web Service SOAP（WSDL 导入生成 BO）',
    descEn: 'Web Service SOAP (WSDL import generates the BO)' },
  { value: 'FHIR', label: 'FHIR 接口', labelEn: 'FHIR repository', enabled: true,
    desc: 'FHIR 存储库（US Core 声明式写入，内置 FHIR BO）',
    descEn: 'FHIR repository (US Core declarative write)' },
  { value: 'REST', label: 'REST 接口', labelEn: 'REST API', enabled: false,
    desc: 'HTTP REST 投放（预留）', descEn: 'HTTP REST delivery (planned)' },
]

// 映射状态
export const MAPPING_STATUS = {
  DRAFT: 'draft',        // 待确认
  CONFIRMED: 'confirmed', // 已确认
}

// 管道状态
export const PIPELINE_STATUS = {
  CREATED: 'created',    // 已生成
  RUNNING: 'running',    // 运行中
  ERROR: 'error',        // 异常
}
