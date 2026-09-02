# =====================================================================
# AI 数据自动化转换 Demo - 前端多阶段构建
# 阶段 1：node:18-alpine 执行 npm run build 产出 dist/
# 阶段 2：nginx:alpine 部署静态产物并反向代理 /api 到后端
# =====================================================================
FROM node:18-alpine AS build

WORKDIR /app

# 先复制依赖清单并安装，充分利用 Docker 层缓存
COPY package.json ./
RUN npm install --registry=https://registry.npmmirror.com || npm install

# 复制源码并构建
COPY . .
RUN npm run build

# ---- 运行阶段 ----
FROM nginx:alpine

# 复制静态产物
COPY --from=build /app/dist /usr/share/nginx/html
# 覆盖默认配置：SPA 路由回退 + /api 反向代理到后端服务
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
