import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址：容器内通过 VITE_BACKEND_URL 指向 backend 服务；本地默认宿主机 5001
const backendUrl = process.env.VITE_BACKEND_URL || 'http://localhost:5001'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    proxy: {
      // 前端所有 /api 请求通过 vite 代理转发到后端，规避跨域
      '/api': {
        target: backendUrl,
        changeOrigin: true,
      },
    },
  },
})
