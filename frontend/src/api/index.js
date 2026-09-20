import axios from 'axios'
import { ElMessage } from 'element-plus'

import i18n from '../i18n'

// 兜底文案走 i18n（英文页 /en 不再弹中文；i18n 尚未就绪时退回中文）
const tt = (key, fallback) => {
  try {
    return i18n.global.t(key)
  } catch (e) {
    return fallback
  }
}

// axios 实例：统一 baseURL（生产环境经 nginx 同源代理 /api，开发环境经 vite 代理）
const http = axios.create({
  baseURL: '/api',
  timeout: 300000,
})

// 响应拦截器：解包统一响应格式 {code, data, message}
http.interceptors.response.use(
  (response) => {
    const body = response.data
    // 非 JSON 或非统一格式时直接返回
    if (body === null || typeof body !== 'object' || !('code' in body)) {
      return body
    }
    if (body.code === 0) {
      return body.data
    }
    ElMessage.error(body.message || tt('common.requestFailed', '请求失败'))
    return Promise.reject(new Error(body.message || tt('common.requestFailed', '请求失败')))
  },
  (error) => {
    ElMessage.error(error.response?.data?.message || error.message || tt('common.networkError', '网络错误'))
    return Promise.reject(error)
  }
)

export default http
