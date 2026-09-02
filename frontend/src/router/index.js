import { createRouter, createWebHistory } from 'vue-router'

// 路由表：每条路由同时提供中文（/）与英文（/en）前缀变体
const routes = [
  { path: '/', name: 'home', component: () => import('../views/Home.vue'), meta: { title: '项目概览' } },
  { path: '/en', name: 'home-en', component: () => import('../views/Home.vue'), meta: { title: 'Overview' } },
  { path: '/datasources', name: 'datasources', component: () => import('../views/Datasources.vue'), meta: { title: '数据源管理' } },
  { path: '/en/datasources', name: 'datasources-en', component: () => import('../views/Datasources.vue'), meta: { title: 'Data Sources' } },
  { path: '/assets', name: 'assets', component: () => import('../views/Assets.vue'), meta: { title: '数据资产' } },
  { path: '/en/assets', name: 'assets-en', component: () => import('../views/Assets.vue'), meta: { title: 'Data Assets' } },
  { path: '/recommend', name: 'recommend', component: () => import('../views/Recommend.vue'), meta: { title: 'AI 智能匹配' } },
  { path: '/en/recommend', name: 'recommend-en', component: () => import('../views/Recommend.vue'), meta: { title: 'AI Matching' } },
  { path: '/mappings', name: 'mappings', component: () => import('../views/Mappings.vue'), meta: { title: '转换关系' } },
  { path: '/en/mappings', name: 'mappings-en', component: () => import('../views/Mappings.vue'), meta: { title: 'Mappings' } },
  { path: '/pipelines', name: 'pipelines', component: () => import('../views/Pipelines.vue'), meta: { title: '管道监控' } },
  { path: '/en/pipelines', name: 'pipelines-en', component: () => import('../views/Pipelines.vue'), meta: { title: 'Pipeline Monitor' } },
  { path: '/targets', name: 'targets', component: () => import('../views/Targets.vue'), meta: { title: '转换目标' } },
  { path: '/en/targets', name: 'targets-en', component: () => import('../views/Targets.vue'), meta: { title: 'Targets' } },
  { path: '/agents', name: 'agents', component: () => import('../views/Agents.vue'), meta: { title: 'AI Agents' } },
  { path: '/en/agents', name: 'agents-en', component: () => import('../views/Agents.vue'), meta: { title: 'AI Agents' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
