<template>
  <div class="agents-page">
    <el-card shadow="never">
      <template #header><span class="card-title">{{ t('agents.title') }}</span></template>
      <el-alert :title="t('agents.alert')" type="info" :closable="false" show-icon class="mb16" />

      <!-- Skill 区：单轮 LLM 技能（职责单一、按需调用） -->
      <template v-if="skillAgents.length">
        <div class="section-title">{{ t('agents.sectionSkills') }}</div>
        <el-row :gutter="16" class="mb16">
          <el-col v-for="a in skillAgents" :key="a.id" :span="12" class="mb16">
            <el-card shadow="hover" class="agent-card">
              <div class="agent-head">
                <el-icon :size="26" color="#409EFF"><SetUp /></el-icon>
                <span class="agent-name">{{ a.name }}</span>
                <el-tag v-if="a.kind === 'skill'" type="primary" size="small">
                  {{ t('agents.skill') }}
                </el-tag>
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
      </template>

      <!-- Agent 区：验证-修复循环（事实工具 + LLM ≤2 轮 + 经验沉淀） -->
      <template v-if="agentAgents.length">
        <div class="section-title section-title-warn">{{ t('agents.sectionAgents') }}</div>
        <el-row :gutter="16">
          <el-col v-for="a in agentAgents" :key="a.id" :span="12" class="mb16">
            <el-card shadow="hover" class="agent-card">
              <div class="agent-head">
                <el-icon :size="26" color="#E6A23C"><SetUp /></el-icon>
                <span class="agent-name">{{ a.name }}</span>
                <el-tag v-if="a.kind === 'agent'" type="warning" size="small">
                  {{ t('agents.agent') }}
                </el-tag>
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
      </template>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { agentApi } from '../api/dataflow'

const { t } = useI18n()
const agents = ref([])

// 按 kind 分组：Skill（单轮 LLM 技能）区 与 Agent（工具+修复循环）区
const skillAgents = computed(() => agents.value.filter((a) => a.kind === 'skill'))
const agentAgents = computed(() => agents.value.filter((a) => a.kind === 'agent'))

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
.section-title {
  font-weight: 600;
  font-size: 15px;
  margin: 4px 0 12px;
  padding-left: 10px;
  border-left: 3px solid #409EFF;
  color: #303133;
}
.section-title-warn { border-left-color: #E6A23C; }
.section-title + .el-row .el-col:last-child { margin-bottom: 0; }
.agent-card { height: 100%; }
.agent-head { display: flex; align-items: center; gap: 8px; }
.agent-name { font-size: 17px; font-weight: 600; }
.agent-role { color: #409EFF; margin: 8px 0 4px; }
.agent-purpose { color: #606266; margin: 0 0 8px; }
</style>
