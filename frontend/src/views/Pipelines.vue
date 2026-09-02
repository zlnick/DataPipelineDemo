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
        <el-select v-model="table" style="width: 180px; float: right" @change="loadTargetData">
          <el-option label="Patient" value="Patient" />
          <el-option label="Observation" value="Observation" />
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
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { mappingApi, pipelineApi } from '../api/dataflow'

const running = ref(false)
const generating = ref(false)
const mockGenerating = ref(false)
const logs = ref([])
const logsLoading = ref(false)
const table = ref('Patient')
const targetRows = ref([])
const targetColumns = ref([])
const validation = ref(null)

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

async function refreshAll() {
  const st = await pipelineApi.status()
  running.value = st?.running === true
  await Promise.all([loadLogs(), loadTargetData()])
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
  const data = await pipelineApi.targetData(table.value, 50)
  targetRows.value = data?.items || []
  targetColumns.value = targetRows.value.length ? Object.keys(targetRows.value[0]) : []
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
    const result = await pipelineApi.generate({
      mappings,
      config: {
        endpoint: 'http://127.0.0.1:52773/csp/healthshare/fhirserver/fhir/r4/',
        username: 'superuser',
        password: 'SYS',
      },
    })
    validation.value = result?.validation || null
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
</style>
