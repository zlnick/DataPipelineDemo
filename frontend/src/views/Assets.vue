<template>
  <div class="assets-page">
    <el-card shadow="never">
      <template #header>
        <span class="card-title">{{ t('assets.title') }}</span>
        <el-select
          v-model="sourceId"
          :placeholder="t('assets.allSources')"
          clearable
          style="width: 240px; float: right"
          @change="loadAssets"
        >
          <el-option
            v-for="ds in datasources"
            :key="ds.id"
            :label="ds.name"
            :value="ds.id"
          />
        </el-select>
      </template>

      <el-alert
        class="mb12"
        :title="t('assets.desc')"
        type="info"
        :closable="false"
        show-icon
      />

      <el-input
        v-model="keyword"
        :placeholder="t('assets.searchPlaceholder')"
        clearable
        class="mb12"
        style="max-width: 360px"
      />

      <el-table :data="filtered" v-loading="loading" border stripe>
        <el-table-column prop="id" :label="t('assets.idCol')" width="130" />
        <el-table-column prop="name" :label="t('assets.resourceType')" min-width="160">
          <template #default="{ row }">
            <el-tag type="success">{{ row.name }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source_name" :label="t('assets.sourceDs')" min-width="120" />
        <el-table-column prop="type" :label="t('assets.typeCol')" width="140" />
        <el-table-column :label="t('assets.actions')" width="140">
          <template #default="{ row }">
            <el-button size="small" type="primary" plain @click="selectForAI(row)">
              {{ t('assets.selectMatch') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="mt12">
        {{ t('assets.count', { n: filtered.length }) }}
        <el-button type="primary" class="ml12" @click="goRecommend">
          {{ t('assets.goAI') }}
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { datasourceApi } from '../api/dataflow'
import { localePath } from '../i18n/path'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const datasources = ref([])
const sourceId = ref(route.query.source_id || '')
const assets = ref([])
const loading = ref(false)
const keyword = ref('')

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return assets.value
  return assets.value.filter((a) => a.name.toLowerCase().includes(kw))
})

async function loadDatasources() {
  const data = await datasourceApi.list()
  datasources.value = data?.items || []
}

async function loadAssets() {
  loading.value = true
  try {
    if (sourceId.value) {
      const data = await datasourceApi.assets(sourceId.value)
      assets.value = data?.items || []
    } else {
      // 汇总全部数据源的资产
      const all = []
      for (const ds of datasources.value) {
        const data = await datasourceApi.assets(ds.id)
        all.push(...(data?.items || []))
      }
      assets.value = all
    }
  } finally {
    loading.value = false
  }
}

function selectForAI(row) {
  router.push({ path: localePath('/recommend'), query: { asset: row.name } })
  ElMessage.success(t('assets.selected', { name: row.name }))
}

function goRecommend() {
  router.push(localePath('/recommend'))
}

watch(sourceId, loadAssets)

onMounted(async () => {
  await loadDatasources()
  await loadAssets()
})
</script>

<style scoped>
.mb12 { margin-bottom: 12px; }
.mt12 { margin-top: 12px; }
.ml12 { margin-left: 12px; }
.card-title { font-weight: 600; }
</style>
