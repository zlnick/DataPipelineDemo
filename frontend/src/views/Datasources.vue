<template>
  <div class="datasources-page">
    <!-- 注册数据源表单 -->
    <el-card shadow="never" class="mb16">
      <template #header><span class="card-title">{{ t('datasources.register') }}</span></template>
      <el-form :inline="true" :model="form" label-width="80px" @submit.prevent>
        <el-form-item :label="t('datasources.name')">
          <el-input v-model="form.name" :placeholder="t('datasources.namePlaceholder')" style="width: 180px" />
        </el-form-item>
        <el-form-item :label="t('datasources.type')">
          <el-select v-model="form.type" style="width: 180px">
            <el-option
              v-for="opt in DATA_SOURCE_TYPES"
              :key="opt.value"
              :label="dsTypeLabel(opt.value) + (opt.enabled ? '' : t('datasources.reserved'))"
              :value="opt.value"
              :disabled="!opt.enabled"
            >
              <el-tooltip :content="opt.desc" placement="left" :disabled="opt.enabled">
                <span>{{ dsTypeLabel(opt.value) }}{{ opt.enabled ? '' : t('datasources.reservedTip') }}</span>
              </el-tooltip>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.type === 'FHIR'" :label="t('datasources.endpoint')">
          <el-input
            v-model="form.endpoint"
            placeholder="http://host:52773/csp/healthshare/fhirserver/fhir/r4/"
            style="width: 420px"
          />
        </el-form-item>
        <el-form-item :label="t('datasources.username')">
          <el-input v-model="form.username" placeholder="superuser" style="width: 120px" />
        </el-form-item>
        <el-form-item :label="t('datasources.password')">
          <el-input v-model="form.password" type="password" placeholder="SYS" style="width: 120px" show-password />
        </el-form-item>
        <!-- SQL 源配置：JDBC 连接（选表后自动生成轮询 Query） -->
        <el-form-item v-if="form.type === 'SQL'" label="JDBC URL">
          <el-input v-model="form.jdbc_url" placeholder="jdbc:IRIS://iris:1972/USER" style="width: 300px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'SQL'" label="驱动类">
          <el-input v-model="form.driver_class" placeholder="com.intersystems.jdbc.IRISDriver" style="width: 240px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="creating" @click="handleCreate">
            {{ t('datasources.register') }}
          </el-button>
        </el-form-item>
      </el-form>
      <el-alert
        :title="t('datasources.note')"
        type="info"
        :closable="false"
        show-icon
      />
    </el-card>

    <!-- 数据源列表 -->
    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('datasources.list') }}</span>
        <el-button size="small" style="float: right" @click="loadList">{{ t('datasources.refresh') }}</el-button>
      </template>
      <el-table :data="list" v-loading="loading" border stripe>
        <el-table-column prop="id" :label="t('datasources.idCol')" width="90" />
        <el-table-column prop="name" :label="t('datasources.nameCol')" min-width="120" />
        <el-table-column prop="type" :label="t('datasources.typeCol')" width="90">
          <template #default="{ row }">
            <el-tag :type="row.type === 'FHIR' ? 'success' : 'info'">{{ row.type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="endpoint" :label="t('datasources.endpointCol')" min-width="260" show-overflow-tooltip />
        <el-table-column prop="status" :label="t('datasources.statusCol')" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'analyzed' ? 'success' : 'warning'">
              {{ row.status === 'analyzed' ? t('datasources.analyzed') : t('datasources.registered') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('datasources.runtimeCol')" min-width="220">
          <template #default="{ row }">
            <div class="rt-cell">
              <span class="gray sm">{{ runtimeSummary(row) }}</span>
              <el-tooltip :content="runtimeHealthDetail(row)" placement="top">
                <el-tag size="small" :type="runtimeHealthOk(row) ? 'success' : 'danger'" class="ml4">
                  {{ runtimeHealthOk(row) ? t('datasources.healthOk') : t('datasources.healthBad') }}
                </el-tag>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('datasources.actions')" width="320" fixed="right">
          <template #default="{ row }">
            <template v-if="row.type === 'SQL'">
              <el-button size="small" type="primary" :loading="seedingId === row.id" @click="handleSeed(row)">
                {{ t('datasources.genDemo') }}
              </el-button>
              <el-button size="small" type="primary" @click="openSourceWizard(row)">{{ t('datasources.selectTables') }}</el-button>
              <el-button size="small" @click="goAssets(row.id)">{{ t('datasources.assets') }}</el-button>
            </template>
            <template v-else>
              <el-button size="small" type="primary" :loading="analyzingId === row.id" @click="handleAnalyze(row)">
                {{ t('datasources.profileAnalyze') }}
              </el-button>
              <el-button size="small" @click="goAssets(row.id)">{{ t('datasources.assets') }}</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 分析结果对话框 -->
    <el-dialog v-model="analysisVisible" :title="t('datasources.analyzeResult')" width="720px">
      <template v-if="analysis">
        <el-descriptions :column="3" border>
          <el-descriptions-item :label="t('datasources.fhirVersion')">{{ analysis.fhir_version }}</el-descriptions-item>
          <el-descriptions-item :label="t('datasources.status')">{{ analysis.status }}</el-descriptions-item>
          <el-descriptions-item :label="t('datasources.resourceCount')">{{ analysis.resource_count }}</el-descriptions-item>
          <el-descriptions-item :label="t('datasources.profileHint')" :span="3">
            {{ (analysis.profile_hint || []).join('、') || t('datasources.standardFhir') }}
          </el-descriptions-item>
        </el-descriptions>
        <div class="mt12">
          <div class="mb8"><b>{{ t('datasources.resourceTypes') }}</b></div>
          <el-tag v-for="rt in analysis.resource_types.slice(0, 30)" :key="rt" class="mr6 mb6" size="small">
            {{ rt }}
          </el-tag>
          <el-tag v-if="analysis.resource_types.length > 30" size="small" type="info">
            {{ t('datasources.etcTotal', { n: analysis.resource_types.length }) }}
          </el-tag>
        </div>
        <el-alert
          class="mt12"
          :title="t('datasources.autoRegistered')"
          type="success"
          :closable="false"
        />
      </template>
    </el-dialog>

    <!-- SQL 源选表向导（连通 → schema → 表 → 分析列） -->
    <el-dialog v-model="srcWizardVisible" :title="t('datasources.srcWizTitle', { name: srcCurrentName })" width="680px">
      <el-steps :active="srcStep" finish-status="success" align-center class="mb16">
        <el-step :title="t('datasources.stepTest')" />
        <el-step :title="t('datasources.stepSchema')" />
        <el-step :title="t('datasources.stepTables')" />
        <el-step :title="t('datasources.stepSave')" />
      </el-steps>

      <div v-if="srcStep === 0" class="center">
        <el-button type="primary" :loading="srcTesting" @click="doSourceTest">{{ t('datasources.btnRunTest') }}</el-button>
        <el-tag v-if="srcTestResult !== null" :type="srcTestResult ? 'success' : 'danger'" class="ml12">
          {{ srcTestResult ? t('datasources.connOk') : t('datasources.connFail') }}
        </el-tag>
      </div>

      <div v-else-if="srcStep === 1">
        <el-alert class="mb12" :title="t('datasources.schemaAlert')" type="info" :closable="false" />
        <el-select v-model="srcSchema" :placeholder="t('datasources.phSchema')" filterable style="width: 100%">
          <el-option v-for="s in srcSchemas" :key="s" :label="s" :value="s" />
        </el-select>
        <div class="mt12 center">
          <el-button type="primary" :disabled="!srcSchema" @click="loadSourceTables">{{ t('datasources.btnNext') }}</el-button>
        </div>
      </div>

      <div v-else-if="srcStep === 2">
        <el-alert class="mb12" :title="t('datasources.checkAlert')" type="info" :closable="false" />
        <el-table ref="srcTableRef" :data="srcTables" border max-height="320" @selection-change="onSrcSelChange">
          <el-table-column type="selection" width="50" />
          <el-table-column prop="table" :label="t('datasources.colTable')" min-width="160" />
          <el-table-column prop="type" :label="t('datasources.colType')" width="90" />
        </el-table>
        <div class="mt12 center">
          <el-button type="primary" :disabled="!srcChecked.length" @click="saveSourceTables">{{ t('datasources.btnSaveCols') }}</el-button>
        </div>
      </div>

      <div v-else class="center">
        <el-alert :title="t('datasources.doneAlert')" type="success" :closable="false" />
        <div class="mt12"><el-button type="primary" @click="srcWizardVisible = false">{{ t('datasources.btnDone') }}</el-button></div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { nextTick, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { DATA_SOURCE_TYPES } from '../api/constants'
import { datasourceApi } from '../api/dataflow'

const router = useRouter()
const { t } = useI18n()
const list = ref([])

// 数据源类型显示名（value → 本地化 label）
function dsTypeLabel(v) {
  const map = {
    FHIR: t('datasources.typeFHIR'),
    SQL: t('datasources.typeSQL'),
    REST: t('datasources.typeREST'),
    SOAP: t('datasources.typeSOAP'),
  }
  return map[v] || v
}
const loading = ref(false)
const creating = ref(false)
const analyzingId = ref('')
const analysisVisible = ref(false)
const analysis = ref(null)

const form = reactive({
  name: t('datasources.defName'),
  type: 'FHIR',
  endpoint: 'http://iris:52773/csp/healthshare/fhirserver/fhir/r4/',
  username: 'superuser',
  password: 'SYS',
  // SQL 源配置（JDBC 连接，选表后自动生成轮询 Query）
  jdbc_url: 'jdbc:IRIS://iris:1972/USER',
  driver_class: 'com.intersystems.jdbc.IRISDriver',
})

async function loadList() {
  loading.value = true
  try {
    const data = await datasourceApi.list()
    list.value = data?.items || []
  } finally {
    loading.value = false
  }
}

// 运行契约摘要（Connection Contract，后端 analyze/选表后自动刷新）
function runtimeSummary(row) {
  const rt = row.runtime || {}
  const kind = (row.type || '').toUpperCase()
  const caps = rt.capabilities || {}
  const poll = rt.poll || {}
  if (kind === 'FHIR') {
    const mode = caps.incremental_search
      ? t('datasources.incSearch', { m: caps.incremental_search })
      : (poll.note ? t('datasources.fullLimited') : t('datasources.full'))
    return `FHIR ${caps.fhir_version || '-'} · ${mode}`
  }
  if (kind === 'SQL') {
    const q = (rt.connection || {}).query || ''
    const tbl = q.includes('FROM ') ? q.split('FROM ').pop().trim() : ''
    return t('datasources.pollSummary', { t: tbl || '-', k: poll.key_column || '-' })
  }
  return '-'
}
function runtimeHealthOk(row) {
  return !!(row.runtime && row.runtime.health && row.runtime.health.ok)
}
function runtimeHealthDetail(row) {
  return (row.runtime && row.runtime.health && row.runtime.health.detail) || ''
}

const seedingId = ref('')

async function handleSeed(row) {
  if (row.type !== 'SQL') return
  seedingId.value = row.id
  try {
    const data = await datasourceApi.seed(row.id, { patients: 10 })
    const s = data?.data || {}
    ElMessage.success(t('datasources.seedOk', {
      p: s.patients ?? 0, e: s.encounters ?? 0, d: s.diagnoses ?? 0, m: s.medications ?? 0,
    }))
  } catch {
    // 错误已由拦截器提示
  } finally {
    seedingId.value = ''
  }
}

async function handleCreate() {
  if (!form.name.trim()) {
    ElMessage.warning(t('datasources.warnNameEndpoint'))
    return
  }
  if (form.type === 'FHIR' && !form.endpoint.trim()) {
    ElMessage.warning(t('datasources.warnNameEndpoint'))
    return
  }
  if (form.type === 'SQL' && !form.jdbc_url.trim()) {
    ElMessage.warning(t('datasources.warnJdbc'))
    return
  }
  creating.value = true
  try {
    const payload = {
      name: form.name.trim(),
      type: form.type,
      endpoint: form.type === 'FHIR' ? form.endpoint.trim() : '',
      auth: { type: 'basic', username: form.username, password: form.password },
    }
    if (form.type === 'SQL') {
      payload.config = {
        jdbc_url: form.jdbc_url.trim(),
        driver_class: form.driver_class.trim(),
        username: form.username,
        password: form.password,
      }
    }
    await datasourceApi.create(payload)
    ElMessage.success(t('datasources.created'))
    await loadList()
  } finally {
    creating.value = false
  }
}

async function handleAnalyze(row) {
  analyzingId.value = row.id
  try {
    const data = await datasourceApi.analyze(row.id)
    analysis.value = data?.analysis
    analysisVisible.value = true
    ElMessage.success(t('datasources.analyzeDone', { n: data?.asset_count ?? 0 }))
    await loadList()
  } finally {
    analyzingId.value = ''
  }
}

function goAssets(id) {
  router.push({ path: '/assets', query: { source_id: id } })
}

// ---- SQL 源选表向导（连通 → schema → 表 → 分析列） ----
const srcWizardVisible = ref(false)
const srcCurrentId = ref('')
const srcCurrentName = ref('')
const srcStep = ref(0)
const srcTesting = ref(false)
const srcTestResult = ref(null)
const srcSchemas = ref([])
const srcSchema = ref('')
const srcTables = ref([])
const srcChecked = ref([])
const srcTableRef = ref(null)

function openSourceWizard(row) {
  srcCurrentId.value = row.id
  srcCurrentName.value = row.name
  srcStep.value = 0
  srcTestResult.value = null
  srcSchemas.value = []
  srcSchema.value = ''
  srcTables.value = []
  srcChecked.value = []
  srcWizardVisible.value = true
  loadSourceSchemas()
}

async function loadSourceSchemas() {
  try {
    const data = await datasourceApi.schemas(srcCurrentId.value)
    srcSchemas.value = data?.items || []
  } catch {
    // 错误提示已由拦截器处理
  }
}

async function doSourceTest() {
  srcTesting.value = true
  try {
    await datasourceApi.test(srcCurrentId.value)
    srcTestResult.value = true
    srcStep.value = 1
  } catch {
    srcTestResult.value = false
  } finally {
    srcTesting.value = false
  }
}

async function loadSourceTables() {
  try {
    const data = await datasourceApi.tables(srcCurrentId.value, srcSchema.value)
    srcTables.value = data?.items || []
    srcStep.value = 2
    // 预勾选**已保存**的表：二次进向导时不会因为"只勾新表"而丢旧表（后端已按 merge 保存）
    await nextTick()
    const tRef = srcTableRef.value
    if (tRef) {
      srcTables.value.filter((r) => r.selected).forEach((r) => tRef.toggleRowSelection(r, true))
    }
  } catch {
    // 错误提示已由拦截器处理
  }
}

function onSrcSelChange(rows) {
  srcChecked.value = rows
}

async function saveSourceTables() {
  const tables = srcChecked.value.map((t) => ({ schema: srcSchema.value, table: t.table }))
  try {
    const data = await datasourceApi.selectTables(srcCurrentId.value, tables)
    srcStep.value = 3
    ElMessage.success(t('datasources.savedTables', { n: data?.total || data?.count || tables.length }))
    await loadList()
  } catch {
    // 错误提示已由拦截器处理
  }
}

onMounted(loadList)
</script>

<style scoped>
.mb16 { margin-bottom: 16px; }
.mb8 { margin-bottom: 8px; }
.mt12 { margin-top: 12px; }
.mr6 { margin-right: 6px; }
.mb6 { margin-bottom: 6px; }
.card-title { font-weight: 600; }
</style>

