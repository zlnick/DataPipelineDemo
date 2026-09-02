<template>
  <div class="agents-page">
    <el-card shadow="never">
      <template #header><span class="card-title">{{ t('agents.title') }}</span></template>
      <el-alert :title="t('agents.alert')" type="info" :closable="false" show-icon class="mb16" />
      <el-row :gutter="16">
        <el-col v-for="a in agents" :key="a.id" :span="12" class="mb16">
          <el-card shadow="hover" class="agent-card">
            <div class="agent-head">
              <el-icon :size="26" color="#409EFF"><SetUp /></el-icon>
              <span class="agent-name">{{ a.name }}</span>
              <el-tag v-if="a.status === 'ready'" type="success" size="small">{{ t('agents.ready') }}</el-tag>
            </div>
            <p class="agent-role">{{ a.role }}</p>
            <p class="agent-purpose">{{ a.purpose }}</p>
            <el-descriptions :column="1" border size="small" class="mt12">
              <el-descriptions-item :label="t('agents.input')">{{ a.input }}</el-descriptions-item>
              <el-descriptions-item :label="t('agents.output')">{{ a.output }}</el-descriptions-item>
              <el-descriptions-item :label="t('agents.trigger')">{{ a.trigger }}</el-descriptions-item>
              <el-descriptions-item :label="t('agents.engine')">{{ a.engine }}</el-descriptions-item>
              <el-descriptions-item :label="t('agents.capabilities')">
                <el-tag v-for="c in a.capabilities" :key="c" size="small" class="mr6" type="info">{{ c }}</el-tag>
              </el-descriptions-item>
            </el-descriptions>
          </el-card>
        </el-col>
      </el-row>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { agentApi } from '../api/dataflow'

const { t } = useI18n()
const agents = ref([])

async function loadAgents() {
  const data = await agentApi.list()
  agents.value = data?.items || []
}

onMounted(loadAgents)
</script>

<style scoped>
.mb16 { margin-bottom: 16px; }
.mr6 { margin-right: 6px; }
.mt12 { margin-top: 12px; }
.card-title { font-weight: 600; }
.agent-card { height: 100%; }
.agent-head { display: flex; align-items: center; gap: 8px; }
.agent-name { font-size: 17px; font-weight: 600; }
.agent-role { color: #409EFF; margin: 8px 0 4px; }
.agent-purpose { color: #606266; margin: 0 0 8px; }
</style>
