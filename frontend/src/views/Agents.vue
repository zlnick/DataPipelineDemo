<template>
  <div class="agents-page">
    <el-card shadow="never">
      <template #header><span class="card-title">{{ t('agents.title') }}</span></template>
      <el-alert :title="t('agents.alert')" type="info" :closable="false" show-icon class="mb16" />

      <!-- ===== Tools 区（确定性工具/校验器：无 LLM）—— 按业界口径归类 =====
           说明：后端 /api/agents 只返回 LLM 能力，故此处按**代码事实**补列（模块名可查），不改后端接口 -->
      <div class="section-title section-title-tool">{{ t('agents.sectionTools') }}</div>
      <el-alert :title="t('agents.toolsNote')" type="info" :closable="false" show-icon class="mb16" />
      <el-table :data="toolItems" size="small" border class="mb16">
        <el-table-column prop="name" :label="t('agents.toolName')" min-width="230" />
        <el-table-column :label="t('agents.taxonomyWhy')" min-width="380">
          <template #default="{ row }">{{ row.why }}</template>
        </el-table-column>
        <el-table-column prop="where" :label="t('agents.toolWhere')" min-width="250" />
      </el-table>

      <!-- ===== Workflows 区（LLM 参与，但路径由代码预定 —— Anthropic 口径） ===== -->
      <div class="section-title section-title-tool">{{ t('agents.sectionWorkflows') }}</div>
      <el-alert :title="t('agents.workflowsNote')" type="info" :closable="false" show-icon class="mb16" />
      <el-table :data="workflowItems" size="small" border class="mb16">
        <el-table-column prop="name" :label="t('agents.toolName')" min-width="210" />
        <el-table-column :label="t('agents.taxonomyWhy')" min-width="420">
          <template #default="{ row }">{{ row.why }}</template>
        </el-table-column>
      </el-table>

      <!-- Skill 区：单轮 LLM 技能（职责单一、按需调用） -->
      <template v-if="skillAgents.length">
        <div class="section-title">{{ t('agents.sectionSkills') }}</div>
        <el-row :gutter="16" class="mb16">
          <el-col v-for="a in skillAgents" :key="a.id" :span="12" class="mb16">
            <el-card shadow="hover" class="agent-card">
              <div class="agent-head">
                <el-icon :size="26" color="#409EFF"><SetUp /></el-icon>
                <span class="agent-name">{{ a.name }}</span>
                <el-tag type="primary" size="small">{{ t('agents.taxoSkill') }}</el-tag>
                <el-tag v-if="a.status === 'ready'" type="success" size="small">{{ t('agents.ready') }}</el-tag>
              </div>
              <p class="taxo-why">{{ t('agents.taxonomyWhy') }}：{{ a.__why }}</p>
              <p class="agent-role">{{ a.role }}</p>
              <p class="agent-purpose">{{ a.purpose }}</p>
              <el-descriptions :column="1" border size="small" class="mt12">
                <el-descriptions-item :label="t('agents.input')">{{ a.input }}</el-descriptions-item>
                <el-descriptions-item :label="t('agents.output')">{{ a.output }}</el-descriptions-item>
                <el-descriptions-item :label="t('agents.trigger')">{{ a.trigger }}</el-descriptions-item>
                <el-descriptions-item :label="t('agents.engine')">{{ a.engine }}</el-descriptions-item>
                <el-descriptions-item :label="t('agents.capabilities')">
                  <el-tag v-for="c in a.capabilities" :key="c" size="small" class="mr6" type="info">{{ c }}</el-tag>
                </el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
        </el-row>
      </template>

      <!-- Agent 区：验证-修复循环（事实工具 + LLM ≤2 轮 + 经验沉淀） -->
      <template v-if="agentAgents.length">
        <div class="section-title section-title-warn">{{ t('agents.sectionAgents') }}</div>
        <el-row :gutter="16">
          <el-col v-for="a in agentAgents" :key="a.id" :span="12" class="mb16">
            <el-card shadow="hover" class="agent-card">
              <div class="agent-head">
                <el-icon :size="26" color="#E6A23C"><SetUp /></el-icon>
                <span class="agent-name">{{ a.name }}</span>
                <el-tag type="warning" size="small">{{ t('agents.taxoAgent') }}</el-tag>
                <el-tag v-if="a.status === 'ready'" type="success" size="small">{{ t('agents.ready') }}</el-tag>
              </div>
              <p class="taxo-why">{{ t('agents.taxonomyWhy') }}：{{ a.__why }}</p>
              <p class="agent-role">{{ a.role }}</p>
              <p class="agent-purpose">{{ a.purpose }}</p>
              <el-descriptions :column="1" border size="small" class="mt12">
                <el-descriptions-item :label="t('agents.input')">{{ a.input }}</el-descriptions-item>
                <el-descriptions-item :label="t('agents.output')">{{ a.output }}</el-descriptions-item>
                <el-descriptions-item :label="t('agents.trigger')">{{ a.trigger }}</el-descriptions-item>
                <el-descriptions-item :label="t('agents.engine')">{{ a.engine }}</el-descriptions-item>
                <el-descriptions-item :label="t('agents.capabilities')">
                  <el-tag v-for="c in a.capabilities" :key="c" size="small" class="mr6" type="info">{{ c }}</el-tag>
                </el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
        </el-row>
      </template>

      <!-- ===== Skill 目录（AI 决策用的受控清单）：管道设计 Skill + 术语判码 Skill ===== -->
      <el-divider />
      <div class="section-title">{{ t('agents.skillCatalog') }}</div>
      <el-alert :title="t('agents.skillCatalogNote')" type="info" :closable="false" show-icon class="mb16" />

      <template v-if="designSkills.length">
        <div class="sub-title">{{ t('agents.skillDesign') }}</div>
        <el-table :data="designSkills" size="small" border class="mb16">
          <el-table-column prop="id" :label="t('agents.skillId')" width="170" />
          <el-table-column :label="t('agents.skillName')" min-width="170">
            <template #default="{ row }">{{ row.name }}</template>
          </el-table-column>
          <el-table-column :label="t('agents.skillApplies')" width="140">
            <template #default="{ row }">
              {{ (row.applies_to || {}).source }} → {{ (row.applies_to || {}).target }}
            </template>
          </el-table-column>
          <el-table-column :label="t('agents.skillStatus')" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'ready' ? 'success' : 'warning'">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('agents.skillTopology')" min-width="220">
            <template #default="{ row }">
              <el-tag v-for="x in row.topology" :key="x" size="small" class="mr6" type="info">{{ x }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="used_count" :label="t('agents.skillUsedCount')" width="90" align="center" />
        </el-table>
      </template>

      <template v-if="termSkills.length">
        <div class="sub-title">{{ t('agents.skillTerm') }}</div>
        <el-table :data="termSkills" size="small" border>
          <el-table-column prop="id" :label="t('agents.skillId')" width="130" />
          <el-table-column :label="t('agents.skillName')" min-width="230">
            <template #default="{ row }">{{ row.name }}</template>
          </el-table-column>
          <el-table-column prop="source_system" :label="t('agents.skillSourceSystem')" min-width="190" />
          <el-table-column prop="target_system" :label="t('agents.skillTargetSystem')" min-width="230" />
          <el-table-column prop="agent" :label="t('agents.skillJudgeAgent')" min-width="170" />
          <el-table-column prop="used_count" :label="t('agents.skillUsedCount')" width="90" align="center" />
        </el-table>
      </template>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { agentApi } from '../api/dataflow'

const { t, locale } = useI18n()
const agents = ref([])
// Skill 目录（AI 决策的受控清单）：管道设计 Skill（Agent B 选用）+ 术语判码 Skill（A/C1 注入）
const designSkills = ref([])
const termSkills = ref([])
// 英文页：Skill 目录名称 / 详情 / 拓扑 / 判定 Agent 的英文覆盖（后端目录为中文，仅界面翻译层处理）
// design: [name, role, topologyEn[]] ｜ term: [name, '', '', agentEn]
const EN_SKILLS = {
  'sql2fhir-patient-tx': ['SQL→FHIR patient transaction Skill',
                          'Poll patient main table → aggregate child tables → FHIR transaction Bundle',
                          ['Source BS (patient main table)', 'SQL query BOs (child tables)',
                           'Aggregation BP', 'FHIR HTTP operation']],
  'sql2db': ['SQL→DB direct write Skill', 'Poll per source table → idempotent UPSERT into target tables',
             ['Source BS ×N (one per table)', 'Transform BP', 'Write BO ×N (UPSERT)']],
  'fhir2db': ['FHIR→DB Skill', 'Incremental FHIR fetch → row message → UPSERT into DB tables',
              ['FHIR sync service', 'Per-item service', 'Transform BP', 'Write BO (UPSERT)']],
  'sql2soap': ['SQL→SOAP Skill', 'Poll SQL rows → pack write-type SOAP request → SOAP operation',
               ['Source BS ×N', 'Transform BP', 'SOAP operation']],
  'fhir2soap': ['FHIR→SOAP Skill', 'Incremental FHIR fetch → SOAP request → SOAP operation',
                ['FHIR sync service', 'Per-item service', 'Transform BP', 'SOAP operation']],
  'fhir2fhir': ['FHIR→FHIR copy Skill', 'Incremental fetch from one FHIR repo → REST PUT into another repo',
                ['FHIR sync service', 'Per-item service', 'Transform BP', 'FHIR HTTP operation']],
  cn2snomed: ['Chinese diagnosis (ICD-10 GB/T) → SNOMED CT', '', '', 'C3-Dx (Diagnosis Mapping Agent)'],
  cn2rx: ['Chinese drug (NRDL) → RxNorm', '', '', 'C3 (Drug Mapping Agent)'],
}

// 英文页：Agent/Skill 能力卡元数据由前端词典覆盖（后端返回中文，仅界面翻译层处理）
const EN_AGENTS = {
  'interface-analyzer-agent': {
    name: 'Interface Analyzer',
    role: 'Source/Target interface semantic analyst',
    purpose: 'Builds AI conclusions on top of deterministic facts (CapabilityStatement / columns / WSDL entities / runtime contract): asset semantics & polling-key hints, target direction (write/read), and a natural-language reading of the runtime contract. Written back to assets/targets and runtime.note.ai as context for Agent A/B.',
    input: 'Probed facts (capability / columns / WSDL / runtime contract, credentials masked)',
    output: 'ai_semantics / key_hint / direction / ai_reason + runtime.note.ai',
    trigger: 'Auto on datasource profile analysis / SQL table select / SOAP import / DB table select',
    capabilities: ['Asset semantics', 'Polling-key hints', 'Direction (write/read)', 'Contract interpretation'],
    engine: 'Single-shot LLM prompt (interface_analyzer); explicit error on failure',
  },
  'transformation-agent': {
    name: 'Transformation Generation',
    role: 'Healthcare data integration expert',
    purpose: 'Generates asset-to-target matching and field-level mappings based on source field semantics and target columns/write semantics.',
    input: 'Source assets + target structures + runtime contracts (masked)',
    output: 'recommendations (asset → target + field mappings)',
    trigger: 'AI Matching',
    capabilities: ['Field semantics', 'FHIRPath', 'concat() expr', 'table.column prefix', 'date transform'],
    engine: 'Single-shot LLM prompt',
  },
  'transformation-validate-agent': {
    name: 'Transformation Validation Agent',
    role: 'Transformation validation expert',
    purpose: 'Validates and fixes field mappings (target column existence, source paths, semantic mismatches) before pipeline generation (per group in multi-pipeline).',
    input: 'Agent A / user mappings + asset structures',
    output: 'Fixed mappings + validation report',
    trigger: 'After AI Matching, before pipeline generation',
    capabilities: ['Target column check', 'Source path check', 'Mismatch judgment', 'Mapping fix'],
    engine: 'Fact-check tools + LLM decisions (L1 rules → L2 LLM ≤2 rounds)',
  },
  'pipeline-agent': {
    name: 'Pipeline Design',
    role: 'IRIS interoperability architect',
    purpose: 'Generates the pipeline component topology from confirmed mappings and source/target runtime contracts. AI decides the component set & order (incl. multiple target tables / multiple pipelines); the registry only fills className/settings and backfills essentials (marked ai_supplemented).',
    input: 'Confirmed mappings + runtime contracts (masked) + component enumeration (+ interface semantics)',
    output: 'pipeline topology (AI-decided composition, registry parameterized)',
    trigger: 'Generate pipeline',
    capabilities: ['Component composition', 'Heterogeneous combos', 'Multi-pipeline', 'Source routing', 'AI-auditable'],
    engine: 'LLM topology generation (recommend_pipeline) + registry parameterization',
  },
  'pipeline-validate-agent': {
    name: 'Pipeline Validation Agent',
    role: 'IRIS pipeline validation & fix expert',
    purpose: 'Validates and fixes pipeline generation: topology/compile/start/message flow (incl. mixed multi-pipeline types); auto rebuild once on failure and stores experience into ^demo.ValidationIssue.',
    input: 'Generation result + topology + source/target types + past issues',
    output: 'Report + fix actions + experience',
    trigger: 'Generation failed or validation failed (single/multi unified)',
    capabilities: ['Topology check', 'Compile check', 'Start check', 'Smoke test', 'Layered fix', 'Experience store'],
    engine: 'Fact-check tools + LLM decisions (L1 → L2 LLM ≤2 rounds → L3 default)',
  },
  'knowledge-polish-agent': {
    name: 'Knowledge Polish',
    role: 'Healthcare IT knowledge-base editor',
    purpose: 'Reviews, deduplicates and restructures the validation experience stored in ^demo.ValidationIssue into structured knowledge (title/problem/solution/prevention), written to the Obsidian vault for human retrieval.',
    input: 'Recent validation issues (may include duplicates of the same failure)',
    output: 'Deduped, restructured knowledge items',
    trigger: 'export_validation_issues.py before writing knowledge/04-Pitfalls',
    capabilities: ['Semantic dedup', 'Restructure', 'Obsidian export'],
    engine: 'Single-shot LLM prompt (llm_client.polish_validation_issues); explicit error on failure',
  },
}

// ===== 归类口径（依据 Anthropic《Building effective agents》，2024-12-19）=====
//   Workflow = "LLMs and tools are orchestrated through predefined code paths"
//   Agent    = "LLMs dynamically direct their own processes and tool usage"
//   派生判据：Tool = 确定性无 LLM；Skill = 打包的指令/知识（单步）；Agent = 工具 + 多轮自主循环 + 目标
// [归类, 中文依据, 英文依据]
const TAXONOMY = {
  'interface-analyzer-agent': ['workflow',
    '工具（事实采集：CapabilityStatement / 列结构 / WSDL 实体 / 契约探查）+ 单轮 LLM 归纳，路径由代码预定',
    'Tools (fact gathering) + one LLM pass along a predefined path'],
  'transformation-agent': ['skill',
    '单轮 LLM + 上下文（提示词包），无工具循环 → Skill',
    'Single-shot LLM with in-context knowledge (prompt pack); no tool loop'],
  'knowledge-polish-agent': ['skill',
    '单轮 LLM 研读 / 去重 / 润色（+ 写文件），无循环 → Skill',
    'Single-shot LLM polish/dedup (plus file write); no loop'],
  'mapping-agent': ['skill',
    '单轮 LLM 判定 + 术语服务检索（Tool）；查不到即判 negative → Skill + Tool',
    'Single-shot LLM judgement + terminology lookup tool'],
  'mapping-agent-dx': ['skill',
    '同上：单轮 LLM 判定 + 术语服务检索（Tool）→ Skill + Tool',
    'Same: single-shot LLM judgement + terminology lookup tool'],
  'pipeline-agent': ['workflow',
    '决策一次（选 Skill + 产出拓扑），随后由平台代码路径渲染落地 → Workflow（Planner）',
    'Decides once (picks a Skill, emits topology); platform renders it along a fixed code path'],
  'transformation-validate-agent': ['agent',
    '事实检查工具 + LLM ≤2 轮修复循环 + 经验沉淀 → 工具 + 循环 + 目标 = Agent',
    'Fact-check tools + LLM fix loop (<=2 rounds) + experience learning = Agent'],
  'pipeline-validate-agent': ['agent',
    '同上：工具集 + 多轮循环 + 目标 → Agent',
    'Same: toolset + multi-round loop + goal = Agent'],
}

const TAXO_OF = (a) => (TAXONOMY[a && a.id] ? TAXONOMY[a.id][0] : 'skill')

// 确定性工具（无 LLM）—— 后端 /api/agents 只返回 LLM 能力，故此处按**代码事实**补列（模块名可查）
// [中文名, 位置, 中文依据, 英文依据, 英文名]
const TOOLS_STATIC = [
  ['connection-profiler（连接探查）', 'backend/services/connection_profiler.py',
   'FHIR metadata / SQL 增量键 / SOAP 操作语义的确定性探测，无 LLM（后端注册表有条目，但不随 /api/agents 返回）',
   'Deterministic probing (FHIR metadata / SQL increment key / SOAP operation semantics); no LLM',
   'Connection profiler'],
  ['connection-gate（连通门禁）', 'pipeline_validator.check_connection',
   '生成前源/目标可达性与参数完整性检查，不通过即拦截 → Tool / Guardrail',
   'Pre-generation reachability & parameter gate; blocks on failure',
   'Connectivity gate'],
  ['pipeline_validator.check_*（事实检查）', 'backend/services/pipeline_validator.py',
   '拓扑 / 编译 / 启动 / 消息 / 目标落地 / 子表派发等事实检查 —— C2 Agent 使用的工具集',
   'Fact checks (topology/compile/start/messages/target effect/dispatch) used by the C2 Agent',
   'Fact checks (pipeline_validator.check_*)'],
  ['transformation_validator（规则检查）', 'backend/services/transformation_validator.py',
   '列级 / 值级 / 结构约束规则检查（term_gap、coded_text_gap 等）→ Tool',
   'Column/value/structural rule checks (term_gap, coded_text_gap, ...)',
   'Rule checks (transformation_validator)'],
  ['generated_bp 静态准入', 'backend/services/generated_bp.py',
   'AI 生成 BP 的静态准入（响应类型、未定义方法、布局键）→ Tool / Guardrail',
   'Static admission for AI-generated BPs (response type, undefined helpers, layout keys)',
   'BP static admission (generated_bp)'],
  ['term_precheck（术语缺口盘点）', 'backend/services/term_precheck.py',
   '按源表编码值盘点术语覆盖（covered / negative / missing）→ Tool',
   'Counts terminology coverage from source codes (covered/negative/missing)',
   'Terminology gap precheck (term_precheck)'],
  ['db_target_columns / db_target_keys', 'backend/services/db_target_columns.py / db_target_keys.py',
   '目标库列结构与主键的事实来源（Agent 上下文 + 校验输入）→ Tools',
   'Target-schema column & primary-key facts (context + validation input)',
   'Target columns & keys (db_target_*)'],
  ['wsdl_importer（WSDL 实体分析）', 'backend/services/wsdl_importer.py',
   'WSDL 导入生成 BO + 实体/字段结构提取 → Tool',
   'WSDL import generates BO classes + entity/field structure',
   'WSDL entity analysis (wsdl_importer)'],
  ['demo.TerminologyOperation（术语检索 BO）', 'iris/src/demo/TerminologyOperation.cls',
   '运行期向术语服务器实时判码（lookup）→ Tool（被管道 BP 调用）',
   'Runtime terminology lookup against the terminology server (called by BPs)',
   'Terminology lookup BO (demo.TerminologyOperation)'],
  ['type_registry / fhir_target_model', 'backend/services/type_registry.py / fhir_target_model.py',
   '组件类型与 FHIR 资源模型的注册表（参数化事实来源）→ Tool / Registry',
   'Registry of component types and FHIR resource models (parameterization facts)',
   'Type registry & FHIR model'],
]
const toolItems = computed(() => TOOLS_STATIC.map((t) => ({
  name: locale.value === 'en' && t[4] ? t[4] : t[0],
  where: t[1], why: locale.value === 'en' ? t[3] : t[2],
})))

// 按 kind 分组：Skill（单轮 LLM 技能）区 与 Agent（工具+修复循环）区
const skillAgents = computed(() => agents.value.filter((a) => TAXO_OF(a) === 'skill'))
const agentAgents = computed(() => agents.value.filter((a) => TAXO_OF(a) === 'agent'))
// Workflows 区：LLM 参与、但路径由代码预定（Anthropic 口径）
const workflowItems = computed(() => agents.value
  .filter((a) => TAXO_OF(a) === 'workflow')
  .map((a) => ({ name: a.name, why: a.__why || '' })))

async function loadAgents() {
  const data = await agentApi.list()
  let items = data?.items || []
  if (locale.value === 'en') {
    items = items.map((a) => (EN_AGENTS[a.id] ? { ...a, ...EN_AGENTS[a.id] } : a))
  }
  // 归一化：挂上"业界归类依据"（Tool / Skill / Workflow / Agent）
  agents.value = items.map((a) => ({
    ...a,
    __taxo: TAXO_OF(a),
    __why: TAXONOMY[a.id] ? (locale.value === 'en' ? TAXONOMY[a.id][2] : TAXONOMY[a.id][1]) : '',
  }))
}

async function loadSkills() {
  try {
    const data = await agentApi.skills()
    const d = data?.design || []
    const m = data?.term || []
    if (locale.value === 'en') {
      designSkills.value = d.map((s) => {
        const e = EN_SKILLS[s.id]
        return e ? { ...s, name: e[0] || s.name, role: e[1] || s.role, topology: e[2] || s.topology } : s
      })
      termSkills.value = m.map((s) => {
        const e = EN_SKILLS[s.id]
        return e ? { ...s, name: e[0] || s.name, agent: e[3] || s.agent } : s
      })
    } else {
      designSkills.value = d
      termSkills.value = m
    }
  } catch (e) {
    // Skill 目录拉取失败不影响 Agents 列表展示（显式留空，不伪造）
    designSkills.value = []
    termSkills.value = []
  }
}

onMounted(() => { loadAgents(); loadSkills() })
</script>

<style scoped>
.mb16 { margin-bottom: 16px; }
.mr6 { margin-right: 6px; }
.mt12 { margin-top: 12px; }
.card-title { font-weight: 600; }
.section-title {
  font-weight: 600;
  font-size: 15px;
  margin: 4px 0 12px;
  padding-left: 10px;
  border-left: 3px solid #409EFF;
  color: #303133;
}
.section-title-warn { border-left-color: #E6A23C; }
.sub-title { font-size: 13px; font-weight: 600; color: #606266; margin: 4px 0 8px; }
.section-title-tool { border-left-color: #909399; }
.taxo-why { font-size: 12px; color: #909399; margin: 6px 0 0; line-height: 1.5; }
.section-title + .el-row .el-col:last-child { margin-bottom: 0; }
.agent-card { height: 100%; }
.agent-head { display: flex; align-items: center; gap: 8px; }
.agent-name { font-size: 17px; font-weight: 600; }
.agent-role { color: #409EFF; margin: 8px 0 4px; }
.agent-purpose { color: #606266; margin: 0 0 8px; }
</style>
