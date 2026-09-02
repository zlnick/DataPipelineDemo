import { createI18n } from 'vue-i18n'
import zh from './locales/zh'
import en from './locales/en'

// 根据 URL 路径决定初始语言：/en 前缀 → 英文，其余 → 中文
const initialLang = window.location.pathname.startsWith('/en') ? 'en' : 'zh'

const i18n = createI18n({
  legacy: false,
  locale: initialLang,
  fallbackLocale: 'zh',
  messages: { zh, en },
})

export default i18n
