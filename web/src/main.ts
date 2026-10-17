/**
 * 控制台入口：装配 Vue / Pinia / Vue Router / Element Plus（中文语言包 + 全量图标）。
 */

import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia } from 'pinia'
import { createApp } from 'vue'
import 'element-plus/dist/index.css'
import App from './App.vue'
import router from './router'
import { useAuthStore } from './stores/auth'
import './styles/main.css'

const app = createApp(App)

// 全量注册图标组件，模板中可直接使用 <el-icon><Refresh /></el-icon>
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

const pinia = createPinia()
app.use(pinia)
app.use(router)
app.use(ElementPlus, { locale: zhCn })

// 会话失效（服务端返回 401）时统一清空本地用户态并跳转登录页。
// 注册在此处而非各页面：一处收敛所有请求的过期处理，避免逐页重复实现。
const auth = useAuthStore(pinia)
auth.bindUnauthorizedHandler(() => {
  auth.clear()
  const current = router.currentRoute.value
  if (current.name !== 'login') {
    void router.replace({ name: 'login', query: { redirect: current.fullPath } })
  }
})

app.mount('#app')