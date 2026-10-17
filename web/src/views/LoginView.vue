<script setup lang="ts">
/**
 * 登录页：账号口令登录（服务端会话 Cookie）。
 *
 * 说明：
 *  - 登录成功后跳转 `redirect` 查询参数指向的页面（由路由守卫写入），缺省进入计划列表；
 *  - 口令框回车即提交；提交中禁用表单避免重复请求；
 *  - 失败提示沿用服务端文案（后端对「用户名不存在」与「口令错误」返回同一句，避免账号枚举）。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ApiClientError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const form = ref({ username: '', password: '' })
const loading = ref(false)
const errorText = ref('')

/** 登录后要去的页面（来自守卫写入的 redirect；缺省进计划列表） */
const redirect = computed(() => {
  const value = route.query.redirect
  return typeof value === 'string' && value.startsWith('/') ? value : '/plans'
})

async function submit(): Promise<void> {
  if (loading.value) return
  errorText.value = ''
  if (!form.value.username.trim() || !form.value.password) {
    errorText.value = '请填写用户名与口令'
    return
  }
  loading.value = true
  try {
    await auth.login(form.value.username.trim(), form.value.password)
    ElMessage.success(`欢迎，${auth.displayName}`)
    await router.replace(redirect.value)
  } catch (error) {
    // 只展示服务端文案：不区分「账号不存在 / 口令错误」，避免被用于枚举账号
    errorText.value =
      error instanceof ApiClientError ? error.message : '登录失败，请稍后重试'
    form.value.password = ''
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  // 会话失效跳转过来时给出原因提示（一次性，避免刷新后重复出现）
  if (route.query.expired === '1') ElMessage.warning('登录状态已失效，请重新登录')
})
</script>

<template>
  <div class="ip-login">
    <div class="ip-login__card">
      <div class="ip-login__brand">
        <el-icon class="ip-login__mark" :size="22"><Compass /></el-icon>
        <div>
          <div class="ip-login__title">迭代规划控制台</div>
          <div class="ip-login__sub">Iteration Planner · Sign in</div>
        </div>
      </div>

      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            placeholder="请输入用户名"
            autocomplete="username"
            size="large"
            :disabled="loading"
          />
        </el-form-item>
        <el-form-item label="口令">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="请输入口令"
            autocomplete="current-password"
            size="large"
            :disabled="loading"
            @keyup.enter="submit"
          />
        </el-form-item>

        <p v-if="errorText" class="ip-login__error">{{ errorText }}</p>

        <el-button type="primary" size="large" class="ip-login__submit" :loading="loading" @click="submit">
          登录
        </el-button>
      </el-form>

      <p class="ip-login__note">
        登录态由服务端会话 Cookie 承载（httpOnly，脚本不可读）。若忘记口令，
        请联系管理员在「账号与权限」中重置。
      </p>
    </div>
  </div>
</template>

<style scoped>
.ip-login {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
}

.ip-login__card {
  width: 100%;
  max-width: 400px;
  padding: 32px 32px 24px;
  background: var(--ip-surface);
  border: 1px solid var(--ip-border);
  border-top: 3px solid var(--ip-brand);
  border-radius: var(--ip-radius-lg);
  box-shadow: 0 18px 40px rgba(4, 18, 31, 0.14);
}

.ip-login__brand {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 18px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--ip-border);
}

.ip-login__mark {
  color: var(--ip-accent);
}

.ip-login__title {
  font-family: var(--ip-font-display);
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--ip-text);
}

.ip-login__sub {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ip-text-muted);
}

.ip-login__error {
  margin: 0 0 14px;
  padding: 9px 12px;
  font-size: 13px;
  color: var(--ip-danger);
  background: color-mix(in srgb, var(--ip-danger) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--ip-danger) 34%, transparent);
  border-radius: var(--ip-radius);
}

.ip-login__submit {
  width: 100%;
}

.ip-login__note {
  margin: 18px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-muted);
}
</style>
