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
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { mappingApi } from '../api/dataflow'

const router = useRouter()
const { t } = useI18n()
const mappings = ref([])
const loading = ref(false)

async function loadMappings() {
  loading.value = true
  try {
    const data = await mappingApi.list()
    mappings.value = data?.items || []
  } finally {
    loading.value = false
  }
}

function goPipeline() {
  router.push('/pipelines')
}

onMounted(loadMappings)
</script>

<style scoped>
.mb12 { margin-bottom: 12px; }
.mt12 { margin-top: 12px; }
.fm-table { margin: 8px 0; }
.gray { color: #909399; }
.card-title { font-weight: 600; }
</style>
