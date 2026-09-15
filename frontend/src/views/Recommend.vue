<template>
  <div class="recommend-page">
    <!-- 输入区：选择资产 + 目标表 -->
    <el-card shadow="never" class="mb16">
      <template #header><span class="card-title">{{ t('recommend.title') }}</span></template>
      <el-form :inline="true" label-width="100px">
        <el-form-item :label="t('recommend.assetMulti')">
          <el-select v-model="selectedAssets" multiple filterable style="width: 400px" :placeholder="t('recommend.assetPlaceholder')">
            <el-option
              v-for="a in allAssets"
              :key="a.id"
              :value="a.id"
            >
              <span>{{ a.type === 'SQL_TABLE' ? '[SQL] ' : '' }}{{ a.name }}</span>
              <span class="gray" style="float: right; font-size: 12px">
                {{ a.type === 'SQL_TABLE' ? (a.fields || []).length + t('recommend.colUnit') : 'FHIR' }}
              </span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item :label="t('recommend.targetMulti')">
          <el-select
            v-model="selectedTargets"
            multiple
            filterable
            style="width: 400px"
            :placeholder="t('recommend.targetPlaceholder')"
          >
            <el-option-group v-for="g in targetGroups" :key="g.label" :label="g.label">
              <el-option
                v-for="t in g.items"
                :key="(t.target_id || '') + '::' + t.table"
                :value="(t.target_id || '') + '::' + t.table"
              >
                <span>{{ targetPrefix(t) }}{{ t.table }}</span>
                <span class="gray" style="float: right; font-size: 12px">
                  {{ targetSuffix(t) }}
                </span>
              </el-option>
            </el-option-group>
          </el-select>
          <el-tooltip v-if="targets.some((x) => x.type === 'FHIR' && !!x.open)" :content="t('recommend.openTooltip')" placement="top">
            <span class="ml6 gray sm">{{ t('recommend.openLegend') }}</span>
          </el-tooltip>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="recommending" @click="handleRecommend">
            <el-icon class="mr4"><MagicStick /></el-icon>{{ t('recommend.aiMatch') }}
          </el-button>
        </el-form-item>
      </el-form>
      <el-alert :title="t('recommend.alert')" type="warning" :closable="false" show-icon />
    </el-card>

    <!-- 验证报告（AI 推荐后的转换关系验证状态） -->
    <el-card shadow="never" v-if="validation" class="mb16">
      <template #header><span class="card-title">{{ t('recommend.validationTitle') }}</span></template>
      <el-alert :title="validationTitle" :type="validationType" :closable="false" show-icon />
      <el-table
        v-if="validation.issues && validation.issues.length"
        :data="validation.issues"
        border
        size="small"
        class="mt8"
      >
        <el-table-column :label="t('recommend.validationCheck')" width="130">
          <template #default="{ row }">
            <el-tag size="small">{{ CHECK_NAMES[row.check] || row.check }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('recommend.validationSeverity')" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.severity === 'error' ? 'danger' : 'warning'">
              {{ row.severity === 'error' ? t('recommend.severityError') : t('recommend.severityWarning') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="item" :label="t('recommend.validationItem')" width="170" />
        <el-table-column prop="message" :label="t('recommend.validationMessage')" min-width="240" />
      </el-table>
    </el-card>

    <!-- 推荐结果 -->
    <el-card shadow="never" v-if="recommendations.length">
      <template #header>
        <span class="card-title">{{ t('recommend.results', { n: recommendations.length }) }}</span>
        <el-button size="small" type="success" style="float: right" @click="confirmAll">
          {{ t('recommend.confirmAll') }}
        </el-button>
      </template>

      <div v-for="rec in recommendations" :key="rec.asset + rec.target_table" class="rec-item">
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item :label="t('recommend.sourceAsset')">
            <el-tag type="success">{{ rec.asset }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('recommend.targetTable')">
            <el-tag type="primary">{{ rec.target_table }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('recommend.confidence')">
            <el-progress :percentage="Math.round((rec.confidence || 0) * 100)" :stroke-width="10" style="width: 120px" />
          </el-descriptions-item>
          <el-descriptions-item :label="t('recommend.reason')">{{ rec.reason }}</el-descriptions-item>
        </el-descriptions>

        <div class="mt8 mb8"><b>{{ t('recommend.fieldMapping') }}</b></div>
        <el-table :data="rec.field_mappings" border size="small" class="fm-table">
          <el-table-column prop="source" :label="t('recommend.sourceField')" min-width="220" />
          <el-table-column prop="target" :label="t('recommend.targetCol')" min-width="140" />
          <el-table-column prop="transform" :label="t('recommend.transform')" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.transform" size="small" type="warning">{{ row.transform }}</el-tag>
              <span v-else class="gray">—</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <el-card shadow="never" v-else>
      <el-empty :description="t('recommend.emptyDesc')" />
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { aiApi, datasourceApi, mappingApi, modelApi, targetApi } from '../api/dataflow'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const allAssets = ref([])
const selectedAssets = ref([])
const selectedTargets = ref([])
const assetFields = ref({})
const targets = ref([])
const recommending = ref(false)
const recommendations = ref([])
const transformationPlan = ref(null)
const validation = ref(null)

// 验证检查项名称（后端 check 字段 → 本地化标签）
const CHECK_NAMES = {
  recommendations: t('recommend.checkRecommendations'),
  source_fields: t('recommend.checkSourceFields'),
}

// 验证报告标题（按状态/错误数生成）
const validationTitle = computed(() => {
  const v = validation.value
  if (!v) return ''
  const warns = v.warning_count || 0
  if (!v.ok) {
    return warns
      ? t('recommend.validationErrorWarn', { n: v.error_count, m: warns })
      : t('recommend.validationError', { n: v.error_count })
  }
  return warns ? t('recommend.validationOkWarn', { n: warns }) : t('recommend.validationOk')
})

// 验证报告 alert 类型：有错误 error / 有警告 warning / 全部通过 success
const validationType = computed(() => {
  const v = validation.value
  if (!v) return 'success'
  if (!v.ok) return 'error'
  return v.warning_count ? 'warning' : 'success'
})

// 目标下拉分组：DB/SOAP 目标、FHIR 已建模 US Core、FHIR 开放类型（服务器支持 R4，字段由 LLM 自定）
const targetGroups = computed(() => {
  const groups = []
  const other = targets.value.filter((x) => x.type !== 'FHIR')
  const modeled = targets.value.filter((x) => x.type === 'FHIR' && !x.open)
  const open = targets.value.filter((x) => x.type === 'FHIR' && !!x.open)
  if (other.length) {
    groups.push({ label: t('recommend.targetBaseGroup'), items: other })
  }
  if (modeled.length) {
    groups.push({ label: t('recommend.targetModeledGroup', { n: modeled.length }), items: modeled })
  }
  if (open.length) {
    groups.push({ label: t('recommend.targetOpenGroup', { n: open.length }), items: open })
  }
  return groups
})

// 目标下拉标签：前缀区分 DB/SOAP/FHIR（FHIR 目标不再误标 [DB]）
function targetPrefix(row) {
  if (!row) return ''
  if (row.type === 'SOAP') return '[SOAP] '
  if (row.type === 'FHIR') return '[FHIR] '
  return '[DB] '
}

// 目标下拉辅助信息：FHIR 开放类型提示交由 LLM；已建模显示 profile 简名；其余显示目标名
function targetSuffix(row) {
  if (!row) return ''
  if (row.type === 'FHIR') {
    if (row.open) return t('recommend.fhirOpenSuffix')
    const p = (row.profile || '').split('/StructureDefinition/').pop()
    return p || (row.target_name ? row.target_name : 'US Core')
  }
  return row.target_name ? row.target_name : row.schema
}

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
  const selectedTargetObjs = targets.value.filter((t) =>
    selectedTargets.value.includes((t.target_id || '') + '::' + t.table)
  )
  const targetList = selectedTargetObjs.map((t) => ({
    table: t.table,
    columns: t.columns,
    type: t.type,
    schema: t.schema,
    profile: t.profile || '',
    open: !!t.open,
    note: t.open
      ? '开放 FHIR R4 类型（服务器支持、平台未建模）：映射 target 字段需你依 FHIR R4/US Core 规范自定'
      : '',
  }))
  const targetModels = selectedTargetObjs.map((t) => ({
    id: t.target_id,
    target_id: t.target_id,
    name: t.target_name || t.table,
    type: t.type || 'DB',
    table: t.table,
    schema: t.schema,
    columns: t.columns || [],
    open: !!t.open,
    modeled: !!t.modeled || !t.open,
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
  // 默认勾选：DB/SOAP 全部 + FHIR 已建模类型；FHIR 开放类型（未建模）由用户按需勾选，
  // 勾选后其映射字段由 Agent 依 FHIR R4 规范自定（避免无关类型撑爆 LLM 上下文）。
  selectedTargets.value = targets.value
    .filter((t) => t.type !== 'FHIR' || !t.open)
    .map((t) => (t.target_id || '') + '::' + t.table)
}

async function handleRecommend() {
  if (!selectedAssets.value.length) {
    ElMessage.warning(t('recommend.warnSelectAsset'))
    return
  }
  if (!selectedTargets.value.length) {
    ElMessage.warning(t('recommend.warnSelectTarget'))
    return
  }
  recommending.value = true
  try {
    const data = await aiApi.recommend(buildRecommendPayload())
    recommendations.value = data?.recommendations || []
    transformationPlan.value = data?.transformation_plan || null
    validation.value = data?.validation || null
    ElMessage.success(t('recommend.recommendDone', { n: recommendations.value.length }))
    if (validation.value && !validation.value.ok) {
      ElMessage.warning(t('recommend.validationWarnMessage', { n: validation.value.error_count }))
    }
  } finally {
    recommending.value = false
  }
}

async function confirmAll() {
  if (!recommendations.value.length) return
  // 目标类型映射（mapping 必须带 target_type，避免后端默认 DB 导致 SOAP 目标误走 DB 路由）
  const typeByTable = {}
  for (const t of targets.value) {
    if (t && t.table && !(t.table in typeByTable)) typeByTable[t.table] = t.type || 'DB'
  }
  // 注意：Date.now() 在同一毫秒内多次调用返回相同值，若直接作为 id 会导致
  // 多条映射 id 相同（^demo.Mapping 以 id 为键），后保存的覆盖先保存的。
  // 因此用时间戳 + 序号拼接生成唯一 id。
  const stamp = Date.now() % 100000
  const mappings = recommendations.value.map((rec, idx) => ({
    id: `M${stamp}${idx}`,
    source: rec.asset,
    target_table: rec.target_table,
    target_type: typeByTable[rec.target_table] || 'DB',
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
  ElMessage.success(t('recommend.saved'))
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
.ml6 { margin-left: 6px; }
.sm { font-size: 12px; }
.card-title { font-weight: 600; }
</style>
