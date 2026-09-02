<template>
  <div class="targets-page">
    <!-- ① 添加数据目标 -->
    <el-card shadow="never" class="mb16">
      <template #header><span class="card-title">添加数据目标</span></template>
      <el-form :inline="true" label-width="90px" @submit.prevent>
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：模拟远端数据库" style="width: 170px" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="form.type" style="width: 130px">
            <el-option
              v-for="opt in TARGET_TYPES"
              :key="opt.value"
              :label="opt.label + (opt.enabled ? '' : '（预留）')"
              :value="opt.value"
              :disabled="!opt.enabled"
            >
              <el-tooltip :content="opt.desc" placement="left" :disabled="opt.enabled">
                <span>{{ opt.label }}{{ opt.enabled ? '' : '（预留，暂未实现）' }}</span>
              </el-tooltip>
            </el-option>
          </el-select>
        </el-form-item>
        <!-- DB 目标：JDBC 连接 -->
        <el-form-item v-if="form.type === 'DB'" label="JDBC URL">
          <el-input v-model="form.jdbc_url" placeholder="jdbc:IRIS://iris:1972/USER" style="width: 320px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'DB'" label="驱动类">
          <el-input v-model="form.driver_class" placeholder="com.intersystems.jdbc.IRISDriver" style="width: 260px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'DB'" label="用户名">
          <el-input v-model="form.username" placeholder="superuser" style="width: 120px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'DB'" label="密码">
          <el-input v-model="form.password" type="password" placeholder="SYS" style="width: 120px" show-password />
        </el-form-item>
        <!-- SOAP 目标：WSDL 导入型 -->
        <el-form-item v-if="form.type === 'SOAP'" label="WSDL">
          <el-input v-model="form.wsdl" placeholder="WSDL 地址或文件路径" style="width: 320px" />
        </el-form-item>
        <el-form-item v-if="form.type === 'SOAP'" label="服务名">
          <el-input v-model="form.service" placeholder="PatientService" style="width: 180px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="creating" @click="handleCreate">添加数据目标</el-button>
        </el-form-item>
      </el-form>
      <el-alert
        :title="form.type === 'SOAP'
          ? '说明：SOAP 目标为「WSDL 导入型」——添加时自动读 WSDL 生成 Business Operation（BO），BO 作为管道投放组件，无需手写 SOAP 请求构造。'
          : '说明：数据库连接与目标发现（schema / 表 / 列）全程通过 JDBC 保障通用性；示例连接 IRIS 模拟远端库（jdbc:IRIS://iris:1972/USER）。'"
        type="info"
        :closable="false"
        show-icon
      />
    </el-card>

    <!-- ② 数据目标管理 -->
    <el-card shadow="never" class="mb16">
      <template #header>
        <span class="card-title">数据目标管理</span>
        <el-button size="small" style="float: right" @click="loadManage">刷新</el-button>
      </template>
      <el-table :data="managers" v-loading="manageLoading" border stripe>
        <el-table-column prop="name" label="名称" min-width="140" />
        <el-table-column prop="type" label="类型" width="80" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'analyzed' ? 'success' : row.status === 'connected' ? 'primary' : row.status === 'error' ? 'danger' : 'warning'">
              {{ row.status === 'analyzed' ? '已分析' : row.status === 'connected' ? '已连接' : row.status === 'error' ? '连接失败' : '已注册' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="已选表" width="80">
          <template #default="{ row }">
            <span v-if="row.type === 'SOAP'">—</span>
            <span v-else>{{ (row.tables || []).length }} 个</span>
          </template>
        </el-table-column>
        <el-table-column prop="connection" label="连接信息" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="row.type === 'SOAP'">
              <el-tag size="small" type="info">{{ row.connection?.service || 'default' }}</el-tag>
              <span class="ml6 gray">{{ row.connection?.wsdl }}</span>
            </template>
            <template v-else>{{ row.connection?.jdbc_url }}</template>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <template v-if="row.type === 'SOAP'">
              <el-button size="small" :loading="importingId === row.id" @click="handleImport(row)">重新导入 WSDL</el-button>
            </template>
            <template v-else>
              <el-button size="small" :loading="testingId === row.id" @click="handleTest(row)">联通测试</el-button>
              <el-button size="small" type="primary" @click="openWizard(row)">选择目标表</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ③ 已选目标表 -->
    <el-card shadow="never">
      <template #header>
        <span class="card-title">已选目标表（供 AI 匹配与管道投放）</span>
        <el-button size="small" style="float: right" @click="loadTargetTables">刷新</el-button>
      </template>
      <el-table :data="targetTables" v-loading="tablesLoading" border stripe>
        <el-table-column prop="target_name" label="所属目标" min-width="130" />
        <el-table-column prop="table" label="表名" min-width="130">
          <template #default="{ row }"><el-tag type="primary">{{ row.table }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="schema" label="Schema" min-width="100" />
        <el-table-column label="列结构" min-width="300">
          <template #default="{ row }">
            <el-tag v-for="c in row.columns" :key="c" size="small" class="mr6 mb4">{{ c }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="count" label="行数" width="80" />
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.type !== 'SOAP'" size="small" type="primary" plain @click="showData(row.table)">查看数据</el-button>
            <el-tag v-else size="small" type="info">SOAP 实体</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 选择目标表向导 -->
    <el-dialog v-model="wizardVisible" :title="`选择目标表 — ${currentName}`" width="720px">
      <el-steps :active="wizardStep" finish-status="success" align-center class="mb16">
        <el-step title="联通测试" />
        <el-step title="选择 Schema" />
        <el-step title="选择目标表" />
        <el-step title="分析列并保存" />
      </el-steps>

      <div v-if="wizardStep === 0" class="center">
        <el-button type="primary" :loading="testingId === currentId" @click="doTest">执行联通测试</el-button>
        <el-tag v-if="testResult !== null" :type="testResult ? 'success' : 'danger'" class="ml12">
          {{ testResult ? '连接成功' : '连接失败' }}
        </el-tag>
      </div>

      <div v-else-if="wizardStep === 1">
        <el-alert class="mb12" title="选择要分析的数据库 schema（如 SQLUser）" type="info" :closable="false" />
        <el-select v-model="wizardSchema" placeholder="请选择 schema" filterable style="width: 100%">
          <el-option v-for="s in schemaList" :key="s" :label="s" :value="s" />
        </el-select>
        <div class="mt12">
          <el-button type="primary" :disabled="!wizardSchema" @click="loadWizardTables">下一步：列出表</el-button>
        </div>
      </div>

      <div v-else-if="wizardStep === 2">
        <el-alert class="mb12" title="勾选要作为数据目标的表" type="info" :closable="false" />
        <el-table :data="wizardTables" border max-height="300" @selection-change="onTableSelect">
          <el-table-column type="selection" width="50" />
          <el-table-column prop="table" label="表名" min-width="160" />
          <el-table-column prop="type" label="类型" width="90" />
        </el-table>
        <div class="mt12">
          <el-button @click="wizardStep = 1">上一步</el-button>
          <el-button type="primary" :disabled="!checkedTables.length" @click="saveSelectedTables">
            保存所选表并分析列
          </el-button>
        </div>
      </div>

      <div v-else>
        <el-alert title="目标表已保存并完成列结构分析，可在下方「已选目标表」查看。" type="success" :closable="false" />
        <div v-for="t in savedResult" :key="t.table" class="mt12">
          <b>{{ t.table }}：</b>
          <el-tag v-for="c in t.columns" :key="c.name" size="small" class="mr6">{{ c.name }}</el-tag>
        </div>
        <div class="mt12"><el-button type="primary" @click="closeWizard">完成</el-button></div>
      </div>
    </el-dialog>

    <!-- 表数据对话框 -->
    <el-dialog v-model="dataVisible" :title="`${currentTable} 表数据`" width="860px">
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
      <div class="mt12">共 {{ rows.length }} 行</div>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { TARGET_TYPES } from '../api/constants'
import { targetApi } from '../api/dataflow'

const managers = ref([])
const targetTables = ref([])
const manageLoading = ref(false)
const tablesLoading = ref(false)
const creating = ref(false)
const testingId = ref('')
const importingId = ref('')

const form = reactive({
  name: '模拟远端数据库',
  type: 'DB',
  jdbc_url: 'jdbc:IRIS://iris:1972/USER',
  driver_class: 'com.intersystems.jdbc.IRISDriver',
  username: 'superuser',
  password: 'SYS',
  // SOAP 目标配置（WSDL 导入型）
  wsdl: '/tmp/patient.wsdl',
  service: 'PatientService',
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
    ElMessage.warning('请填写目标名称')
    return
  }
  if (form.type === 'DB' && !form.jdbc_url.trim()) {
    ElMessage.warning('请填写 JDBC URL')
    return
  }
  if (form.type === 'SOAP' && !form.wsdl.trim()) {
    ElMessage.warning('SOAP 目标需要提供 WSDL 地址')
    return
  }
  creating.value = true
  try {
    const connection = form.type === 'SOAP'
      ? { wsdl: form.wsdl.trim(), service: form.service.trim() || 'default' }
      : {
          jdbc_url: form.jdbc_url.trim(),
          driver_class: form.driver_class.trim(),
          username: form.username,
          password: form.password,
        }
    await targetApi.create({ name: form.name.trim(), type: form.type, connection })
    ElMessage.success(form.type === 'SOAP' ? 'SOAP 目标添加成功（WSDL 已导入生成 BO 及实体分析）' : '数据目标添加成功')
    await Promise.all([loadManage(), loadTargetTables()])
  } finally {
    creating.value = false
  }
}

async function handleTest(row) {
  testingId.value = row.id
  try {
    const data = await targetApi.test(row.id)
    ElMessage.success(`连接成功（${data?.product || 'JDBC'}）`)
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
    ElMessage.success(`WSDL 重新导入成功（BO: ${data?.bo_class || ''}）`)
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
      ElMessage.success('联通测试通过')
      wizardStep.value = 1
      const schemas = await targetApi.schemas(currentId.value)
      schemaList.value = schemas?.items || []
    } else {
      ElMessage.error('连接失败')
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
    ElMessage.success(`已保存 ${data?.count ?? 0} 个目标表并完成列分析`)
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


