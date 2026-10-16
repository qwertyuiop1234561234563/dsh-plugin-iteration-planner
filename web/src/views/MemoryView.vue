<script setup lang="ts">
/**
 * 长期记忆（Mem0 风格）：Agent 在对话中自动沉淀的长期事实与约定的治理视图。
 *
 * 口径边界：
 *  - 记忆由后端在每轮对话后**自动提取**（模型抽取 → 向量去重 → 入库），本页只做
 *    「检视 / 纠偏 / 补录 / 清理」，不改动提取与召回算法；
 *  - 检索试跑走 `/memories/search`，返回的就是「下一轮对话模型会想起什么」，
 *    便于用户在发现记忆跑偏时及时停用或删除；
 *  - 未配置向量化模型时后端返回 503 EMBEDDING_UNAVAILABLE，本页据此展示引导，
 *    而不是把它伪装成「暂无记忆」。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type { MemoryItem, MemoryType } from '@/api/types'
import { MEMORY_IMPORTANCE_VALUES, MEMORY_TYPE_VALUES } from '@/api/types'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import {
  MEMORY_IMPORTANCE_LABEL,
  MEMORY_IMPORTANCE_TAG,
  MEMORY_STATUS_LABEL,
  MEMORY_TYPE_LABEL,
  formatIso,
} from '@/utils/format'

const stats = ref<Awaited<ReturnType<typeof api.memoryStats>> | null>(null)
const items = ref<MemoryItem[]>([])
const loading = ref(false)
/** 向量化未启用（后端 503 EMBEDDING_UNAVAILABLE）：整页降级为引导态 */
const disabled = ref(false)
const disabledReason = ref('')

/* --------------------------------- 过滤与检索 --------------------------------- */

const filterStatus = ref<'all' | 'active' | 'disabled'>('all')
const filterType = ref('')
const keyword = ref('')

const recallQuery = ref('')
const recalling = ref(false)
const recalled = ref<MemoryItem[]>([])

/* ---------------------------------- 手工新增 ---------------------------------- */

const createVisible = ref(false)
const creating = ref(false)
const createForm = ref({ text: '', type: 'fact', importance: 'medium', tags: '' })

const tiles = computed<StatTile[]>(() => [
  { label: '记忆总数', value: stats.value?.total ?? 0, tone: 'brand', hint: `上限 ${stats.value?.maxItems ?? 0} 条` },
  { label: '启用中', value: stats.value?.active ?? 0, hint: '参与语义召回' },
  { label: '已停用', value: stats.value?.disabled ?? 0, hint: '保留但不召回' },
  {
    label: '自动提取',
    value: stats.value?.autoExtract ? '开启' : '关闭',
    tone: stats.value?.autoExtract ? 'success' : 'default',
    hint: `召回 top-${stats.value?.topK ?? 0}`,
  },
])

/** 过滤后的行（关键词命中内容 / 标签 / 来源） */
const rows = computed<MemoryItem[]>(() => {
  const lowered = keyword.value.trim().toLowerCase()
  return items.value.filter((item) => {
    if (filterStatus.value !== 'all' && item.status !== filterStatus.value) return false
    if (filterType.value && item.type !== filterType.value) return false
    if (lowered) {
      const haystack = `${item.text} ${item.tags.join(' ')} ${item.source}`.toLowerCase()
      if (!haystack.includes(lowered)) return false
    }
    return true
  })
})

const typeOptions = MEMORY_TYPE_VALUES.map((value) => ({ value, label: MEMORY_TYPE_LABEL[value] }))
const importanceOptions = MEMORY_IMPORTANCE_VALUES.map((value) => ({
  value,
  label: `${value} · ${MEMORY_IMPORTANCE_LABEL[value]}`,
}))

/** 后端返回的是自由字符串，这里做安全映射（未知取值原样展示） */
function typeLabel(value: string): string {
  return MEMORY_TYPE_LABEL[value as MemoryType] ?? value
}

function importanceTag(value: string): 'primary' | 'success' | 'info' | 'warning' | 'danger' {
  return MEMORY_IMPORTANCE_TAG[value as keyof typeof MEMORY_IMPORTANCE_TAG] ?? 'info'
}

function importanceLabel(value: string): string {
  return MEMORY_IMPORTANCE_LABEL[value as keyof typeof MEMORY_IMPORTANCE_LABEL] ?? value
}

function statusLabel(value: string): string {
  return MEMORY_STATUS_LABEL[value as keyof typeof MEMORY_STATUS_LABEL] ?? value
}

