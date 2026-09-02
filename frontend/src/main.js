import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import enLocale from 'element-plus/es/locale/lang/en'
import * as Icons from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import './styles/index.css'

const app = createApp(App)

// 全局注册 Element Plus 图标组件
for (const [key, component] of Object.entries(Icons)) {
  app.component(key, component)
}

// Element Plus 组件语言随当前语言切换（/en → English，否则简体中文）
const epLocale = i18n.global.locale.value === 'en' ? enLocale : zhCn
app.use(ElementPlus, { locale: epLocale })
app.use(i18n)
app.use(router)
app.mount('#app')
