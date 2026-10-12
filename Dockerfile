# syntax=docker/dockerfile:1
#
# 企业级研发迭代规划智能 Agent —— 一体化镜像
#   阶段一：构建 Vue3 控制台（web/ → web/dist）
#   阶段二：装配 API 运行时（Fastify 托管 REST + WebSocket + 前端静态资源）
#
# 说明：后端以 TypeScript 源码直接运行（tsx），避免二次编译产物与源码漂移，
#       保证镜像内运行的正是仓库中的同一份代码。

# ----------------------------- 阶段一：前端构建 -----------------------------
FROM node:22-alpine AS web-builder
WORKDIR /build/web
# 仅拷贝依赖清单，命中 Docker 层缓存（依赖不变则不重复安装）
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ----------------------------- 阶段二：运行时 -----------------------------
FROM node:22-alpine AS runtime
WORKDIR /app

# 后端依赖（含 tsx 运行时依赖，直接执行 TS 源）
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# 后端源码与配置
COPY tsconfig.json ./
COPY src ./src

# 前端产物
COPY --from=web-builder /build/web/dist ./web/dist

# 运行时默认值（可被 docker-compose / 环境变量覆盖）
ENV NODE_ENV=production \
    API_HOST=0.0.0.0 \
    API_PORT=8080 \
    API_BASE_PATH=/api \
    WEB_STATIC_DIR=/app/web/dist \
    STORAGE_DRIVER=postgres \
    LOG_LEVEL=info

EXPOSE 8080

# 健康检查：轮询 /api/health（该路由不参与鉴权，适合探活）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8080/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["npm", "run", "api:start"]