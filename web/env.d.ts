/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

interface ImportMetaEnv {
  /** REST API 基地址（默认 /api，同域部署时留空即可） */
  readonly VITE_API_BASE?: string
  /** WebSocket 地址（默认按当前域名推导） */
  readonly VITE_WS_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}