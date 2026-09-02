import axios from 'axios'
import { ElMessage } from 'element-plus'

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
    ElMessage.error(body.message || '请求失败')
    return Promise.reject(new Error(body.message || '请求失败'))
  },
  (error) => {
    ElMessage.error(error.response?.data?.message || error.message || '网络错误')
    return Promise.reject(error)
  }
)

export default http
