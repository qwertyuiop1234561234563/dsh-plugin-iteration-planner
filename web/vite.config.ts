/**
 * Vite 构建配置。
 *
 * 开发环境通过 proxy 把 /api 与 /ws 转发到本地 API 服务（默认 http://localhost:8080），
 * 生产环境由 API 服务直接托管 dist 静态资源（同域部署，无需再配置代理）。
 */

import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/** 后端 API 地址（开发代理目标，可用 VITE_DEV_API_TARGET 覆盖） */
const devApiTarget = process.env.VITE_DEV_API_TARGET ?? 'http://localhost:8080'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/api': { target: devApiTarget, changeOrigin: true },
      '/ws': { target: devApiTarget, changeOrigin: true, ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // 拆分大依赖，避免单文件过大影响首屏
        manualChunks: {
          vue: ['vue', 'vue-router', 'pinia'],
          element: ['element-plus', '@element-plus/icons-vue'],
          charts: ['echarts'],
          graph: ['@antv/x6'],
        },
      },
    },
  },
})