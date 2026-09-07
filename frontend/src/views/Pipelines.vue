<template>
  <div class="pipelines-page">
    <!-- 状态与操作 -->
    <el-row :gutter="16" class="mb16">
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><span class="card-title">管道状态</span></template>
          <el-tag :type="running ? 'success' : 'danger'" size="large">
            {{ running ? '运行中' : '未运行' }}
          </el-tag>
          <div class="gray mt8">Production: demo.DataflowProduction</div>
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card shadow="never">
          <template #header><span class="card-title">操作</span></template>
          <el-button type="primary" :loading="generating" @click="handleGenerate">
            生成 / 重建数据管道
          </el-button>
          <el-button type="warning" :loading="mockGenerating" @click="handleGenerateMock">
            🎲 生成模拟数据（演示增量）
          </el-button>
          <el-button type="success" :loading="running" @click="handleRun">触发转换</el-button>
          <el-button @click="refreshAll">刷新状态</el-button>
          <el-button type="info" plain :disabled="!aiInfo" @click="aiLogVisible = true">
            🧾 AI 审计日志
          </el-button>
          <div class="gray mt8">FHIRSyncService 定时增量同步（约每 10 秒），FHIRService 逐条处理（每条独立会话）；「生成模拟数据」可模拟第三方产生新数据触发增量转换。</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 管道验证报告（生成后的拓扑/编译/启动/消息流转检查） -->
    <el-card shadow="never" v-if="validation" class="mb16">
      <template #header><span class="card-title">管道验证报告</span></template>
      <el-alert :title="validationTitle" :type="validationType" :closable="false" show-icon />
      <el-table
        v-if="validation.issues && validation.issues.length"
        :data="validation.issues"
        border
        size="small"
        class="mt8"
      >
        <el-table-column label="检查项" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ CHECK_NAMES[row.check] || row.check }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.severity === 'error' ? 'danger' : 'warning'">
              {{ row.severity === 'error' ? '错误' : '警告' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="item" label="对象" width="160" />
        <el-table-column prop="message" label="说明" min-width="240" />
      </el-table>
    </el-card>

    <!-- 消息流转日志 -->
    <el-card shadow="never" class="mb16">
      <template #header><span class="card-title">消息流转日志（Ens.MessageHeader 实时历史）</span></template>
      <el-table :data="logs" v-loading="logsLoading" border stripe max-height="360">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column prop="timestamp" label="时间" min-width="160" />
        <el-table-column prop="source" label="来源组件" min-width="140" />
        <el-table-column label="→" width="50" align="center">
          <template #default><span class="gray">→</span></template>
        </el-table-column>
        <el-table-column prop="target" label="目标组件" min-width="140" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'Completed' ? 'success' : 'danger'">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="duration_ms" label="耗时(ms)" width="90" />
      </el-table>
    </el-card>

    <!-- 落库数据 -->
    <el-card shadow="never">
      <template #header>
        <span class="card-title">目标表落库结果</span>
        <el-select v-model="table" style="width: 240px; float: right" @change="loadTargetData" placeholder="选择要查看的数据表">
          <el-option v-for="t in tableOptions" :key="t.table" :label="t.table" :value="t.table" />
        </el-select>
      </template>
      <el-table :data="targetRows" border stripe max-height="360">
        <el-table-column
          v-for="col in targetColumns"
          :key="col"
          :prop="col"
          :label="col"
          min-width="110"
          show-overflow-tooltip
        />
      </el-table>
      <div class="mt12">共 {{ targetRows.length }} 行</div>
    </el-card>

    <!-- AI 生成审计日志（弹窗） -->
    <el-dialog v-model="aiLogVisible" title="AI 管道生成审计日志" width="680px">
      <template v-if="aiInfo">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="生成时间">
            {{ aiGeneratedAt }}
          </el-descriptions-item>
          <el-descriptions-item label="生成方式">
            <el-tag
              :type="aiInfo.rule_fallback || aiInfo.driven === false ? 'warning' : 'success'"
            >
              {{ aiInfo.rule_fallback || aiInfo.driven === false
                 ? '类型注册表规则兜底（非 AI 决策）' : 'Agent B（大模型）AI 决策' }}
            </el-tag>
            <span v-if="aiInfo.c2_rule_rebuilt" class="ml8 warning-text">
              ⚠ C2 验证-修复中规则曾改写 AI 拓扑（已审计标注）
            </span>
          </el-descriptions-item>
        </el-descriptions>

        <!-- 多管道：逐组展示 -->
        <template v-if="aiInfo.groups && aiInfo.groups.length">
          <div class="mt12 bold">多管道逐组设计（Agent B 对每组独立调用 LLM）：</div>
          <div v-for="(g, i) in aiInfo.groups" :key="i" class="mt8">
            <el-tag type="primary" class="mr6">{{ g.source_type }} → {{ g.target_type }}</el-tag>
            <el-tag
              v-for="(c, j) in g.components"
              :key="j"
              size="small"
              type="success"
              effect="plain"
              class="mr4 mt4"
            >{{ c }}</el-tag>
          </div>
        </template>

        <!-- 单管道：组件链展示 -->
        <template v-else>
          <div class="mt12 bold">Agent B（LLM）决策的组件链：</div>
          <div class="mt8">
            <el-tag
              v-for="(c, j) in (aiInfo.components || [])"
              :key="j"
              size="small"
              type="success"
              effect="plain"
              class="mr4"
            >{{ c }}</el-tag>
          </div>
          <div v-if="aiInfo.supplemented && aiInfo.supplemented.length" class="mt8">
            <el-tag size="small" type="warning">注册表保底补齐（AI 遗漏必需组件）</el-tag>
            <span class="ml4">{{ aiInfo.supplemented.join('、') }}</span>
          </div>
          <div v-else class="mt8 gray">纯 AI 决策，无规则补全</div>
        </template>

        <div class="mt12 gray">
          校验方式：对比 <b>backend 日志中的「数据管道Agent token 用量」</b>；AI 失败会直接报错而非生成。
        </div>
      </template>
      <template v-else>
        <el-empty description="尚无生成记录：请先点击「生成 / 重建数据管道」" />
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { datasourceApi, mappingApi, modelApi, pipelineApi, targetApi } from '../api/dataflow'

const running = ref(false)
const generating = ref(false)
const mockGenerating = ref(false)
const logs = ref([])
const logsLoading = ref(false)
const tableOptions = ref([]) // 可查看数据表（动态：来自已登记的源/目标）
const table = ref('')
const targetRows = ref([])
const targetColumns = ref([])
const validation = ref(null)
const aiInfo = ref(null)          // Agent B 生成响应（ai.driven/components/supplemented/groups…）
const aiLogVisible = ref(false)
const aiGeneratedAt = ref('')

// 检查项名称映射（后端 check 字段 → 中文）
const CHECK_NAMES = {
  pipeline: '拓扑检查',
  compile: '编译检查',
  start: '启动检查',
  smoke: '消息流转',
}

// 验证报告标题（按状态/错误数生成）
const validationTitle = computed(() => {
  const v = validation.value
  if (!v) return ''
  const warns = v.warning_count || 0
  if (!v.ok) {
    return warns
      ? `管道验证发现 ${v.error_count} 个错误、${warns} 个警告`
      : `管道验证发现 ${v.error_count} 个错误`
  }
  return warns ? `管道验证通过（${warns} 个警告）` : '管道验证通过'
})

// 验证报告 alert 类型
const validationType = computed(() => {
  const v = validation.value
  if (!v) return 'success'
  if (!v.ok) return 'error'
  return v.warning_count ? 'warning' : 'success'
})

let timer = null

async function loadViewTables() {
  // 可查看表 = 演示过程已登记的源表/目标表（动态，非写死）
  const data = await pipelineApi.viewTables()
  tableOptions.value = data?.items || []
  if (!table.value && tableOptions.value.length) {
    table.value = tableOptions.value[0].table
  }
}

async function refreshAll() {
  const st = await pipelineApi.status()
  running.value = st?.running === true
  await Promise.all([loadViewTables(), loadLogs()])
  await loadTargetData()
}

async function loadLogs() {
  logsLoading.value = true
  try {
    const data = await pipelineApi.logs(20)
    logs.value = data?.items || []
  } finally {
    logsLoading.value = false
  }
}

async function loadTargetData() {
  if (!table.value) {
    targetRows.value = []
    targetColumns.value = []
    return
  }
  const data = await pipelineApi.targetData(table.value, 50)
  targetRows.value = data?.items || []
  targetColumns.value = targetRows.value.length ? Object.keys(targetRows.value[0]) : []
}

async function buildPipelinesFromMappings(mappings, dsList, targetRows) {
  // 资产名 → 数据源（类型/ID）
  const assetDs = {}
  for (const d of dsList) {
    const assets = (await datasourceApi.assets(d.id))?.items || []
    for (const a of assets) {
      if (a && a.name) assetDs[a.name] = { dsId: d.id, dsType: d.type || '' }
    }
  }
  // 目标表/实体 → 目标记录（含 type）
  const tgtByTable = {}
  for (const t of targetRows || []) {
    if (t && t.table && !tgtByTable[t.table]) tgtByTable[t.table] = t
  }
  // 按 (源数据源, 目标) 分组
  const groupKey = (m) => {
    const ds = assetDs[m.source] || {}
    const tg = tgtByTable[m.target_table]
    return `${ds.dsId || ''}::${tg?.target_id || tg?.id || m.target_table}`
  }
  const byKey = {}
  for (const m of mappings) {
    const k = groupKey(m)
    byKey[k] = byKey[k] || []
    byKey[k].push(m)
  }
  const pipelines = Object.values(byKey).map((ms) => {
    const ds = assetDs[ms[0].source] || {}
    const tg = tgtByTable[ms[0].target_table] || {}
    return {
      source_type: ds.dsType || (ms[0].target_type === 'SOAP' ? 'FHIR' : 'SQL'),
      source_id: ds.dsId,
      target_type: ms[0].target_type || (tg.type === 'SOAP' ? 'SOAP' : 'DB'),
      target_id: tg.target_id || tg.id,
      mappings: ms,
    }
  })
  return pipelines
}

async function handleGenerate() {
  // 使用已确认的转换关系生成管道（含 FHIR 数据源配置）
  const data = await mappingApi.list()
  const mappings = data?.items || []
  if (!mappings.length) {
    ElMessage.warning('请先在「AI 智能匹配」确认转换关系')
    return
  }
  generating.value = true
  try {
    const plans = (await modelApi.plans())?.items || []
    const plan = [...plans].reverse().find((p) => p.status === 'confirmed') || null
    // FHIR 源配置动态取自已登记数据源（endpoint 在记录顶层、账号在 auth，不写死）
    const dsList = (await datasourceApi.list())?.items || []
    const fhirDs = dsList.find((d) => d.type === 'FHIR')
    const config = fhirDs ? {
      endpoint: fhirDs.endpoint || fhirDs.config?.endpoint || fhirDs.config?.base_url || '',
      username: fhirDs.auth?.username || fhirDs.config?.username || 'superuser',
      password: fhirDs.auth?.password || fhirDs.config?.password || 'SYS',
    } : {}
    // 多管道：当已确认 mappings 目标类型/来源混合时（如 SQL→SOAP + FHIR→DB），
    // 自动按 (源数据源, 目标) 分组走 /generate 的 pipelines 参数；单一组合则走单管道。
    const mixedTypes = new Set(mappings.map((m) => m.target_type || ''))
    const mixedSources = new Set(mappings.map((m) => m.source || ''))
    let result
    if ((mixedTypes.size > 1 || mixedSources.size > 1) && dsList.length) {
      const targetRows = (await targetApi.list())?.items || []
      const pipelines = await buildPipelinesFromMappings(mappings, dsList, targetRows)
      if (pipelines.length > 1) {
        result = await pipelineApi.generate({ pipelines })
      }
    }
    if (!result) {
      result = await pipelineApi.generate({
        mappings,
        transformation_plan: plan || undefined,
        source_models: plan?.source_models || [],
        target_models: plan?.target_models || [],
        config,
      })
    }
    validation.value = result?.validation || null
    if (result?.ai) {
      aiInfo.value = result.ai
      aiGeneratedAt.value = new Date().toLocaleString()
    }
    ElMessage.success('数据管道已生成并启动')
    await refreshAll()
  } finally {
    generating.value = false
  }
}

async function handleGenerateMock() {
  mockGenerating.value = true
  try {
    const data = await pipelineApi.generateMock(3)
    ElMessage.success(data?.message || '模拟数据已生成')
    // 等待增量同步 + 逐条转换后自动刷新落库结果
    setTimeout(async () => {
      await Promise.all([loadLogs(), loadTargetData()])
      ElMessage.info('增量同步结果已刷新')
    }, 12000)
  } finally {
    mockGenerating.value = false
  }
}

async function handleRun() {
  const data = await pipelineApi.run()
  ElMessage.success(data?.result || '已触发')
}

onMounted(async () => {
  await refreshAll()
  timer = setInterval(loadLogs, 5000) // 每 5 秒刷新日志
})

onUnmounted(() => timer && clearInterval(timer))
</script>

<style scoped>
.mb16 { margin-bottom: 16px; }
.mt12 { margin-top: 12px; }
.mt8 { margin-top: 8px; }
.gray { color: #909399; }
.card-title { font-weight: 600; }
.mt4 { margin-top: 4px; }
.mr4 { margin-right: 4px; }
.mr6 { margin-right: 6px; }
.ml4 { margin-left: 4px; }
.ml8 { margin-left: 8px; }
.mt12 { margin-top: 12px; }
.mb16 { margin-bottom: 16px; }
.bold { font-weight: 600; }
.warning-text { color: #e6a23c; }
</style>
