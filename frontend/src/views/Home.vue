<template>
  <div class="home-page">
    <el-card shadow="never" class="intro-card">
      <template #header>
        <span class="card-title">{{ t('home.title') }}</span>
      </template>
      <h2>{{ t('home.subtitle') }}</h2>
      <p class="intro-text">
        {{ t('home.intro1') }}
        {{ t('home.intro2') }}
        {{ t('home.intro3') }}
      </p>

      <el-steps :active="4" align-center class="flow-steps">
        <el-step :title="t('home.step1')" :description="t('home.step1Desc')" />
        <el-step :title="t('home.step2')" :description="t('home.step2Desc')" />
        <el-step :title="t('home.step3')" :description="t('home.step3Desc')" />
        <el-step :title="t('home.step4')" :description="t('home.step4Desc')" />
        <el-step :title="t('home.step5')" :description="t('home.step5Desc')" />
      </el-steps>
    </el-card>

    <!-- 关键状态 -->
    <el-row :gutter="16" class="status-row">
      <el-col :span="4"><el-card shadow="never">
        <template #header><span class="card-title">{{ t('home.irisConn') }}</span></template>
        <el-tag :type="irisOk ? 'success' : 'danger'">{{ irisOk ? t('home.connected') : t('home.disconnected') }}</el-tag>
      </el-card></el-col>
      <el-col :span="4"><el-card shadow="never">
        <template #header><span class="card-title">{{ t('home.datasources') }}</span></template>
        <b>{{ stat.datasources }}</b> {{ t('home.items') }}
      </el-card></el-col>
      <el-col :span="4"><el-card shadow="never">
        <template #header><span class="card-title">{{ t('home.assets') }}</span></template>
        <b>{{ stat.assets }}</b> {{ t('home.items') }}
      </el-card></el-col>
      <el-col :span="4"><el-card shadow="never">
        <template #header><span class="card-title">{{ t('home.mappings') }}</span></template>
        <b>{{ stat.mappings }}</b> {{ t('home.items') }}
      </el-card></el-col>
      <el-col :span="4"><el-card shadow="never">
        <template #header><span class="card-title">{{ t('home.pipeline') }}</span></template>
        <el-tag :type="stat.running ? 'success' : 'info'">{{ stat.running ? t('home.running') : t('home.stopped') }}</el-tag>
      </el-card></el-col>
      <el-col :span="4"><el-card shadow="never">
        <template #header><span class="card-title">{{ t('home.targetRows') }}</span></template>
        <b>{{ stat.targetRows }}</b> {{ t('home.rows') }}
      </el-card></el-col>
    </el-row>

    <el-card shadow="never" class="mt16">
      <template #header><span class="card-title">{{ t('home.workflow') }}</span></template>
      <el-timeline>
        <el-timeline-item :timestamp="t('home.tl1')" type="primary">
          {{ t('home.tl1Text') }}
        </el-timeline-item>
        <el-timeline-item :timestamp="t('home.tl2')" type="success">
          {{ t('home.tl2Text') }}
        </el-timeline-item>
        <el-timeline-item :timestamp="t('home.tl3')" type="warning">
          {{ t('home.tl3Text') }}
        </el-timeline-item>
        <el-timeline-item :timestamp="t('home.tl4')" type="danger">
          {{ t('home.tl4Text') }}
        </el-timeline-item>
        <el-timeline-item :timestamp="t('home.tl5')" type="success">
          {{ t('home.tl5Text') }}
        </el-timeline-item>
      </el-timeline>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import http from '../api'
import { datasourceApi, mappingApi, pipelineApi, targetApi } from '../api/dataflow'

const { t } = useI18n()

const irisOk = ref(false)
const stat = reactive({ datasources: 0, assets: 0, mappings: 0, running: false, targetRows: 0 })

onMounted(async () => {
  try {
    const data = await http.get('/health')
    irisOk.value = data?.iris === 'connected'
  } catch {
    irisOk.value = false
  }
  try {
    const [ds, assets, mappings, targets] = await Promise.all([
      datasourceApi.list(),
      http.get('/datasources').then(() => datasourceApi.list()),
      mappingApi.list(),
      targetApi.list(),
    ])
    stat.datasources = ds?.items?.length || 0
    // 汇总所有数据源的资产
    let assetCount = 0
    for (const d of ds?.items || []) {
      const a = await datasourceApi.assets(d.id)
      assetCount += a?.items?.length || 0
    }
    stat.assets = assetCount
    stat.mappings = mappings?.items?.length || 0
    stat.targetRows = (targets?.items || []).reduce((sum, t) => sum + (t.count || 0), 0)
    try {
      const st = await pipelineApi.status()
      stat.running = st?.running === true
    } catch {
      stat.running = false
    }
  } catch {
    // 忽略统计加载失败
  }
})
</script>

<style scoped>
.intro-card { margin-bottom: 16px; }
.card-title { font-weight: 600; }
.intro-text { color: #606266; line-height: 1.8; }
.flow-steps { margin-top: 24px; }
.status-row { margin-top: 16px; }
.mt16 { margin-top: 16px; }
</style>
