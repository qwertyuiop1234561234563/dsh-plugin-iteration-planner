<script setup lang="ts">
/**
 * 账号与权限页（仅管理员可见）。
 *
 * 能力：新建账号、变更角色 / 状态、重置口令、删除账号，以及为访客（guest）维护
 * 「可见计划白名单」——访客只能看到被指派的计划，其余计划在列表与详情均不可见。
 *
 * 口径：
 *  - 服务端已做全部权限校验，本页的按钮禁用仅为体验优化（防止无谓的 403 往返）；
 *  - 删除 / 停用 / 降权最后一个管理员会被服务端拒绝（保证控制台始终有管理入口）；
 *  - 重置口令或变更角色后，该账号的既有会话立即失效（需重新登录）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type { AuthAccount, AuthRole } from '@/api/types'
import { ROLE_LABELS, ROLE_TAG_TYPES, useAuthStore } from '@/stores/auth'
import { usePlanStore } from '@/stores/plan'
import { truncate } from '@/utils/format'
import { formatIso } from '@/utils/format'

const auth = useAuthStore()
const planStore = usePlanStore()

const loading = ref(false)
const accounts = ref<AuthAccount[]>([])

/** 新建账号表单 */
const createVisible = ref(false)
const createForm = reactive({ username: '', password: '', role: 'viewer' as AuthRole, displayName: '' })
const createLoading = ref(false)

/** 编辑账号表单 */
const editVisible = ref(false)
const editLoading = ref(false)
const editTarget = ref<AuthAccount | null>(null)
const editForm = reactive({ role: 'viewer' as AuthRole, status: 'active' as 'active' | 'disabled', displayName: '', password: '' })

/** 计划指派（仅访客） */
const assignVisible = ref(false)
const assignLoading = ref(false)
const assignTarget = ref<AuthAccount | null>(null)
const assignIds = ref<string[]>([])

/** 角色选项（顺序即权限由高到低） */
const roleOptions = (Object.keys(ROLE_LABELS) as AuthRole[]).map((role) => ({
  value: role,
  label: ROLE_LABELS[role],
}))

/** 计划下拉选项 */
const planOptions = computed(() =>
  planStore.summaries.map((item) => ({
    value: item.planId,
    label: `${truncate(item.rawRequirement, 26)}（${item.taskCount} 任务）`,
  })),
)

/** 当前登录账号（禁止删除自己） */
const selfId = computed(() => auth.user?.userId ?? '')

function errorText(error: unknown): string {
  return error instanceof ApiClientError ? error.message : '操作失败，请稍后重试'
}