/** 统一处理「向量化未启用」：置位引导态，避免把配置缺失当业务空数据 */
function handleError(error: unknown, prefix: string): void {
  const message = error instanceof ApiClientError ? error.message : String(error)
  if (error instanceof ApiClientError && error.code === 'EMBEDDING_UNAVAILABLE') {
    disabled.value = true
    disabledReason.value = message
    return
  }
  ElMessage.error(`${prefix}：${message}`)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [stat, list] = await Promise.all([api.memoryStats(), api.memories({ limit: 2000 })])
    stats.value = stat
    items.value = list.items
    disabled.value = false
    disabledReason.value = ''
  } catch (error) {
    items.value = []
    stats.value = null
    handleError(error, '加载长期记忆失败')
  } finally {
    loading.value = false
  }
}

/* -------------------------------- 状态与删除 -------------------------------- */

async function toggleStatus(item: MemoryItem): Promise<void> {
  const next = item.status === 'active' ? 'disabled' : 'active'
  try {
    await api.updateMemoryStatus(item.memoryId, next)
    ElMessage.success(next === 'active' ? '记忆已启用，将参与后续召回' : '记忆已停用，保留数据但不再召回')
    await load()
  } catch (error) {
    handleError(error, '更新记忆状态失败')
  }
}

async function remove(item: MemoryItem): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除这条记忆？\n\n「${item.text}」\n\n删除后不可恢复。`, '删除记忆', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await api.deleteMemory(item.memoryId)
    ElMessage.success('记忆已删除')
    await load()
  } catch (error) {
    handleError(error, '删除失败')
  }
}

async function clearAll(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认清空全部 ${stats.value?.total ?? 0} 条长期记忆？该操作不可恢复，Agent 将不再记得任何历史约定。`,
      '清空全部记忆',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await api.clearMemories()
    ElMessage.success(`已清空 ${result.deleted} 条记忆`)
    recalled.value = []
    await load()
  } catch (error) {
    handleError(error, '清空失败')
  }
}

/* ---------------------------------- 新增记忆 ---------------------------------- */

function openCreate(): void {
  createForm.value = { text: '', type: 'fact', importance: 'medium', tags: '' }
  createVisible.value = true
}

