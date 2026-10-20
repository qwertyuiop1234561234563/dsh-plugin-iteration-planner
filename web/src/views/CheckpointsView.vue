<script setup lang="ts">
/**
 * 运行台账（持久化检查点 · 断点续跑）。
 *
 * 这一页回答的是「**Agent 干到一半被打断了怎么办**」：写操作在落库前会先登记为待确认动作，
 * 过去这份「半成品」只活在进程内存里 —— 服务重启、副本切换、用户关掉浏览器就没了，
 * 用户只能把需求重新描述一遍。现在它是一份**持久化台账**：
 *
 *  - 列表：本主体最近的台账（待确认 / 可续跑 / 历史记录），只看摘要；
 *  - 详情：请求原文、待确认动作、已执行调用、结论、反思轨迹；
 *  - 续跑：`confirm` 确认落库（可只确认选中项）、`reject` 驳回选中项、`retry` 用**台账里保存的
 *    原始请求**重跑（用户不必重新描述需求）、`abort` 作废并移除（只丢弃，**绝不**顺带执行）。
 *
 * 口径边界：
 *  - 确认是**幂等**的：重复确认已终态台账会返回上次结果并标记 `replayed`，不会二次写库；
 *  - 台账按主体隔离，非属主一律 **404**（不区分「格式错」与「不存在」），本页只展示自己的；
 *  - 存储后端故障返回 503 `CHECKPOINT_STORE_UNAVAILABLE`，本页据此给出「后端不可用」提示，
 *    而不是伪装成「你没有任何台账」——那会让人误判成待确认动作丢了。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type {
  CheckpointAction,
  CheckpointDetail,
  CheckpointListResult,
  CheckpointSummary,
} from '@/api/types'
import ReflectionTrace from '@/components/ReflectionTrace.vue'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import {
  agentToolLabel,
  checkpointStatusLabel,
  checkpointStatusTag,
  formatTime,
} from '@/utils/format'

const list = ref<CheckpointListResult | null>(null)
const loading = ref(false)
/** 存储后端不可用（503）：整页降级为提示态，不把「查不到」当成「没有」 */
const unavailable = ref('')

const detail = ref<CheckpointDetail | null>(null)
const detailVisible = ref(false)
const detailLoading = ref(false)
/** 续跑中（按钮统一 loading，避免重复触发） */
const resuming = ref(false)
/** 详情里勾选的动作（用于「只确认这些」/「驳回这些」） */
const checked = ref<string[]>([])

/** 后端返回的是 Unix **秒**，展示层统一按毫秒时间戳处理 */
function toMillis(seconds: number): number {
  return Math.max(0, Math.round(seconds * 1000))
}

const items = computed<CheckpointSummary[]>(() => list.value?.items ?? [])

const tiles = computed<StatTile[]>(() => {
  const rows = items.value
  const pending = rows.reduce((total, row) => total + row.pendingCount, 0)
  const resumable = rows.filter((row) => row.resumable).length
  return [
    { label: '台账总数', value: rows.length, tone: 'brand', hint: '最近记录（列表上限 100 条）' },
    { label: '待确认动作', value: pending, tone: pending > 0 ? 'warning' : 'default', hint: '确认后才真正落库' },
    { label: '可续跑', value: resumable, hint: resumable > 0 ? '等待你批复或重跑' : '无中断的编排' },
    {
      label: '存储后端',
      value: list.value?.backend || '—',
      hint: list.value?.backend === 'redis' ? '多副本共享，重启不丢' : '单副本内存（重启即失）',
    },
  ]
})

/** 统一的错误处理：区分「台账存储不可用」与一般失败 */
function handleError(error: unknown, prefix: string): void {
  const message = error instanceof ApiClientError ? error.message : String(error)
  if (error instanceof ApiClientError && error.code === 'CHECKPOINT_STORE_UNAVAILABLE') {
    unavailable.value = message
    return
  }
  ElMessage.error(`${prefix}：${message}`)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    list.value = await api.checkpoints(100)
    unavailable.value = ''
  } catch (error) {
    list.value = null
    handleError(error, '加载运行台账失败')
  } finally {
    loading.value = false
  }
}

async function openDetail(checkpointId: string): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  checked.value = []
  try {
    detail.value = await api.checkpoint(checkpointId)
  } catch (error) {
    detail.value = null
    handleError(error, '读取台账详情失败')
  } finally {
    detailLoading.value = false
  }
}

/** 续跑完成后统一刷新：详情要看最新快照，列表要看最新状态 */
async function refreshAfterResume(checkpointId: string): Promise<void> {
  try {
    detail.value = await api.checkpoint(checkpointId)
    checked.value = []
  } catch {
    // 台账可能刚被作废移除：详情置空即可，不覆盖上面的成功提示
    detail.value = null
    detailVisible.value = false
  }
  await load()
}

