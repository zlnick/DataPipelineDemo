<template>
  <el-container class="app-layout">
    <!-- 左侧固定导航栏（宽度 220px） -->
    <el-aside width="220px" class="app-aside">
      <div class="app-logo">
        <el-icon :size="24" color="#409EFF"><MagicStick /></el-icon>
        <span>{{ t('app.title') }}</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        background-color="#001529"
        text-color="rgba(255,255,255,0.68)"
        active-text-color="#ffffff"
        class="app-menu"
      >
        <el-menu-item :index="lang ? '/en' : '/'" :key="(lang || 'zh') + '-home'">
          <el-icon><HomeFilled /></el-icon>
          <span>{{ t('app.menu.overview') }}</span>
        </el-menu-item>
        <el-menu-item :index="lang + '/datasources'" :key="(lang || 'zh') + '-ds'">
          <el-icon><Connection /></el-icon>
          <span>{{ t('app.menu.datasources') }}</span>
        </el-menu-item>
        <el-menu-item :index="lang + '/assets'" :key="(lang || 'zh') + '-assets'">
          <el-icon><Files /></el-icon>
          <span>{{ t('app.menu.assets') }}</span>
        </el-menu-item>
        <el-menu-item :index="lang + '/recommend'" :key="(lang || 'zh') + '-rec'">
          <el-icon><MagicStick /></el-icon>
          <span>{{ t('app.menu.recommend') }}</span>
        </el-menu-item>
        <el-menu-item :index="lang + '/mappings'" :key="(lang || 'zh') + '-map'">
          <el-icon><Share /></el-icon>
          <span>{{ t('app.menu.mappings') }}</span>
        </el-menu-item>
        <el-menu-item :index="lang + '/pipelines'" :key="(lang || 'zh') + '-pipe'">
          <el-icon><Cpu /></el-icon>
          <span>{{ t('app.menu.pipelines') }}</span>
        </el-menu-item>
        <el-menu-item :index="lang + '/targets'" :key="(lang || 'zh') + '-tgt'">
          <el-icon><Coin /></el-icon>
          <span>{{ t('app.menu.targets') }}</span>
        </el-menu-item>
        <el-menu-item :index="lang + '/agents'" :key="(lang || 'zh') + '-agt'">
          <el-icon><SetUp /></el-icon>
          <span>{{ t('app.menu.agents') }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container class="app-right">
      <!-- 顶部标题栏 -->
      <el-header class="app-header">
        <span class="app-header-title">{{ currentTitle }}</span>
      </el-header>
      <!-- 主内容区 -->
      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, watchEffect } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'

const route = useRoute()
const { t } = useI18n()

// 当前语言前缀：/en 路径 → '/en'，否则 ''
const lang = computed(() => (route.path.startsWith('/en') ? '/en' : ''))

// 子路由高亮对应的一级菜单（忽略 /en 前缀）
const activeMenu = computed(() => {
  const p = route.path.startsWith('/en') ? route.path.slice(3) : route.path
  return route.meta.activeMenu || p
})

// 顶部标题：英文用 i18n，中文用路由 meta.title
const currentTitle = computed(() => {
  const base = route.name ? String(route.name).replace(/-en$/, '') : ''
  if (lang.value === '/en') return t(`app.menu.${base}`)
  return route.meta.title || t('app.title')
})

// 浏览器标签页标题 + <html lang> 随语言与页面切换（英文页不再出现中文标题）
watchEffect(() => {
  document.documentElement.lang = lang.value === '/en' ? 'en' : 'zh-CN'
  document.title = `${t('app.title')} · ${currentTitle.value}`
})
</script>

<style scoped>
.app-layout {
  height: 100%;
}

.app-aside {
  background-color: #001529;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.app-logo {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  flex-shrink: 0;
}

.app-menu {
  border-right: none;
  flex: 1;
}

.app-right {
  min-width: 0;
}

.app-header {
  background: #fff;
  display: flex;
  align-items: center;
  box-shadow: 0 1px 4px rgba(0, 21, 41, 0.08);
  z-index: 10;
}

.app-header-title {
  font-size: 16px;
  font-weight: 600;
  color: #001529;
}

.app-main {
  background-color: #f0f2f5;
  padding: 16px;
}
</style>