function splitTags(raw: string): string[] {
  return raw
    .replace(/，/g, ',')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

async function submitCreate(): Promise<void> {
  const text = createForm.value.text.trim()
  if (!text) {
    ElMessage.warning('请填写记忆内容')
    return
  }
  creating.value = true
  try {
    const memory = await api.createMemory({
      text,
      type: createForm.value.type,
      importance: createForm.value.importance,
      tags: splitTags(createForm.value.tags),
      source: 'manual',
    })
    if (memory.created) {
      ElMessage.success('记忆已写入')
    } else {
      ElMessage.info('与既有记忆高度相似，已按去重策略跳过（未重复写入）')
    }
    createVisible.value = false
    await load()
  } catch (error) {
    handleError(error, '新增记忆失败')
  } finally {
    creating.value = false
  }
}

/* -------------------------------- 语义召回试跑 -------------------------------- */

async function runRecall(): Promise<void> {
  const text = recallQuery.value.trim()
  if (!text) {
    ElMessage.warning('请输入一段话，模拟下一轮对话的输入')
    return
  }
  recalling.value = true
  try {
    const result = await api.searchMemory(text)
    recalled.value = result.items
    if (result.items.length === 0) ElMessage.info('未召回任何记忆（可能低于相关度下限或全部停用）')
    else await load()
  } catch (error) {
    recalled.value = []
    handleError(error, '召回试跑失败')
  } finally {
    recalling.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="ip-page">
    <el-alert v-if="disabled" type="warning" :closable="false" class="ip-mem__alert">
      <template #title>向量化模型未启用，长期记忆不可用</template>
      <div class="ip-mem__alert-body">
        {{ disabledReason || '请配置 EMBEDDING_API_KEY 或复用 LLM_API_KEY 后重启服务。' }}
        长期记忆依赖向量库做语义召回与去重，未启用时 Agent 自动退化为「无记忆」模式，主对话链路不受影响。
      </div>
    </el-alert>

    <StatTiles :items="tiles" />

    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>记忆清单</span>
        <div class="ip-mem__actions">
          <el-button size="small" :icon="'Refresh'" :disabled="disabled" @click="load">刷新</el-button>
          <el-button size="small" type="primary" :icon="'Plus'" :disabled="disabled" @click="openCreate">手工新增</el-button>
          <el-button size="small" type="danger" :icon="'Delete'" :disabled="disabled || items.length === 0" @click="clearAll">
            清空全部
          </el-button>
        </div>
      </div>

      <div class="ip-mem__filters">
        <el-radio-group v-model="filterStatus" size="small">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="active">启用中</el-radio-button>
          <el-radio-button value="disabled">已停用</el-radio-button>
        </el-radio-group>
        <el-select v-model="filterType" size="small" placeholder="全部类型" clearable style="width: 150px">
          <el-option v-for="item in typeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </el-select>
        <el-input
          v-model="keyword"
          size="small"
          placeholder="搜索内容 / 标签 / 来源"
          clearable
          style="max-width: 240px"
          :prefix-icon="'Search'"
        />
      </div>

      <el-table v-loading="loading" :data="rows" size="small" border>
        <el-table-column label="记忆内容" min-width="300">
          <template #default="{ row }">
            <div class="ip-mem__text">{{ row.text }}</div>
            <div v-if="row.tags.length > 0" class="ip-mem__tags">
              <el-tag v-for="tag in row.tags" :key="tag" size="small" effect="plain">{{ tag }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ typeLabel(row.type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="重要度" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="importanceTag(row.importance)">
              {{ importanceLabel(row.importance) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="row.status === 'active' ? 'success' : 'info'">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="120">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.source || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="最近使用" width="150">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.lastUsedAt ? formatIso(row.lastUsedAt) : '未召回' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" align="center" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text :type="row.status === 'active' ? 'info' : 'primary'" @click="toggleStatus(row)">
              {{ row.status === 'active' ? '停用' : '启用' }}
            </el-button>
            <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="disabled ? '向量化模型未启用' : '暂无长期记忆；对话中产生的约定会自动沉淀到这里'" />
        </template>
      </el-table>
    </div>

    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>语义召回试跑</span>
        <span class="ip-muted ip-mono">模拟下一轮对话输入，查看模型会想起哪些记忆</span>
      </div>

      <div class="ip-mem__recall">
        <el-input
          v-model="recallQuery"
          placeholder="例如：当前计划的人数改小一点"
          clearable
          :disabled="disabled"
          @keydown.enter="runRecall"
        />
        <el-button type="primary" :icon="'Search'" :loading="recalling" :disabled="disabled" @click="runRecall">召回</el-button>
      </div>

      <div v-if="recalled.length > 0" class="ip-mem__recalled">
        <div v-for="(item, index) in recalled" :key="item.memoryId" class="ip-mem__hit">
          <div class="ip-mem__hit-head">
            <el-tag size="small" effect="plain" type="primary">#{{ index + 1 }}</el-tag>
            <el-tag size="small" effect="plain">{{ typeLabel(item.type) }}</el-tag>
            <el-tag size="small" effect="plain" :type="importanceTag(item.importance)">{{ importanceLabel(item.importance) }}</el-tag>
            <span class="ip-mem__hit-score ip-mono">相似度 {{ item.score.toFixed(4) }}</span>
          </div>
          <div class="ip-mem__hit-text">{{ item.text }}</div>
        </div>
      </div>
      <el-empty v-else-if="!disabled" :image-size="64" description="输入一句话后点击「召回」，查看相似度排序结果" />
    </div>

    <el-dialog v-model="createVisible" title="手工新增记忆" width="560px" append-to-body destroy-on-close>
      <el-form label-width="88px">
        <el-form-item label="内容">
          <el-input
            v-model="createForm.text"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="自包含的一句话，例如：订单中心迭代默认配置 3 名开发"
          />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="createForm.type" style="width: 100%">
            <el-option v-for="item in typeOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="重要度">
          <el-select v-model="createForm.importance" style="width: 100%">
            <el-option v-for="item in importanceOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="createForm.tags" placeholder="逗号分隔（最多 5 个）" />
        </el-form-item>
      </el-form>
      <p class="ip-muted ip-mem__hint">写入前会与既有记忆做向量相似度比对，高度相似的内容将自动去重跳过。</p>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">写入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ip-mem__alert {
  margin-bottom: 12px;
}

.ip-mem__alert-body {
  margin-top: 4px;
  font-size: 12.5px;
  line-height: 1.7;
}

.ip-mem__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.ip-mem__filters {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.ip-mem__text {
  font-size: 13px;
  line-height: 1.7;
  color: var(--ip-text);
  white-space: normal;
  word-break: break-word;
}

.ip-mem__tags {
  margin-top: 4px;
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.ip-mem__recall {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.ip-mem__recalled {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ip-mem__hit {
  padding: 10px 12px;
  border: 1px solid var(--ip-border);
  border-left: 3px solid var(--ip-accent);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-mem__hit-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.ip-mem__hit-score {
  margin-left: auto;
  font-size: 11px;
  color: var(--ip-text-muted);
}

.ip-mem__hit-text {
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--ip-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.ip-mem__hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
}
</style>