async function reload(): Promise<void> {
  loading.value = true
  try {
    accounts.value = (await api.listUsers()).items
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  createForm.username = ''
  createForm.password = ''
  createForm.role = 'viewer'
  createForm.displayName = ''
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  if (!createForm.username.trim() || !createForm.password) {
    ElMessage.warning('请填写用户名与初始口令')
    return
  }
  createLoading.value = true
  try {
    await api.createUser({
      username: createForm.username.trim(),
      password: createForm.password,
      role: createForm.role,
      displayName: createForm.displayName.trim() || undefined,
    })
    ElMessage.success('账号已创建')
    createVisible.value = false
    await reload()
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    createLoading.value = false
  }
}

function openEdit(row: AuthAccount): void {
  editTarget.value = row
  editForm.role = row.role
  editForm.status = row.status
  editForm.displayName = row.displayName
  editForm.password = ''
  editVisible.value = true
}

async function submitEdit(): Promise<void> {
  const target = editTarget.value
  if (!target) return
  // 仅提交发生变化的字段：避免「未改动也触发会话吊销」这类副作用
  const payload: {
    role?: AuthRole
    status?: 'active' | 'disabled'
    displayName?: string
    password?: string
  } = {}
  if (editForm.role !== target.role) payload.role = editForm.role
  if (editForm.status !== target.status) payload.status = editForm.status
  if (editForm.displayName.trim() !== target.displayName) payload.displayName = editForm.displayName.trim()
  if (editForm.password) payload.password = editForm.password
  if (Object.keys(payload).length === 0) {
    ElMessage.info('未检测到任何变更')
    return
  }
  editLoading.value = true
  try {
    await api.updateUser(target.userId, payload)
    ElMessage.success('账号已更新')
    editVisible.value = false
    await reload()
    // 改到自己的角色 / 口令时，本地用户态需同步刷新（否则菜单与权限清单会与实际不符）
    if (target.userId === selfId.value) await auth.refresh()
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    editLoading.value = false
  }
}

async function toggleStatus(row: AuthAccount): Promise<void> {
  const next = row.status === 'active' ? 'disabled' : 'active'
  try {
    await api.updateUser(row.userId, { status: next })
    ElMessage.success(next === 'active' ? '账号已启用' : '账号已停用')
    await reload()
  } catch (error) {
    ElMessage.error(errorText(error))
  }
}

async function removeAccount(row: AuthAccount): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除账号「${row.username}」？其会话将立即失效，计划指派一并清除。`,
      '删除账号',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.deleteUser(row.userId)
    ElMessage.success('账号已删除')
    await reload()
  } catch (error) {
    ElMessage.error(errorText(error))
  }
}

async function openAssign(row: AuthAccount): Promise<void> {
  assignTarget.value = row
  assignIds.value = [...row.planIds]
  assignVisible.value = true
  // 计划列表可能尚未加载（本页不依赖计划上下文）
  if (planStore.summaries.length === 0) await planStore.loadPlans()
}

async function submitAssign(): Promise<void> {
  const target = assignTarget.value
  if (!target) return
  assignLoading.value = true
  try {
    await api.setUserPlans(target.userId, assignIds.value)
    ElMessage.success('可见计划已更新')
    assignVisible.value = false
    await reload()
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    assignLoading.value = false
  }
}

onMounted(reload)
</script>

<template>
  <div class="ip-page">
    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>账号与权限</span>
        <div class="ip-actions">
          <el-button :icon="'Refresh'" :loading="loading" @click="reload">刷新</el-button>
          <el-button type="primary" :icon="'Plus'" @click="openCreate">新建账号</el-button>
        </div>
      </div>

      <div class="ip-stats">
        <el-tag type="success" effect="plain" size="large">管理员 {{ accounts.filter((a) => a.role === 'admin').length }}</el-tag>
        <el-tag type="primary" effect="plain" size="large">成员 {{ accounts.filter((a) => a.role === 'member').length }}</el-tag>
        <el-tag type="info" effect="plain" size="large">只读 {{ accounts.filter((a) => a.role === 'viewer').length }}</el-tag>
        <el-tag type="warning" effect="plain" size="large">访客 {{ accounts.filter((a) => a.role === 'guest').length }}</el-tag>
        <el-tag type="danger" effect="plain" size="large">停用 {{ accounts.filter((a) => a.status === 'disabled').length }}</el-tag>
      </div>

      <el-table v-loading="loading" :data="accounts" stripe empty-text="暂无账号">
        <el-table-column label="用户名" min-width="140">
          <template #default="{ row }">
            <span class="ip-mono">{{ row.username }}</span>
            <el-tag v-if="row.userId === selfId" size="small" type="info" effect="plain" class="ip-self">当前</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="显示名" min-width="120" prop="displayName" />
        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="ROLE_TAG_TYPES[row.role as AuthRole]" effect="plain">
              {{ ROLE_LABELS[row.role as AuthRole] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'active' ? 'success' : 'danger'" effect="plain">
              {{ row.status === 'active' ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="可见计划" width="110" align="center">
          <template #default="{ row }">
            <el-button v-if="row.role === 'guest'" link type="primary" @click="openAssign(row)">
              指派 {{ row.planIds.length }} 个
            </el-button>
            <span v-else class="ip-muted">全部</span>
          </template>
        </el-table-column>
        <el-table-column label="最近登录" width="170">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.lastLoginAt ? formatIso(row.lastLoginAt) : '从未登录' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link :type="row.status === 'active' ? 'warning' : 'success'" @click="toggleStatus(row)">
              {{ row.status === 'active' ? '停用' : '启用' }}
            </el-button>
            <el-button link type="danger" :disabled="row.userId === selfId" @click="removeAccount(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新建账号 -->
    <el-dialog v-model="createVisible" title="新建账号" width="480px">
      <el-form label-width="96px">
        <el-form-item label="用户名" required>
          <el-input v-model="createForm.username" placeholder="3-64 位字母 / 数字 / . _ @ -" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="createForm.displayName" placeholder="缺省与用户名相同" />
        </el-form-item>
        <el-form-item label="初始口令" required>
          <el-input v-model="createForm.password" type="password" show-password placeholder="至少 8 位，含字母与数字" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="createForm.role" style="width: 100%">
            <el-option v-for="item in roleOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createLoading" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑账号 -->
    <el-dialog v-model="editVisible" :title="`编辑账号 · ${editTarget?.username ?? ''}`" width="480px">
      <el-form label-width="96px">
        <el-form-item label="显示名">
          <el-input v-model="editForm.displayName" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="editForm.role" style="width: 100%">
            <el-option v-for="item in roleOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-radio-group v-model="editForm.status">
            <el-radio value="active">启用</el-radio>
            <el-radio value="disabled">停用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="重置口令">
          <el-input v-model="editForm.password" type="password" show-password placeholder="留空表示不修改" />
        </el-form-item>
      </el-form>
      <p class="ip-hint">变更角色 / 状态或重置口令后，该账号的既有会话会立即失效，需重新登录。</p>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editLoading" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 计划指派（访客专属） -->
    <el-dialog v-model="assignVisible" :title="`可见计划 · ${assignTarget?.username ?? ''}`" width="560px">
      <p class="ip-hint">
        访客只能看到此处勾选的计划；未勾选任何计划时，该访客登录后计划列表为空。
      </p>
      <el-select v-model="assignIds" multiple filterable clearable style="width: 100%" placeholder="选择可见计划">
        <el-option v-for="item in planOptions" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>
      <template #footer>
        <el-button @click="assignVisible = false">取消</el-button>
        <el-button type="primary" :loading="assignLoading" @click="submitAssign">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ip-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ip-stats {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}

.ip-self {
  margin-left: 8px;
}

.ip-hint {
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-muted);
}
</style>
