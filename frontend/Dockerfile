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

# 清掉构建层里可能残留的 apk 锁文件（实测 2026-09-18：残留 /lib/apk/db/lock 会加剧下面的问题）
RUN rm -f /lib/apk/db/lock

# 去掉官方 entrypoint 的「IPv6 监听补齐」脚本（它用 `apk manifest nginx` 校验 default.conf 的
# apk 校验和；本环境该命令**直接挂死**（实测 `timeout 8 apk manifest nginx` 被 Terminated）→
# entrypoint 卡死 → 容器 Up 但 80 端口无人监听（`curl /` 返回 000）。
# 我们本来就用自己的 nginx.conf 覆盖 default.conf（见下一行 COPY），该脚本无实际作用。
RUN rm -f /docker-entrypoint.d/10-listen-on-ipv6-by-default.sh

# 复制静态产物
COPY --from=build /app/dist /usr/share/nginx/html
# 覆盖默认配置：SPA 路由回退 + /api 反向代理到后端服务
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
