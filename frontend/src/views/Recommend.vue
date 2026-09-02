<template>
  <div class="recommend-page">
    <!-- 输入区：选择资产 + 目标表 -->
    <el-card shadow="never" class="mb16">
      <template #header><span class="card-title">选择数据资产与转换目标</span></template>
      <el-form :inline="true" label-width="100px">
        <el-form-item label="数据资产（多选）">
          <el-select v-model="selectedAssets" multiple filterable style="width: 400px" placeholder="选择要转换的数据资产">
            <el-option
              v-for="a in allAssets"
              :key="a.id"
              :value="a.id"
            >
              <span>{{ a.type === 'SQL_TABLE' ? '[SQL] ' : '' }}{{ a.name }}</span>
              <span class="gray" style="float: right; font-size: 12px">
                {{ a.type === 'SQL_TABLE' ? (a.fields || []).length + ' 列' : 'FHIR' }}
              </span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="转换目标">
          <el-tag v-for="t in targets" :key="t.table" class="mr6" type="success">{{ t.table }}</el-tag>
          <span v-if="!targets.length" class="gray">无目标表</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="recommending" @click="handleRecommend">
            <el-icon class="mr4"><MagicStick /></el-icon>AI 智能匹配
          </el-button>
        </el-form-item>
      </el-form>
      <el-alert
        title="AI 将根据资产字段语义与目标表列结构，推荐「资产 → 目标表」匹配并生成字段级映射。需在 .env 中配置 LLM_API_KEY（OpenAI 兼容服务）。"
        type="warning"
        :closable="false"
        show-icon
      />
    </el-card>

    <!-- 验证报告（AI 推荐后的转换关系验证状态） -->
    <el-card shadow="never" v-if="validation" class="mb16">
      <template #header><span class="card-title">转换关系验证报告</span></template>
      <el-alert :title="validationTitle" :type="validationType" :closable="false" show-icon />
      <el-table
        v-if="validation.issues && validation.issues.length"
        :data="validation.issues"
        border
        size="small"
        class="mt8"
      >
        <el-table-column label="检查项" width="130">
          <template #default="{ row }">
            <el-tag size="small">{{ CHECK_NAMES[row.check] || row.check }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.severity === 'error' ? 'danger' : 'warning'">
              {{ row.severity === 'error' ? '错误' : '警告' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="item" label="对象" width="170" />
        <el-table-column prop="message" label="说明" min-width="240" />
      </el-table>
    </el-card>

    <!-- 推荐结果 -->
    <el-card shadow="never" v-if="recommendations.length">
      <template #header>
        <span class="card-title">AI 推荐结果（{{ recommendations.length }} 条）</span>
        <el-button size="small" type="success" style="float: right" @click="confirmAll">
          全部确认并保存转换关系
        </el-button>
      </template>

      <div v-for="rec in recommendations" :key="rec.asset + rec.target_table" class="rec-item">
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item label="源资产">
            <el-tag type="success">{{ rec.asset }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="目标表">
            <el-tag type="primary">{{ rec.target_table }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="置信度">
            <el-progress :percentage="Math.round((rec.confidence || 0) * 100)" :stroke-width="10" style="width: 120px" />
          </el-descriptions-item>
          <el-descriptions-item label="理由">{{ rec.reason }}</el-descriptions-item>
        </el-descriptions>

        <div class="mt8 mb8"><b>字段映射：</b></div>
        <el-table :data="rec.field_mappings" border size="small" class="fm-table">
          <el-table-column prop="source" label="源字段（FHIRPath）" min-width="220" />
          <el-table-column prop="target" label="目标列" min-width="140" />
          <el-table-column prop="transform" label="转换" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.transform" size="small" type="warning">{{ row.transform }}</el-tag>
              <span v-else class="gray">—</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <el-card shadow="never" v-else>
      <el-empty description="尚未进行 AI 匹配，请选择资产后点击「AI 智能匹配」" />
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { aiApi, datasourceApi, mappingApi, modelApi, targetApi } from '../api/dataflow'

const route = useRoute()
const router = useRouter()

const allAssets = ref([])
const selectedAssets = ref([])
const assetFields = ref({})
const targets = ref([])
const recommending = ref(false)
const recommendations = ref([])
const transformationPlan = ref(null)
const validation = ref(null)

// 检查项名称映射（后端 check 字段 → 中文）
const CHECK_NAMES = {
  recommendations: '映射校验',
  source_fields: '源字段检查',
}

// 验证报告标题（按状态/错误数生成）
const validationTitle = computed(() => {
  const v = validation.value
  if (!v) return ''
  const warns = v.warning_count || 0
  if (!v.ok) {
    return warns
      ? `验证发现 ${v.error_count} 个错误、${warns} 个警告`
      : `验证发现 ${v.error_count} 个错误`
  }
  return warns ? `验证通过（${warns} 个警告）` : '转换关系验证通过'
})

// 验证报告 alert 类型：有错误 error / 有警告 warning / 全部通过 success
const validationType = computed(() => {
  const v = validation.value
  if (!v) return 'success'
  if (!v.ok) return 'error'
  return v.warning_count ? 'warning' : 'success'
})

// 资产字段结构（供 AI 推荐使用）：从资产名推断典型字段
const ASSET_FIELDS = {
  Patient: ['id', 'name[0].family', 'name[0].given[0]', 'gender', 'birthDate', 'telecom[0].value', 'address[0].line[0]', 'address[0].city'],
  Observation: ['id', 'subject.reference', 'code.coding[0].code', 'code.coding[0].display', 'valueQuantity.value', 'valueQuantity.unit', 'effectiveDateTime', 'status'],
}

function buildRecommendPayload() {
  const selected = allAssets.value.filter((a) => selectedAssets.value.includes(a.id))
  const assets = selected.map((a) => ({
    id: a.id,
    name: a.name,
    type: a.type,
    source_id: a.source_id,
    // 优先使用资产真实字段（SQL 表资产=列名；FHIR 资产无 fields 时前端典型字段兜底）
    fields: a.fields || assetFields.value[a.name] || ASSET_FIELDS[a.name] || [a.name, 'id'],
    structure: a.structure || null,
  }))
  const targetList = targets.value.map((t) => ({
    table: t.table,
    columns: t.columns,
  }))
  const targetModels = targets.value.map((t) => ({
    id: t.target_id,
    target_id: t.target_id,
    name: t.target_name || t.table,
    type: t.type || 'DB',
    table: t.table,
    schema: t.schema,
    columns: t.columns || [],
    config: t.type === 'SOAP'
      ? { service: t.table, bo_class: t.bo_class }
      : {},
  }))
  return {
    assets,
    targets: targetList,
    source_models: assets,
    target_models: targetModels,
  }
}

async function loadAssets() {
  const datasources = (await datasourceApi.list())?.items || []
  const all = []
  for (const ds of datasources) {
    const data = await datasourceApi.assets(ds.id)
    all.push(...(data?.items || []))
  }
  allAssets.value = all
  // 构建资产名 → 真实字段映射（SQL_TABLE 资产自带 fields=列名）
  const fieldsMap = {}
  for (const a of all) {
    if (Array.isArray(a.fields) && a.fields.length) {
      fieldsMap[a.name] = a.fields
    }
  }
  assetFields.value = fieldsMap
}

async function loadTargets() {
  const data = await targetApi.list()
  targets.value = data?.items || []
}

async function handleRecommend() {
  if (!selectedAssets.value.length) {
    ElMessage.warning('请先选择数据资产')
    return
  }
  if (!targets.value.length) {
    ElMessage.warning('无可用目标表')
    return
  }
  recommending.value = true
  try {
    const data = await aiApi.recommend(buildRecommendPayload())
    recommendations.value = data?.recommendations || []
    transformationPlan.value = data?.transformation_plan || null
    validation.value = data?.validation || null
    ElMessage.success(`AI 推荐完成，共 ${recommendations.value.length} 条建议`)
    if (validation.value && !validation.value.ok) {
      ElMessage.warning(`转换验证发现 ${validation.value.error_count} 个错误，请检查后确认`)
    }
  } finally {
    recommending.value = false
  }
}

async function confirmAll() {
  if (!recommendations.value.length) return
  // 注意：Date.now() 在同一毫秒内多次调用返回相同值，若直接作为 id 会导致
  // 多条映射 id 相同（^demo.Mapping 以 id 为键），后保存的覆盖先保存的。
  // 因此用时间戳 + 序号拼接生成唯一 id。
  const stamp = Date.now() % 100000
  const mappings = recommendations.value.map((rec, idx) => ({
    id: `M${stamp}${idx}`,
    source: rec.asset,
    target_table: rec.target_table,
    field_mappings: (rec.field_mappings || []).map((fm) => ({
      source: fm.source,
      target: fm.target,
      transform: fm.transform || null,
    })),
  }))
  const plan = {
    ...(transformationPlan.value || {}),
    source_models: buildRecommendPayload().source_models,
    target_models: buildRecommendPayload().target_models,
    mappings,
    status: 'confirmed',
  }
  await modelApi.createPlan(plan)
  await mappingApi.save(mappings)
  ElMessage.success('转换关系已保存，可前往「转换关系」查看或「管道监控」生成管道')
  router.push('/mappings')
}

onMounted(async () => {
  await Promise.all([loadAssets(), loadTargets()])
  // 支持从资产页带 asset 参数进入
  if (route.query.asset && !selectedAssets.value.includes(route.query.asset)) {
    const match = allAssets.value.find((a) => a.name === route.query.asset)
    if (match) selectedAssets.value = [match.id]
  }
})
</script>

<style scoped>
.mb16 { margin-bottom: 16px; }
.mr6 { margin-right: 6px; }
.mr4 { margin-right: 4px; }
.mt8 { margin-top: 8px; }
.mb8 { margin-bottom: 8px; }
.rec-item { margin-bottom: 16px; padding: 12px; border: 1px solid #ebeef5; border-radius: 6px; }
.fm-table { margin-bottom: 8px; }
.gray { color: #909399; }
.card-title { font-weight: 600; }
</style>