/** 续跑：确认 / 驳回 / 重跑 / 作废，四种动作共用一个入口 */
async function resume(action: CheckpointAction, options: { reject?: boolean; actionIds?: string[] } = {}): Promise<void> {
  const current = detail.value
  if (!current) return
  resuming.value = true
  try {
    const result = await api.resumeCheckpoint(current.checkpointId, { action, ...options })
    if (result.replayed) {
      ElMessage.info('该台账此前已确认过，已返回上次结果（未重复写库）')
    } else if (result.rejected) {
      ElMessage.success(`已驳回 ${options.actionIds?.length ?? 0} 项待确认动作，未执行任何写入`)
    } else if (action === 'retry') {
      ElMessage.success('已按台账保存的原始请求重跑完成')
    } else if (result.remainingActions.length > 0) {
      ElMessage.warning(`部分动作未执行，仍有 ${result.remainingActions.length} 项待确认`)
    } else {
      ElMessage.success(`已确认落库 ${result.executed.length} 项写入`)
    }
    await refreshAfterResume(current.checkpointId)
  } catch (error) {
    handleError(error, '续跑台账失败')
  } finally {
    resuming.value = false
  }
}

async function abort(checkpointId: string): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '作废该台账：丢弃全部待确认动作并移除记录，不会执行任何写入。该操作不可恢复。',
      '作废台账',
      { type: 'warning', confirmButtonText: '作废', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  resuming.value = true
  try {
    await api.deleteCheckpoint(checkpointId)
    ElMessage.success('台账已作废移除')
    detailVisible.value = false
    detail.value = null
    await load()
  } catch (error) {
    handleError(error, '作废台账失败')
  } finally {
    resuming.value = false
  }
}

/** 是否还能在台账上继续操作（终态台账为只读历史） */
const canResume = computed(() => detail.value?.resumable === true)
const pendingActions = computed(() => detail.value?.pendingActions ?? [])
const awaiting = computed(() => detail.value?.status === 'awaiting_approval')

onMounted(load)
</script>

