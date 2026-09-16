<template>
  <div class="pipelines-page">
    <!-- 状态与操作 -->
    <el-row :gutter="16" class="mb16">
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><span class="card-title">{{ t('pipelines.statusTitle') }}</span></template>
          <el-tag :type="running ? 'success' : 'danger'" size="large">
            {{ running ? t('pipelines.running') : t('pipelines.stopped') }}
          </el-tag>
          <div class="gray mt8">Production: demo.DataflowProduction</div>
        </el-card>
      </el-col>
      <el-col :span="16">
        <el-card shadow="never">
          <template #header><span class="card-title">{{ t('pipelines.actions') }}</span></template>
          <el-button type="primary" :loading="generating" @click="handleGenerate">
            {{ t('pipelines.generateBtn') }}
          </el-button>
          <el-button type="warning" :loading="mockGenerating" @click="handleGenerateMock">
            🎲 {{ t('pipelines.generateMock') }}
          </el-button>
          <el-button type="success" :loading="running" @click="handleRun">{{ t('pipelines.runBtn') }}</el-button>
          <el-button @click="refreshAll">{{ t('pipelines.refreshBtn') }}</el-button>
          <el-button type="info" plain :disabled="!aiInfo" @click="aiLogVisible = true">
            {{ t('pipelines.aiAuditBtn') }}
          </el-button>
          <div class="gray mt8">{{ t('pipelines.syncNote') }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 数据管道（受管理实体）：管道 = 源 + 目标 + 设计 Skill；按管道类别折叠分组，可整条启停 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <span class="card-title">{{ t('pipelines.instancesTitle') }}</span>
        <el-tag class="ml8" size="small" type="info">
          {{ t('pipelines.instanceCount') }}: {{ instances.length }}
        </el-tag>
        <el-tag v-if="activeInstanceCount" class="ml4" size="small" type="success">
          {{ t('pipelines.instanceActive') }}: {{ activeInstanceCount }}
        </el-tag>
        <el-button size="small" style="float: right" @click="loadInstances">
          {{ t('pipelines.refreshBtn') }}
        </el-button>
        <el-button
          size="small"
          type="primary"
          plain
          class="mr6"
          style="float: right"
          :loading="syncingInstances"
          @click="handleSyncInstances"
        >
          {{ t('pipelines.instanceSync') }}
        </el-button>
      </template>
      <div class="gray">{{ t('pipelines.instancesNote') }}</div>
      <el-collapse v-if="instances.length" v-model="openGroups" class="mt8">
        <el-collapse-item v-for="g in instanceGroups" :key="g.category" :name="g.category">
          <template #title>
            <el-tag size="small" :type="g.active ? 'success' : 'info'" class="mr6">
              {{ g.category }}
            </el-tag>
            <el-tag v-if="g.active" size="small" type="success" effect="plain" class="mr6">
              {{ t('pipelines.groupActiveTag') }}
            </el-tag>
            <span class="gray">
              {{ g.items.length }} × {{ t('pipelines.pipelineTag') }}
              ｜ {{ g.enabled_count }}/{{ g.component_count }} {{ t('pipelines.licenseUsed') }}
            </span>
          </template>
          <el-table :data="g.items" border size="small">
            <el-table-column prop="id" :label="t('pipelines.colPipeId')" min-width="190" />
            <el-table-column :label="t('pipelines.colPipeSource')" width="150">
              <template #default="{ row }">
                {{ row.source_type }} <span class="gray">{{ row.source_id || '' }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('pipelines.colPipeTarget')" width="150">
              <template #default="{ row }">
                {{ row.target_type }} <span class="gray">{{ row.target_id || '' }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('pipelines.colPipeSkill')" min-width="180">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ row.design_skill }}</el-tag>
                <el-tag v-if="row.recovered" size="small" type="warning" effect="plain" class="ml4">
                  {{ t('pipelines.instanceRecovered') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('pipelines.colPipeComponents')" min-width="260">
              <template #default="{ row }">
                <el-tag
                  v-for="(c, i) in (row.component_names || [])"
                  :key="i"
                  size="small"
                  effect="plain"
                  class="mr4 mt4"
                >{{ c }}</el-tag>
                <div v-if="(row.shared_component_names || []).length" class="gray mt4">
                  {{ t('pipelines.categoryShared') }}: {{ row.shared_component_names.join(', ') }}
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('pipelines.colPipeStatus')" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTagType(row.status)">
                  {{ statusText(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('pipelines.colPipeGen')" width="90" align="center">
              <template #default="{ row }">{{ row.generation_count || 0 }}</template>
            </el-table-column>
            <el-table-column :label="t('pipelines.colPipeUpdated')" min-width="160">
              <template #default="{ row }">
                {{ (row.updated_at || '').replace('T', ' ').slice(0, 19) }}
              </template>
            </el-table-column>
            <el-table-column :label="t('pipelines.actions')" width="180" fixed="right">
              <template #default="{ row }">
                <!-- 已被取代的管道（组件被其它管道接管/移除）不能原地启停，只能删除记录 -->
                <el-button
                  v-if="row.status === 'superseded'"
                  size="small"
                  type="danger"
                  link
                  @click="handleDeleteInstance(row)"
                >{{ t('pipelines.instanceDelete') }}</el-button>
                <template v-else>
                  <el-button
                    v-if="row.status !== 'active'"
                    size="small"
                    type="success"
                    link
                    @click="handleToggleInstance(row, true)"
                  >{{ t('pipelines.instanceEnable') }}</el-button>
                  <el-button
                    v-else
                    size="small"
                    type="warning"
                    link
                    @click="handleToggleInstance(row, false)"
                  >{{ t('pipelines.instanceDisable') }}</el-button>
                  <el-button
                    size="small"
                    type="danger"
                    link
                    @click="handleDeleteInstance(row)"
                  >{{ t('pipelines.instanceDelete') }}</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
      <el-empty v-else :description="t('pipelines.instancesEmpty')" />
    </el-card>

    <!-- 许可与组件（许可预算）：IRIS 社区版每业务主机常驻占 1 个许可单元 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <span class="card-title">{{ t('pipelines.licenseTitle') }}</span>
        <el-tag class="ml8" size="small" type="info">
          {{ t('pipelines.licenseUnits') }}: {{ licenseUnits }}
        </el-tag>
        <el-tag
          class="ml4"
          size="small"
          :type="enabledCount + 1 > licenseUnits ? 'danger' : 'success'"
        >
          {{ t('pipelines.licenseUsed') }}: {{ enabledCount }}
        </el-tag>
        <el-button size="small" style="float: right" @click="loadItems">
          {{ t('pipelines.refreshBtn') }}
        </el-button>
      </template>
      <div class="gray">{{ t('pipelines.licenseNote') }}</div>
      <el-alert
        v-if="budgetTitle"
        class="mt8"
        :type="budget?.over_capacity ? 'warning' : 'success'"
        :title="budgetTitle"
        :closable="false"
        show-icon
      />
      <el-table :data="prodItems" border size="small" max-height="280" class="mt8">
        <el-table-column prop="name" :label="t('pipelines.colItemName')" min-width="200" />
        <el-table-column prop="className" :label="t('pipelines.colItemClass')" min-width="260" />
        <el-table-column prop="category" :label="t('pipelines.colItemCategory')" width="170" />
        <el-table-column :label="t('pipelines.colItemEnabled')" width="130">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled === 1"
              @change="(v) => handleToggleItem(row, v)"
            />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 管道验证报告（生成后的拓扑/编译/启动/消息流转检查） -->
    <el-card shadow="never" v-if="validation" class="mb16">
      <template #header><span class="card-title">{{ t('pipelines.valReport') }}</span></template>
      <el-alert :title="validationTitle" :type="validationType" :closable="false" show-icon />
      <el-table
        v-if="validation.issues && validation.issues.length"
        :data="validation.issues"
        border
        size="small"
        class="mt8"
      >
        <el-table-column :label="t('pipelines.colCheck')" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ checkLabel(row.check) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('pipelines.colLevel')" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.severity === 'error' ? 'danger' : 'warning'">
              {{ row.severity === 'error' ? t('pipelines.sevError') : t('pipelines.sevWarning') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="item" :label="t('pipelines.colItem')" width="160" />
        <el-table-column prop="message" :label="t('pipelines.colMessage')" min-width="240" />
      </el-table>
    </el-card>

    <!-- 消息流转日志 -->
    <el-card shadow="never" class="mb16">
      <template #header><span class="card-title">{{ t('pipelines.logNote') }}</span></template>
      <el-table :data="logs" v-loading="logsLoading" border stripe max-height="360">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column prop="timestamp" :label="t('pipelines.colTime')" min-width="160" />
        <el-table-column prop="source" :label="t('pipelines.colSource')" min-width="140" />
        <el-table-column label="→" width="50" align="center">
          <template #default><span class="gray">→</span></template>
        </el-table-column>
        <el-table-column prop="target" :label="t('pipelines.colTarget')" min-width="140" />
        <el-table-column prop="status" :label="t('pipelines.colStatus')" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'Completed' ? 'success' : 'danger'">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="duration_ms" :label="t('pipelines.colMs')" width="90" />
      </el-table>
    </el-card>

    <!-- 落库数据 -->
    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('pipelines.targetData') }}</span>
        <el-select v-model="table" style="width: 240px; float: right" @change="loadTargetData" :placeholder="t('pipelines.pickTable')">
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
      <div class="mt12">{{ t('pipelines.rows', { n: targetRows.length }) }}</div>
    </el-card>

    <!-- AI 生成审计日志（弹窗） -->
    <el-dialog v-model="aiLogVisible" :title="t('pipelines.aiLogTitle')" width="680px">
      <template v-if="aiInfo">
        <el-descriptions :column="1" border>
          <el-descriptions-item :label="t('pipelines.aiGenTime')">
            {{ aiGeneratedAt }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('pipelines.aiGenMode')">
            <el-tag
              :type="aiInfo.rule_fallback || aiInfo.driven === false ? 'warning' : 'success'"
            >
              {{ aiInfo.rule_fallback || aiInfo.driven === false
                 ? t('pipelines.aiRuleFallback') : t('pipelines.aiDecision') }}
            </el-tag>
            <span v-if="aiInfo.c2_rule_rebuilt" class="ml8 warning-text">
              {{ t('pipelines.aiC2Warn') }}
            </span>
          </el-descriptions-item>
        </el-descriptions>

        <!-- 多管道：逐组展示 -->
        <template v-if="aiInfo.groups && aiInfo.groups.length">
          <div class="mt12 bold">{{ t('pipelines.aiMultiTitle') }}</div>
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
          <div class="mt12 bold">{{ t('pipelines.aiSingleTitle') }}</div>
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
            <el-tag size="small" type="warning">{{ t('pipelines.aiSuppTag') }}</el-tag>
            <span class="ml4">{{ aiInfo.supplemented.join('、') }}</span>
          </div>
          <div v-else class="mt8 gray">{{ t('pipelines.aiPure') }}</div>
        </template>

        <div class="mt12 gray">
          {{ t('pipelines.aiAuditNote') }}
        </div>
      </template>
      <template v-else>
        <el-empty :description="t('pipelines.aiEmpty')" />
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { datasourceApi, mappingApi, modelApi, pipelineApi, targetApi } from '../api/dataflow'

const { t } = useI18n()

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

// 检查项名称（后端 check 字段 → 本地化标签）
function checkLabel(key) {
  const map = {
    pipeline: t('pipelines.chkTopology'),
    compile: t('pipelines.chkCompile'),
    start: t('pipelines.chkStart'),
    smoke: t('pipelines.chkSmoke'),
    settle: t('pipelines.chkSettle'),
    target_effect: t('pipelines.chkTarget'),
  }
  return map[key] || key
}

// 验证报告标题（按状态/错误数生成）
const validationTitle = computed(() => {
  const v = validation.value
  if (!v) return ''
  const warns = v.warning_count || 0
  if (!v.ok) {
    return warns
      ? t('pipelines.valErrorN', { err: v.error_count, warn: warns })
      : t('pipelines.valError', { err: v.error_count })
  }
  return warns ? t('pipelines.valWarn', { warn: warns }) : t('pipelines.valOk')
})

// 验证报告 alert 类型
const validationType = computed(() => {
  const v = validation.value
  if (!v) return 'success'
  if (!v.ok) return 'error'
  return v.warning_count ? 'warning' : 'success'
})

let timer = null

// —— 许可与组件（许可预算）：IRIS 社区版 KeyLicenseUnits=8，每个 Ens 业务主机占 1 个 ——
const prodItems = ref([])        // Production 组件清单（name/className/enabled）
const licenseUnits = ref(0)      // 许可单元总数（社区版 8）
const enabledCount = ref(0)      // 当前启用组件数（+1 个后端连接即为占用）
const budget = ref(null)         // 本次生成的许可预算结果（disabled/over_capacity/note）

// 许可预算提示：本次生成让路的旧管道组件 / 许可调度结果（已启用分组 + 因上限停用的分组）
const budgetTitle = computed(() => {
  const b = budget.value
  if (!b) return ''
  const parts = []
  if ((b.disabled || []).length) {
    parts.push(`${t('pipelines.budgetDisabled')}: ${b.disabled.join(', ')}`)
  }
  if ((b.scheduled || []).length) {
    parts.push(`${t('pipelines.budgetScheduled')}: ${b.scheduled.join(', ')}`)
  }
  if (b.over_capacity) parts.push(b.note || t('pipelines.budgetOver'))
  return parts.join(' ｜ ')
})

async function loadItems() {
  const data = await pipelineApi.items()
  prodItems.value = data?.items || []
  licenseUnits.value = data?.units || 0
  enabledCount.value = data?.enabled_count || 0
}

// —— 数据管道（受管理实体）：管道 = 源 + 目标 + 设计 Skill；类别 = 组件的 Ens Category ——
const instances = ref([])        // 管道实体清单
const instanceGroups = ref([])   // 按管道类别的折叠分组
const openGroups = ref([])       // 展开的类别
const syncingInstances = ref(false)

const activeInstanceCount = computed(
  () => instances.value.filter((r) => r.status === 'active').length
)

// 管道状态三态：active=运行中 / suspended=被许可预算让路（可原地启用）/ superseded=已被新生成取代
const STATUS_TAG_TYPE = { active: 'success', suspended: 'info', superseded: 'warning' }
const STATUS_TEXT_KEY = {
  active: 'pipelines.instanceActive',
  suspended: 'pipelines.instanceSuspended',
  superseded: 'pipelines.instanceSuperseded'
}

function statusTagType(status) {
  return STATUS_TAG_TYPE[status] || 'info'
}

function statusText(status) {
  return t(STATUS_TEXT_KEY[status] || 'pipelines.instanceSuspended')
}

async function loadInstances() {
  const data = await pipelineApi.instances()
  instances.value = data?.items || []
  instanceGroups.value = data?.groups || []
  // 默认展开活动管道所在分组（让"谁在吃许可"一眼可见）
  const actives = instanceGroups.value.filter((g) => g.active).map((g) => g.category)
  openGroups.value = actives.length ? actives : instanceGroups.value.map((g) => g.category)
}

async function handleSyncInstances() {
  syncingInstances.value = true
  try {
    const res = await pipelineApi.syncInstances()
    ElMessage.success(t('pipelines.instanceSyncDone', {
      created: res?.created ?? 0, updated: res?.updated ?? 0,
    }))
  } finally {
    syncingInstances.value = false
    await Promise.all([loadInstances(), loadItems()])
  }
}

async function handleToggleInstance(row, enabled) {
  try {
    if (enabled) {
      const res = await pipelineApi.enableInstance(row.id)
      // 一键切换：许可不足时后端自动停用其它管道的组件腾单元（显式告知，不静默）
      const leaked = res?.disabled_others || []
      if (leaked.length) {
        ElMessage.warning(t('pipelines.instanceSwitched', { items: leaked.join(', ') }))
      } else {
        ElMessage.success(t('pipelines.instanceEnabledDone'))
      }
    } else {
      await pipelineApi.disableInstance(row.id)
      ElMessage.success(t('pipelines.instanceDisabledDone'))
    }
  } finally {
    await Promise.all([loadInstances(), loadItems()])
  }
}

async function handleDeleteInstance(row) {
  await ElMessageBox.confirm(
    t('pipelines.instanceDeleteConfirm'), t('pipelines.instanceDelete'), { type: 'warning' })
  await pipelineApi.deleteInstance(row.id)
  ElMessage.success(t('pipelines.instanceDeleteDone'))
  await loadInstances()
}

async function handleToggleItem(row, enabled) {
  try {
    const res = await pipelineApi.toggleItem({ name: row.name, enabled })
    ElMessage.success(res?.message || t('pipelines.toggleDone'))
  } finally {
    await loadItems()
  }
}

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
  await Promise.all([loadViewTables(), loadLogs(), loadItems(), loadInstances()])
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
  // 资产键 → 候选数据源（同名资产可能存在于多个数据源，必须消歧）
  // 键同时登记 **资产名 / 资产 ID / 两者末段**：mapping.source 两种口径都可能出现
  // （Agent A 可能回填资产 ID DS71120_TSQLUser.Patient），只按资产名索引会让源数据源匹配失败
  // → source_id 丢失 → 源类型退化成兜底推测（缺陷 N11）
  const assetCands = {}
  const addCand = (key, dsId, dsType) => {
    const k = String(key || '').trim()
    if (!k) return
    assetCands[k] = assetCands[k] || []
    if (!assetCands[k].some((c) => c.dsId === dsId)) assetCands[k].push({ dsId, dsType })
  }
  for (const d of dsList) {
    const assets = (await datasourceApi.assets(d.id))?.items || []
    for (const a of assets) {
      if (!a) continue
      for (const k of [a.name, a.id, String(a.name || '').split('.').pop(), String(a.id || '').split('.').pop()]) {
        addCand(k, d.id, d.type || '')
      }
    }
  }
  // 映射的源候选：source → asset → source 末段 依次回退
  const candsOf = (m) => {
    const s = String(m?.source || '')
    return assetCands[s] || assetCands[s.split('.').pop()] || assetCands[String(m?.asset || '')] || []
  }
  // 目标表/实体 → 目标记录（含 type）
  const tgtByTable = {}
  for (const t of targetRows || []) {
    if (t && t.table && !tgtByTable[t.table]) tgtByTable[t.table] = t
  }
  // 先按「目标」分组：同一目标的映射必须归到同一个源数据源；
  // 数据源选择 = 能覆盖该组源表最多的那个（避免「两个数据源都有同名 Patient 表」把一组映射拆散，
  // 例如 FHIR 组的 Patient/Encounter/Diagnosis/MedicationOrder 应整体归到同时含这些表的数据源）
  const byTarget = {}
  for (const m of mappings) {
    const tg = tgtByTable[m.target_table]
    const key = `${tg?.target_id || tg?.id || m.target_table}`
    byTarget[key] = byTarget[key] || []
    byTarget[key].push(m)
  }
  const pipelines = []
  for (const ms of Object.values(byTarget)) {
    const score = {}
    for (const m of ms) {
      for (const c of candsOf(m)) {
        score[c.dsId] = (score[c.dsId] || 0) + 1
      }
    }
    const best = Object.entries(score).sort((a, b) => b[1] - a[1])[0]
    const dsId = best ? best[0] : ''
    const dsType = (candsOf(ms[0]).find((c) => c.dsId === dsId) || {}).dsType || ''
    const tg = tgtByTable[ms[0].target_table] || {}
    pipelines.push({
      source_type: dsType || (ms[0].target_type === 'SOAP' ? 'FHIR' : 'SQL'),
      source_id: dsId || undefined,
      target_type: ms[0].target_type || (tg.type === 'SOAP' ? 'SOAP' : 'DB'),
      target_id: tg.target_id || tg.id,
      mappings: ms,
    })
  }
  return pipelines
}

async function handleGenerate() {
  // 使用已确认的转换关系生成管道（含 FHIR 数据源配置）
  const data = await mappingApi.list()
  const mappings = data?.items || []
  if (!mappings.length) {
    ElMessage.warning(t('pipelines.warnNoMapping'))
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
    budget.value = result?.license_budget || null
    if (result?.pipeline_error) {
      // 生成成功但管道实体未登记：显式提示（不静默）
      ElMessage.warning(t('pipelines.pipelineError', { msg: result.pipeline_error }))
    }
    if (result?.ai) {
      aiInfo.value = result.ai
      aiGeneratedAt.value = new Date().toLocaleString()
    }
    // 许可调度：超许可上限的分组已生成但处于停用状态 → 明确提示（不营造"全部启动"假象）
    if (result?.license_budget?.over_capacity) {
      ElMessage.warning(t('pipelines.generateDonePartial'))
    } else {
      ElMessage.success(t('pipelines.generateDone'))
    }
    await refreshAll()
  } finally {
    generating.value = false
  }
}

async function handleGenerateMock() {
  mockGenerating.value = true
  try {
    const data = await pipelineApi.generateMock(3)
    ElMessage.success(data?.message || t('pipelines.mockDone'))
    // 等待增量同步 + 逐条转换后自动刷新落库结果
    setTimeout(async () => {
      await Promise.all([loadLogs(), loadTargetData()])
      ElMessage.info(t('pipelines.refreshDone'))
    }, 12000)
  } finally {
    mockGenerating.value = false
  }
}

async function handleRun() {
  const data = await pipelineApi.run()
  ElMessage.success(data?.result || t('pipelines.triggered'))
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
