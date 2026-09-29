<template>
  <div class="mappings-page">
    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('mappings.title') }}</span>
        <el-button size="small" style="float: right" @click="loadMappings">{{ t('mappings.refresh') }}</el-button>
      </template>

      <el-alert
        class="mb12"
        :title="t('mappings.alert')"
        type="info"
        :closable="false"
        show-icon
      />

      <el-table :data="mappings" v-loading="loading" border stripe>
        <el-table-column prop="id" :label="t('mappings.id')" width="100" />
        <el-table-column prop="source" :label="t('mappings.sourceAsset')" min-width="120">
          <template #default="{ row }">
            <el-tag type="success">{{ row.source }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="target_table" :label="t('mappings.targetTable')" min-width="120">
          <template #default="{ row }">
            <el-tag type="primary">{{ row.target_table }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" :label="t('mappings.status')" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'confirmed' ? 'success' : 'warning'">
              {{ row.status === 'confirmed' ? t('mappings.confirmed') : t('mappings.pending') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('mappings.fieldMapping')" min-width="120">
          <template #default="{ row }">
            {{ (row.field_mappings || []).length }} {{ t('mappings.fields') }}
          </template>
        </el-table-column>
        <el-table-column type="expand" width="50">
          <template #default="{ row }">
            <el-table :data="row.field_mappings || []" border size="small" class="fm-table">
              <el-table-column prop="source" :label="t('mappings.sourceField')" min-width="220" />
              <el-table-column prop="target" :label="t('mappings.targetCol')" min-width="140" />
              <el-table-column prop="transform" :label="t('mappings.transform')" width="100">
                <template #default="{ row: fm }">
                  <el-tag v-if="fm.transform" size="small" type="warning">{{ fm.transform }}</el-tag>
                  <span v-else class="gray">—</span>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </el-table-column>
      </el-table>

      <div class="mt12">
        <el-button type="primary" @click="goPipeline">{{ t('mappings.goPipeline') }}</el-button>
      </div>
    </el-card>

    <!-- 术语映射（术语服务器 = 唯一事实源；运行期由共享 BO 实时查询） -->
    <el-card shadow="never" class="mt12">
      <template #header>
        <span class="card-title">{{ t('mappings.termTitle') }}</span>
        <el-button size="small" style="float: right" :loading="termLoading" @click="loadTermCatalog">
          {{ t('mappings.termRefresh') }}
        </el-button>
      </template>

      <el-alert class="mb12" :title="t('mappings.termAlert')" type="info" :closable="false" show-icon />

      <el-descriptions :column="3" border size="small">
        <el-descriptions-item :label="t('mappings.termServer')">
          <span class="mono">{{ termCatalog.catalog.url || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item :label="t('mappings.termGate')">
          <el-tag v-if="termCatalog.gate.skipped" type="info">{{ t('mappings.termNoJob') }}</el-tag>
          <el-tag v-else-if="termCatalog.gate.ok" type="success">{{ t('mappings.termOk') }}</el-tag>
          <el-tag v-else type="warning">{{ t('mappings.termDegrade') }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item :label="t('mappings.termSourceNs')">
          <span class="mono">{{ Object.keys(termCatalog.gate.namespaces || {}).join(', ') || '—' }}</span>
        </el-descriptions-item>
      </el-descriptions>

      <el-table :data="termCatalog.catalog.pairs || []" border size="small" class="mt12">
        <el-table-column prop="sourceSystem" :label="t('mappings.termSourceSystem')" min-width="240">
          <template #default="{ row }"><span class="mono">{{ row.sourceSystem }}</span></template>
        </el-table-column>
        <el-table-column prop="targetSystem" :label="t('mappings.termTargetSystem')" min-width="220">
          <template #default="{ row }"><span class="mono">{{ row.targetSystem }}</span></template>
        </el-table-column>
        <el-table-column prop="entries" :label="t('mappings.termEntries')" width="90" />
        <el-table-column prop="active" :label="t('mappings.termActive')" width="90" />
        <el-table-column prop="negative" :label="t('mappings.termNegative')" width="90" />
        <el-table-column prop="pending" :label="t('mappings.termPending')" width="90" />
      </el-table>

      <div class="mt12 term-jobs">
        <div v-if="(termCatalog.gate.jobs || []).length">
          <el-tag size="small" type="info" class="mr6" v-for="j in termCatalog.gate.jobs" :key="j">{{ j }}</el-tag>
          <span class="gray">
            {{ t('mappings.termCovered') }} {{ termCatalog.gate.covered }} ·
            {{ t('mappings.termNegativeShort') }} {{ termCatalog.gate.negative }}
          </span>
        </div>
        <div v-if="(termCatalog.gate.todo || termCatalog.gate.missing || []).length">
          <el-alert
            class="mt12"
            type="warning"
            :closable="false"
            show-icon
            :title="t('mappings.termTodoTitle', { n: (termCatalog.gate.todo || termCatalog.gate.missing).length })"
            :description="`${(termCatalog.gate.todo || termCatalog.gate.missing).join(t('common.listSep'))}\n${termCatalog.hint}`"
          />
        </div>
        <div v-if="(termCatalog.gate.unresolved || []).length">
          <el-alert
            class="mt12"
            type="warning"
            :closable="false"
            show-icon
            :title="t('mappings.termUnresolvedTitle')"
            :description="termCatalog.gate.unresolved.join(t('common.listSep'))"
          />
        </div>
        <div v-if="termCatalog.gate.error">
          <el-alert class="mt12" type="warning" :closable="false" show-icon :title="termCatalog.gate.error" />
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { mappingApi } from '../api/dataflow'

const router = useRouter()
const { t } = useI18n()
const mappings = ref([])
const loading = ref(false)

// 术语映射（术语服务器 = 唯一事实源）：目录 + 只读盘点摘要（运行期由共享 BO 实时查询）
const termLoading = ref(false)
const termCatalog = reactive({
  catalog: { url: '', pairs: [], ok: false, error: '' },
  gate: { ok: false, skipped: '', jobs: [], namespaces: {}, covered: 0, negative: 0,
          missing: [], todo: [], pending: [], unresolved: [], error: '' },
  hint: '',
  runtime: '',
})

async function loadMappings() {
  loading.value = true
  try {
    const data = await mappingApi.list()
    mappings.value = data?.items || []
  } finally {
    loading.value = false
  }
}

async function loadTermCatalog() {
  termLoading.value = true
  try {
    const data = await mappingApi.termCatalog()
    if (data?.catalog) termCatalog.catalog = data.catalog
    if (data?.gate) termCatalog.gate = data.gate
    termCatalog.hint = data?.hint || ''
    termCatalog.runtime = data?.runtime || ''
  } catch (e) {
    ElMessage.error(t('mappings.termLoadFailed'))
  } finally {
    termLoading.value = false
  }
}

function goPipeline() {
  router.push('/pipelines')
}

onMounted(() => {
  loadMappings()
  loadTermCatalog()
})
</script>

<style scoped>
.mb12 { margin-bottom: 12px; }
.mt12 { margin-top: 12px; }
.mr6 { margin-right: 6px; }
.fm-table { margin: 8px 0; }
.gray { color: #909399; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.card-title { font-weight: 600; }
.term-jobs { line-height: 24px; }
</style>