<template>
  <div class="ip-page">
    <el-alert v-if="unavailable" type="warning" :closable="false" class="ip-ckpt__alert">
      <template #title>运行台账存储不可用</template>
      <div class="ip-ckpt__alert-body">
        {{ unavailable }}
        台账是**状态数据**，后端故障时不会静默降级成「无记录」——避免让人误判待确认动作已丢失。
        恢复后刷新即可看到完整记录。
      </div>
    </el-alert>

    <StatTiles :items="tiles" />

    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>台账列表</span>
        <span class="ip-muted ip-mono">写操作确认前后的完整轨迹；重启 / 换副本后依然在</span>
        <el-button size="small" :icon="'Refresh'" @click="load">刷新</el-button>
      </div>

      <el-table v-loading="loading" :data="items" size="small" border>
        <el-table-column label="台账" min-width="220">
          <template #default="{ row }">
            <div class="ip-mono ip-ckpt__id">{{ row.checkpointId }}</div>
            <div class="ip-mono ip-muted ip-ckpt__sid">{{ row.sessionId || '—' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.kind === 'plan' ? '端到端建计划' : '会话对话' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="checkpointStatusTag(row.status)">
              {{ checkpointStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="待确认" width="80" align="center">
          <template #default="{ row }">
            <span :class="row.pendingCount > 0 ? 'ip-ckpt__pending' : 'ip-muted ip-mono'">
              {{ row.pendingCount }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="尝试" width="70" align="center">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.attempt }}</span>
          </template>
        </el-table-column>
        <el-table-column label="关联计划" width="140">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.planId || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ formatTime(toMillis(row.updatedAt)) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="170" align="center" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openDetail(row.checkpointId)">详情</el-button>
            <el-button size="small" text type="danger" :disabled="!row.resumable" @click="abort(row.checkpointId)">
              作废
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无运行台账；有待确认动作或编排失败时，这里会留下可续跑的记录" />
        </template>
      </el-table>
    </div>

    <el-dialog v-model="detailVisible" title="台账详情" width="820px" append-to-body destroy-on-close>
      <div v-loading="detailLoading" class="ip-ckpt__detail">
        <template v-if="detail">
          <el-descriptions :column="2" size="small" border>
            <el-descriptions-item label="台账 ID">
              <span class="ip-mono">{{ detail.checkpointId }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="状态">
              <el-tag size="small" effect="plain" :type="checkpointStatusTag(detail.status)">
                {{ checkpointStatusLabel(detail.status) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="尝试次数">
              <span class="ip-mono">{{ detail.attempt }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="关联计划">
              <span class="ip-mono">{{ detail.planId || '—' }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="会话 ID">
              <span class="ip-mono">{{ detail.sessionId || '—（端到端建计划）' }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="更新时间">
              <span class="ip-mono">{{ formatTime(toMillis(detail.updatedAt)) }}</span>
            </el-descriptions-item>
          </el-descriptions>

          <div class="ip-ckpt__block">
            <div class="ip-ckpt__block-title">原始请求</div>
            <div class="ip-ckpt__text">{{ detail.request.message }}</div>
            <div class="ip-muted ip-ckpt__meta">
              <span v-if="detail.request.planId">计划 {{ detail.request.planId }}</span>
              <span v-if="detail.request.iterationDeadline">截止 {{ detail.request.iterationDeadline }}</span>
              <span v-if="detail.request.devMemberCount">开发人数 {{ detail.request.devMemberCount }}</span>
            </div>
          </div>

          <el-alert v-if="detail.error" type="error" :closable="false" class="ip-ckpt__error">
            <template #title>编排失败</template>
            {{ detail.error }} —— 可点「按原请求重跑」重试，无需重新描述需求。
          </el-alert>

          <div class="ip-ckpt__block">
            <div class="ip-ckpt__block-title">
              待确认动作 {{ pendingActions.length }} 项
              <span class="ip-muted ip-ckpt__meta-inline">勾选后可「只确认这些」或「驳回这些」</span>
            </div>
            <el-checkbox-group v-if="pendingActions.length > 0" v-model="checked" class="ip-ckpt__actions">
              <div v-for="action in pendingActions" :key="action.actionId" class="ip-ckpt__action">
                <el-checkbox :value="action.actionId">
                  <span class="ip-ckpt__action-label">{{ action.label }}</span>
                </el-checkbox>
                <el-tag size="small" effect="plain">{{ agentToolLabel(action.tool) }}</el-tag>
                <span class="ip-mono ip-muted ip-ckpt__action-id">{{ action.actionId }}</span>
              </div>
            </el-checkbox-group>
            <el-empty v-else :image-size="48" description="无待确认动作" />
          </div>

          <div v-if="detail.executed.length > 0" class="ip-ckpt__block">
            <div class="ip-ckpt__block-title">已执行调用 {{ detail.executed.length }} 次</div>
            <div v-for="(call, index) in detail.executed" :key="index" class="ip-ckpt__call">
              <el-tag size="small" effect="plain" :type="call.ok ? 'success' : 'danger'">
                {{ call.ok ? '成功' : '失败' }}
              </el-tag>
              <span class="ip-ckpt__call-name">{{ agentToolLabel(call.name) }}</span>
              <span v-if="call.error" class="ip-ckpt__call-err">{{ call.error }}</span>
            </div>
          </div>

          <ReflectionTrace v-if="detail.reflections.length > 0" :reflections="detail.reflections" />

          <div v-if="detail.content" class="ip-ckpt__block">
            <div class="ip-ckpt__block-title">本轮结论</div>
            <div class="ip-ckpt__text">{{ detail.content }}</div>
          </div>
        </template>
        <el-empty v-else-if="!detailLoading" :image-size="64" description="未取到台账快照" />
      </div>

      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <template v-if="detail && canResume">
          <el-button
            :disabled="resuming"
            :loading="resuming"
            :icon="'RefreshLeft'"
            @click="resume('confirm', { reject: true, actionIds: checked })"
          >
            驳回{{ checked.length > 0 ? `选中 ${checked.length} 项` : '全部' }}
          </el-button>
          <el-button :disabled="resuming" :loading="resuming" :icon="'Refresh'" @click="resume('retry')">
            按原请求重跑
          </el-button>
          <el-button
            type="primary"
            :disabled="resuming"
            :loading="resuming"
            :icon="'Select'"
            @click="resume('confirm', checked.length > 0 ? { actionIds: checked } : {})"
          >
            {{ awaiting ? (checked.length > 0 ? `确认选中 ${checked.length} 项` : '确认全部落库') : '继续（自动分流）' }}
          </el-button>
        </template>
        <el-button v-else-if="detail" type="danger" @click="abort(detail.checkpointId)">作废并移除</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ip-ckpt__alert {
  margin-bottom: 12px;
}

.ip-ckpt__alert-body {
  margin-top: 4px;
  font-size: 12.5px;
  line-height: 1.7;
}

.ip-ckpt__id {
  font-size: 12px;
  color: var(--ip-text);
}

.ip-ckpt__sid {
  margin-top: 2px;
  font-size: 11px;
}

.ip-ckpt__pending {
  font-family: var(--ip-font-mono);
  font-weight: 600;
  color: var(--ip-warning);
}

.ip-ckpt__detail {
  min-height: 120px;
}

.ip-ckpt__block {
  margin-top: 14px;
}

.ip-ckpt__block-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ip-text-secondary);
}

.ip-ckpt__meta-inline {
  font-size: 11px;
  font-weight: 400;
}

.ip-ckpt__text {
  padding: 8px 10px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--ip-text);
  white-space: pre-wrap;
  word-break: break-word;
}

.ip-ckpt__meta {
  margin-top: 6px;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 11.5px;
}

.ip-ckpt__error {
  margin-top: 12px;
}

.ip-ckpt__actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ip-ckpt__action {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 10px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-ckpt__action-label {
  font-size: 12.5px;
  color: var(--ip-text);
}

.ip-ckpt__action-id {
  margin-left: auto;
  font-size: 11px;
}

.ip-ckpt__call {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 0;
  font-size: 12px;
  color: var(--ip-text);
}

.ip-ckpt__call-name {
  font-weight: 600;
}

.ip-ckpt__call-err {
  color: var(--ip-danger);
}
</style>
