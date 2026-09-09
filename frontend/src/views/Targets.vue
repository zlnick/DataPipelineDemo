<template>
  <div class="targets-page">
    <!-- ① 添加数据目标 -->
    <el-card shadow="never" class="mb16">
      <template #header><span class="card-title">{{ t('targets.addHeader') }}</span></template>
      <el-form :inline="true" label-width="90px" @submit.prevent>
        <el-form-item :label="t('targets.fldName')">
          <el-input v-model="form.name" :placeholder="t('targets.namePlaceholder')" style="width: 170px" />
        </el-form-item>
        <el-form-item :label="t('targets.fldType')">
          <el-select v-model="form.type" style="width: 130px">
            <el-option
              v-for="opt in TARGET_TYPES"
              :key="opt.value"
              :label="typeLabel(opt.value) + (opt.enabled ? '' : '（' + t('targets.reserved') + '）')"
              :value="opt.value"
              :disabled="!opt.enabled"
            >
              <el-tooltip :content="opt.desc" placement="left" :disabled="opt.enabled">
                <span>{{ typeLabel(opt.value) }}{{ opt.enabled ? '' : '（' + t('targets.reserved') + '）' }}</span>
              </el-tooltip>
            </el-option>
          </el-select>
        </el-form-item>
        <!-- DB 目标：JDBC 连接 -->
        <el-form-item v-if="form.type === 'DB'" :label="t('targets.fldJdbcUrl')">
          <el-input v-model="form.jdbc_url" placeholder="jdbc:IRIS://iris:1972/USER" style="width: 320px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'DB'" :label="t('targets.fldDriver')">
          <el-input v-model="form.driver_class" placeholder="com.intersystems.jdbc.IRISDriver" style="width: 260px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'DB'" :label="t('targets.fldUser')">
          <el-input v-model="form.username" placeholder="superuser" style="width: 120px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'DB'" :label="t('targets.fldPass')">
          <el-input v-model="form.password" type="password" placeholder="SYS" style="width: 120px" show-password />
        </el-form-item>
        <!-- SOAP 目标：WSDL 导入型 -->
        <el-form-item v-if="form.type === 'SOAP'" :label="t('targets.fldWsdl')">
          <el-input v-model="form.wsdl" :placeholder="t('targets.phWsdl')" style="width: 320px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'SOAP'" :label="t('targets.fldService')">
          <el-input v-model="form.service" placeholder="PatientService" style="width: 180px" />
        </el-form-item>
        <!-- FHIR 目标：US Core 存储库（base_url + 认证） -->
        <el-form-item v-if="form.type === 'FHIR'" :label="t('targets.fldBaseUrl')">
          <el-input v-model="form.base_url" :placeholder="t('targets.phFhirBase')" style="width: 460px" />
        </el-form-item>
        <template v-if="form.type === 'FHIR'">
          <el-form-item :label="t('targets.fldUser')">
            <el-input v-model="form.username" placeholder="superuser" style="width: 150px" />
          </el-form-item>
          <el-form-item :label="t('targets.fldPass')">
            <el-input v-model="form.password" type="password" placeholder="SYS" style="width: 120px" show-password />
          </el-form-item>
        </template>
        <el-form-item>
          <el-button type="primary" :loading="creating" @click="handleCreate">{{ t('targets.addBtn') }}</el-button>
        </el-form-item>
      </el-form>
      <el-alert
        :title="infoTitle"
        type="info"
        :closable="false"
        show-icon
      />
    </el-card>

    <!-- ② 数据目标管理 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <span class="card-title">{{ t('targets.mgmtTitle') }}</span>
        <el-button size="small" style="float: right" @click="loadManage">{{ t('pipelines.refreshBtn') }}</el-button>
      </template>
      <el-table :data="managers" v-loading="manageLoading" border stripe>
        <el-table-column prop="name" :label="t('targets.fldName')" min-width="140" />
        <el-table-column prop="type" :label="t('targets.fldType')" width="80" />
        <el-table-column prop="status" :label="t('targets.colStatus')" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'analyzed' ? 'success' : row.status === 'connected' ? 'primary' : row.status === 'error' ? 'danger' : 'warning'">
              {{ row.status === 'analyzed' ? t('targets.analyzed') : row.status === 'connected' ? t('targets.connected') : row.status === 'error' ? t('targets.failed') : t('targets.registered') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('targets.colSelected')" width="80">
          <template #default="{ row }">
            <span v-if="row.type === 'SOAP'">—</span>
            <span v-else>{{ t('targets.selCount', { n: (row.tables || []).length }) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="connection" :label="t('targets.colConn')" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="row.type === 'SOAP'">
              <el-tag size="small" type="info">{{ row.connection?.service || 'default' }}</el-tag>
              <span class="ml6 gray">{{ row.connection?.wsdl }}</span>
            </template>
            <template v-else>{{ row.connection?.jdbc_url }}</template>
          </template>
        </el-table-column>
        <el-table-column :label="t('targets.colDelivery')" min-width="220">
          <template #default="{ row }">
            <template v-if="row.type === 'SOAP'">
              <div v-for="op in (row.runtime?.capabilities?.operations || [])" :key="op.name" class="mb4">
                <el-tag size="small" :type="op.kind === 'write' ? 'success' : 'warning'">
                  {{ op.name }} · {{ op.kind === 'write' ? t('targets.opWrite') : t('targets.opQuery') }}
                </el-tag>
              </div>
              <div v-if="row.runtime?.delivery?.note" class="mb4 gray sm">{{ row.runtime.delivery.note }}</div>
            </template>
            <template v-else><el-tag size="small" type="info">DB UPSERT</el-tag></template>
            <el-tooltip :content="(row.runtime?.health?.detail) || ''" placement="top">
              <el-tag size="small" :type="row.runtime?.health?.ok ? 'success' : 'danger'">
                {{ row.runtime?.health?.ok ? t('targets.reachOk') : t('targets.reachNa') }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column :label="t('targets.colOps')" width="220" fixed="right">
          <template #default="{ row }">
            <template v-if="row.type === 'SOAP'">
              <el-button size="small" :loading="importingId === row.id" @click="handleImport(row)">{{ t('targets.btnReimport') }}</el-button>
            </template>
            <template v-else>
              <el-button size="small" :loading="testingId === row.id" @click="handleTest(row)">{{ t('targets.test') }}</el-button>
              <el-button size="small" type="primary" @click="openWizard(row)">{{ t('targets.selectTables') }}</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ③ 已选目标表 -->
    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('targets.selected') }}</span>
        <el-button size="small" style="float: right" @click="loadTargetTables">{{ t('pipelines.refreshBtn') }}</el-button>
      </template>
      <el-table :data="targetTables" v-loading="tablesLoading" border stripe>
        <el-table-column prop="target_name" :label="t('targets.colOwner')" min-width="130" />
        <el-table-column prop="table" :label="t('targets.colTable')" min-width="130">
          <template #default="{ row }"><el-tag type="primary">{{ row.table }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="schema" :label="t('targets.colSchema')" min-width="100" />
        <el-table-column :label="t('targets.colCols')" min-width="300">
          <template #default="{ row }">
            <el-tag v-for="c in row.columns" :key="c" size="small" class="mr6 mb4">{{ c }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="count" :label="t('targets.colRows')" width="80" />
        <el-table-column :label="t('targets.colOps')" width="120" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.type !== 'SOAP'" size="small" type="primary" plain @click="showData(row.table)">{{ t('targets.btnView') }}</el-button>
            <el-tag v-else size="small" type="info">{{ t('targets.soapEntity') }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 选择目标表向导 -->
    <el-dialog v-model="wizardVisible" :title="t('targets.wizTitle', { name: currentName })" width="720px">
      <el-steps :active="wizardStep" finish-status="success" align-center class="mb16">
        <el-step :title="t('targets.stepTest')" />
        <el-step :title="t('targets.stepSchema')" />
        <el-step :title="t('targets.stepTables')" />
        <el-step :title="t('targets.stepSave')" />
      </el-steps>

      <div v-if="wizardStep === 0" class="center">
        <el-button type="primary" :loading="testingId === currentId" @click="doTest">{{ t('targets.btnRunTest') }}</el-button>
        <el-tag v-if="testResult !== null" :type="testResult ? 'success' : 'danger'" class="ml12">
          {{ testResult ? t('targets.connOk') : t('targets.connFail') }}
        </el-tag>
      </div>

      <div v-else-if="wizardStep === 1">
        <el-alert class="mb12" :title="t('targets.schema')" type="info" :closable="false" />
        <el-select v-model="wizardSchema" :placeholder="t('targets.phSchema')" filterable style="width: 100%">
          <el-option v-for="s in schemaList" :key="s" :label="s" :value="s" />
        </el-select>
        <div class="mt12">
          <el-button type="primary" :disabled="!wizardSchema" @click="loadWizardTables">{{ t('targets.btnNext') }}</el-button>
        </div>
      </div>

      <div v-else-if="wizardStep === 2">
        <el-alert class="mb12" :title="t('targets.checkNote')" type="info" :closable="false" />
        <el-table :data="wizardTables" border max-height="300" @selection-change="onTableSelect">
          <el-table-column type="selection" width="50" />
          <el-table-column prop="table" :label="t('targets.colTable')" min-width="160" />
          <el-table-column prop="type" :label="t('targets.fldType')" width="90" />
        </el-table>
        <div class="mt12">
          <el-button @click="wizardStep = 1">{{ t('targets.btnPrev') }}</el-button>
          <el-button type="primary" :disabled="!checkedTables.length" @click="saveSelectedTables">
            {{ t('targets.btnSaveCols') }}
          </el-button>
        </div>
      </div>

      <div v-else>
        <el-alert :title="t('targets.savedNote')" type="success" :closable="false" />
        <div v-for="t in savedResult" :key="t.table" class="mt12">
          <b>{{ t.table }}：</b>
          <el-tag v-for="c in t.columns" :key="c.name" size="small" class="mr6">{{ c.name }}</el-tag>
        </div>
        <div class="mt12"><el-button type="primary" @click="closeWizard">{{ t('targets.btnDone') }}</el-button></div>
      </div>
    </el-dialog>

    <!-- 表数据对话框 -->
    <el-dialog v-model="dataVisible" :title="t('targets.viewDataTitle', { t: currentTable })" width="860px">
      <el-table :data="rows" border stripe max-height="480">
        <el-table-column
          v-for="col in columns"
          :key="col"
          :prop="col"
          :label="col"
          min-width="110"
          show-overflow-tooltip
        />
      </el-table>
      <div class="mt12">{{ t('targets.rows', { n: rows.length }) }}</div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { TARGET_TYPES } from '../api/constants'
import { targetApi } from '../api/dataflow'

const { t } = useI18n()

// 类型显示名（value → 本地化 label）
function typeLabel(v) {
  const map = { DB: t('targets.tDB'), SOAP: t('targets.tSOAP'), FHIR: t('targets.tFHIR'), REST: t('targets.tREST') }
  return map[v] || v
}

const infoTitle = computed(() => {
  if (form.type === 'SOAP') return t('targets.soapInfo')
  if (form.type === 'FHIR') return t('targets.fhirInfo')
  return t('targets.dbInfo')
})

const managers = ref([])
const targetTables = ref([])
const manageLoading = ref(false)
const tablesLoading = ref(false)
const creating = ref(false)
const testingId = ref('')
const importingId = ref('')

const form = reactive({
  name: t('targets.defName'),
  type: 'DB',
  jdbc_url: 'jdbc:IRIS://iris:1972/USER',
  driver_class: 'com.intersystems.jdbc.IRISDriver',
  username: 'superuser',
  password: 'SYS',
  // SOAP 目标配置（WSDL 导入型）
  wsdl: '/tmp/patient.wsdl',
  service: 'PatientService',
  // FHIR 目标配置（US Core 声明式存储库）
  base_url: 'http://iris:52773/csp/healthshare/fhirserver/fhir/r4',
})

async function loadManage() {
  manageLoading.value = true
  try {
    const data = await targetApi.manage()
    managers.value = data?.items || []
  } finally {
    manageLoading.value = false
  }
}

async function loadTargetTables() {
  tablesLoading.value = true
  try {
    const data = await targetApi.list()
    targetTables.value = data?.items || []
  } finally {
    tablesLoading.value = false
  }
}

async function handleCreate() {
  if (!form.name.trim()) {
    ElMessage.warning(t('targets.warnName'))
    return
  }
  if (form.type === 'DB' && !form.jdbc_url.trim()) {
    ElMessage.warning(t('targets.fldJdbcUrl'))
    return
  }
  if (form.type === 'SOAP' && !form.wsdl.trim()) {
    ElMessage.warning(t('targets.wsdlNeeded'))
    return
  }
  if (form.type === 'FHIR' && !form.base_url.trim()) {
    ElMessage.warning(t('targets.fldBaseUrl'))
    return
  }
  creating.value = true
  try {
    const connection = form.type === 'SOAP'
      ? { wsdl: form.wsdl.trim(), service: form.service.trim() || 'default' }
      : form.type === 'FHIR'
        ? { base_url: form.base_url.trim(), username: form.username, password: form.password }
        : {
            jdbc_url: form.jdbc_url.trim(),
            driver_class: form.driver_class.trim(),
            username: form.username,
            password: form.password,
          }
    await targetApi.create({ name: form.name.trim(), type: form.type, connection })
    const okMsg = form.type === 'SOAP' ? t('targets.soapCreated')
      : form.type === 'FHIR' ? t('targets.fhirCreated') : t('targets.dbCreated')
    ElMessage.success(okMsg)
    await Promise.all([loadManage(), loadTargetTables()])
  } finally {
    creating.value = false
  }
}

async function handleTest(row) {
  testingId.value = row.id
  try {
    const data = await targetApi.test(row.id)
    ElMessage.success(t('targets.testOkDetail', { p: data?.product || 'JDBC' }))
  } catch {
    // 错误提示已由拦截器处理
  } finally {
    testingId.value = ''
    await loadManage()
  }
}

async function handleImport(row) {
  importingId.value = row.id
  try {
    const data = await targetApi.import(row.id)
    ElMessage.success(t('targets.reimportOk', { b: data?.bo_class || '' }))
  } catch {
    // 错误提示已由拦截器处理
  } finally {
    importingId.value = ''
    await Promise.all([loadManage(), loadTargetTables()])
  }
}

// ---- 选择目标表向导 ----
const wizardVisible = ref(false)
const currentId = ref('')
const currentName = ref('')
const wizardStep = ref(0)
const testResult = ref(null)
const schemaList = ref([])
const wizardSchema = ref('')
const wizardTables = ref([])
const checkedTables = ref([])
const savedResult = ref([])

function openWizard(row) {
  currentId.value = row.id
  currentName.value = row.name
  wizardStep.value = 0
  testResult.value = null
  schemaList.value = []
  wizardSchema.value = ''
  wizardTables.value = []
  checkedTables.value = []
  savedResult.value = []
  wizardVisible.value = true
}

async function doTest() {
  testingId.value = currentId.value
  try {
    const data = await targetApi.test(currentId.value)
    testResult.value = data?.ok === true
    if (testResult.value) {
      ElMessage.success(t('targets.testPassed'))
      wizardStep.value = 1
      const schemas = await targetApi.schemas(currentId.value)
      schemaList.value = schemas?.items || []
    } else {
      ElMessage.error(t('targets.connFail'))
    }
  } finally {
    testingId.value = ''
  }
}

async function loadWizardTables() {
  try {
    const data = await targetApi.tables(currentId.value, wizardSchema.value)
    wizardTables.value = data?.items || []
    wizardStep.value = 2
  } catch {
    // 错误提示已由拦截器处理
  }
}

function onTableSelect(rows) {
  checkedTables.value = rows
}

async function saveSelectedTables() {
  const selected = checkedTables.value.map((t) => ({
    schema: wizardSchema.value,
    table: t.table,
  }))
  try {
    const data = await targetApi.selectTables(currentId.value, selected)
    savedResult.value = data?.saved || []
    wizardStep.value = 3
    ElMessage.success(t('targets.saved', { n: data?.count ?? 0 }))
    await Promise.all([loadManage(), loadTargetTables()])
  } catch {
    // 错误提示已由拦截器处理
  }
}

function closeWizard() {
  wizardVisible.value = false
}

// ---- 查看表数据 ----
const dataVisible = ref(false)
const currentTable = ref('')
const rows = ref([])
const columns = ref([])

async function showData(table) {
  currentTable.value = table
  const data = await targetApi.data(table, 100)
  rows.value = data?.items || []
  columns.value = rows.value.length ? Object.keys(rows.value[0]) : []
  dataVisible.value = true
}

onMounted(async () => {
  await Promise.all([loadManage(), loadTargetTables()])
})
</script>

<style scoped>
.mb16 { margin-bottom: 16px; }
.mt12 { margin-top: 12px; }
.ml12 { margin-left: 12px; }
.mr6 { margin-right: 6px; }
.mb4 { margin-bottom: 4px; }
.center { text-align: center; padding: 16px 0; }
.card-title { font-weight: 600; }
</style>


