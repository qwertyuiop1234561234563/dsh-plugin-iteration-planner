<script setup lang="ts">
/**
 * Agent 执行轨迹（调用链瀑布图 + 逐 step 回放 + 事故一键沉淀为回归用例）。
 *
 * 这一页回答的是「**它当时到底做了什么**」。台账只记录「有待确认动作 / 失败」的运行，
 * 评测台只给结论分数；而调试「为什么这次答得不对」「为什么这个计划少了一个任务」需要
 * 看**过程** —— 每一步模型读了什么、调了哪个工具、拿了什么返回、被哪条护栏纠正过。
 *
 * 三件事在这一页形成闭环：
 *
 * 1. **瀑布图**：span 按时间轴排布（横向偏移 = 起始时刻，条宽 = 耗时），一眼看出耗时黑洞
 *    （是模型慢还是引擎慢）与失败落在第几步；点任一时间片，右下展示该片的输入 / 输出 / 说明，
 *    即「逐 step 回放」；
 * 2. **按原请求重跑**：轨迹存了原始请求，不必让人重新手写需求；重跑后的结果与新轨迹 ID
 *    直接回显，可立刻对照两次运行的差异；
 * 3. **沉淀为回归用例**：断言由服务端从事实**确定性推导**（工具失败 → 要求不再失败；
 *    护栏触发 → 要求归零），沉淀后自动并入评测运行集与 CI 门禁 —— 事故只发生一次。
 *
 * 边界：轨迹是**观测设施**，不是审计台账 —— 存储不可用时返回 503 并明确提示，不静默返回
 * 空列表（那会被误读成「最近没跑过」）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type {
  AgentChatResult,
  IncidentCaseResult,
  TraceDetail,
  TraceListResult,
  TraceReplay,
  TraceSpan,
  TraceSummary,
} from '@/api/types'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import {
  formatDuration,
  formatTime,
  traceKindLabel,
  traceKindTag,
  traceRunStatusLabel,
  traceRunStatusTag,
  traceSpanStatusLabel,
  traceSpanStatusTag,
  truncate,
} from '@/utils/format'

const route = useRoute()
const router = useRouter()

const list = ref<TraceListResult | null>(null)
const loadingList = ref(false)
/** 轨迹存储未启用（503 TRACE_STORE_UNAVAILABLE）时整页降级为提示态 */
const disabledReason = ref('')

const kindFilter = ref('')
const statusFilter = ref('')

const detail = ref<TraceDetail | null>(null)
const detailLoading = ref(false)
/** 当前选中的时间片（逐 step 回放） */
const selectedIndex = ref<number | null>(null)

const replay = ref<TraceReplay | null>(null)
const replaying = ref(false)
/** 重跑结果（含新轨迹 ID，便于与新一次运行对照） */
const replayResult = ref<AgentChatResult | null>(null)

const sinkVisible = ref(false)
const sinking = ref(false)
const sinkNote = ref('')
const sinkTitle = ref('')

const items = computed(() => list.value?.items ?? [])

const tiles = computed<StatTile[]>(() => {
  const all = items.value
  const incidents = all.filter((item) => item.hasIncident).length
  const failed = all.filter((item) => item.status === 'error').length
  const guards = all.reduce((total, item) => total + item.guardCount, 0)
  return [
    { label: '保留轨迹', value: all.length, tone: 'brand', hint: `上限 ${list.value?.total ?? 0} 条（按 TTL 滚动过期）` },
    { label: '含事故信号', value: incidents, tone: incidents > 0 ? 'danger' : 'success', hint: '工具失败 / 护栏触发 / 降级' },
    { label: '编排失败', value: failed, tone: failed > 0 ? 'danger' : 'success', hint: '异常中断并已落库，可复现' },
    { label: '护栏拦截', value: guards, hint: '谎报完成 / 无依据作答 / 空转被纠正' },
  ]
})

/** 瀑布图时间轴总长（秒）：取 span 终点最大值，兜底轨迹总耗时 */
const trackTotal = computed(() => {
  const current = detail.value
  if (!current) return 1
  const ends = current.spans.map((span) => span.at + span.durationMs / 1000)
  const max = ends.length > 0 ? Math.max(...ends) : 0
  return max > 0 ? max : Math.max(current.durationMs / 1000, 0.001)
})

/** span 在时间轴上的左偏移与宽度（百分比）；最小宽度保证 0ms 级时间片仍可见 */
function barStyle(span: TraceSpan): Record<string, string> {
  const left = Math.min(99.5, (span.at / trackTotal.value) * 100)
  const width = Math.max(0.5, (span.durationMs / 1000 / trackTotal.value) * 100)
  return { left: `${left}%`, width: `${width}%` }
}

/** 时间片条形配色：按类型取语义色，失败片强制红色（一眼定位断点） */
function barTone(span: TraceSpan): string {
  if (span.status === 'error') return '#d92d20'
  if (span.status === 'degraded') return '#f79009'
  if (span.kind === 'llm') return '#2b6cb0'
  if (span.kind === 'tool') return '#12b76a'
  if (span.kind === 'guard') return '#d92d20'
  if (span.kind === 'reflect') return '#f79009'
  return '#8c8c8c'
}

function handleError(error: unknown, prefix: string): void {
  const message = error instanceof ApiClientError ? error.message : String(error)
  if (error instanceof ApiClientError && error.code === 'TRACE_STORE_UNAVAILABLE') {
    disabledReason.value = message
    return
  }
  ElMessage.error(`${prefix}：${message}`)
}

async function loadList(): Promise<void> {
  loadingList.value = true
  try {
    list.value = await api.traces({
      limit: 50,
      kind: kindFilter.value || undefined,
    })
    disabledReason.value = ''
  } catch (error) {
    list.value = null
    handleError(error, '加载执行轨迹失败')
  } finally {
    loadingList.value = false
  }
}

/** 打开某条轨迹：拉全量 span 后自动选中第一条失败/降级时间片（事故复盘不必手动找断点） */
async function openTrace(traceId: string, event?: 'replay'): Promise<void> {
  detailLoading.value = true
  replay.value = null
  replayResult.value = null
  selectedIndex.value = null
  try {
    detail.value = await api.trace(traceId)
    const first = detail.value.spans.find((span) => span.status === 'error' || span.status === 'degraded')
    selectedIndex.value = (first ?? detail.value.spans[0])?.index ?? null
    replay.value = await api.traceReplay(traceId)
    if (event === 'replay') await runReplay()
  } catch (error) {
    detail.value = null
    handleError(error, '加载轨迹明细失败')
  } finally {
    detailLoading.value = false
  }
}

/**
 * 按原请求重跑。
 *
 * 只对**会话类**轨迹就地重跑：这类重跑不改任何数据（写操作仍需二次确认）。
 * 计划类轨迹若在这里重跑会真的新建一份计划，属于有副作用的操作，因此不在本页触发——
 * 页面会显式说明，把选择权交回用户（页面上不做惊喜式写操作）。
 */
async function runReplay(): Promise<void> {
  const payload = replay.value
  if (!payload || replaying.value) return
  if (payload.kind === 'plan') {
    ElMessage.warning('计划类轨迹的重跑会新建一份计划，请在「新建计划」页按原需求执行')
    return
  }
  replaying.value = true
  replayResult.value = null
  try {
    const message = String(payload.input.message ?? '')
    const planId = (payload.input.planId as string | undefined) ?? undefined
    let final: AgentChatResult | null = null
    await api.agentChatStream(
      { message, planId, deferWrites: true },
      {
        // 流式接口不返回终态：终态只在 final 事件里下发，因此这里必须接住它
        onFinal: (result: AgentChatResult) => {
          final = result
        },
      },
    )
    replayResult.value = final
    if (final) {
      ElMessage.success('已按原请求重跑，可在下方对照两次运行')
      await loadList()
    } else {
      ElMessage.warning('重跑未收到终态事件，请打开新产生的轨迹查看过程')
    }
  } catch (error) {
    handleError(error, '重跑失败')
  } finally {
    replaying.value = false
  }
}

/** 打开沉淀对话框（标题可由用户覆盖，缺省由轨迹自动生成） */
function openSink(): void {
  const current = detail.value
  if (!current) return
  sinkTitle.value = `回归·${traceKindLabel(current.kind)}·${truncate(current.contentPreview || current.traceId, 40)}`
  sinkNote.value = ''
  sinkVisible.value = true
}

/**
 * 沉淀为回归用例。
 *
 * 断言不由人写：服务端从轨迹里的客观事实（失败的工具有哪些、护栏触发几次、反思归因到哪类）
 * 推导出门禁断言，因此「这条用例在防什么」永远与事故本身一致，不会随人事后遗忘而失真。
 */
async function confirmSink(): Promise<void> {
  const current = detail.value
  if (!current || sinking.value) return
  sinking.value = true
  try {
    const created: IncidentCaseResult = await api.createIncidentCase({
      traceId: current.traceId,
      title: sinkTitle.value || undefined,
      note: sinkNote.value || undefined,
    })
    sinkVisible.value = false
    detail.value = { ...current, incidentCaseId: created.case.id }
    ElMessage.success(`已沉淀回归用例 ${created.case.id}，将自动并入评测门禁（生效用例 ${created.effectiveCaseCount} 条）`)
  } catch (error) {
    handleError(error, '沉淀回归用例失败')
  } finally {
    sinking.value = false
  }
}

/** 取消沉淀（阻断态确认） */
async function cancelSink(): Promise<void> {
  try {
    await ElMessageBox.confirm('取消后本条轨迹的事故信号不会进入门禁，稍后仍可重新沉淀。', '确认取消沉淀？', {
      type: 'warning',
      confirmButtonText: '继续取消',
      cancelButtonText: '返回沉淀',
    })
    sinkVisible.value = false
  } catch {
    /* 用户选择返回：保持对话框 */
  }
}

/** 交付给子组件的行选择：支持从评测台失败用例按 traceId 直达 */
function rowClassName({ row }: { row: TraceSummary }): string {
  if (row.traceId === detail.value?.traceId) return 'ip-trace__row--active'
  return row.hasIncident ? 'ip-trace__row--incident' : ''
}

watch(
  () => [kindFilter.value, statusFilter.value],
  () => {
    void loadList()
  },
)

onMounted(async () => {
  await loadList()
  // 评测台入口：`traceId` 直达某条轨迹；`replay=1` 立即按原请求重跑对照；
  // `sink=1` 立即打开「沉淀为回归用例」—— 事故闭环的入口就在失败那一行
  const traceId = typeof route.query.traceId === 'string' ? route.query.traceId : ''
  if (!traceId) return
  await openTrace(traceId, route.query.replay === '1' ? 'replay' : undefined)
  if (route.query.sink === '1' && detail.value) openSink()
})
</script>

<template>
  <div class="ip-page">
    <el-alert v-if="disabledReason" type="warning" :closable="false" class="ip-trace__alert">
      <template #title>执行轨迹未启用</template>
      <div class="ip-trace__alert-body">
        {{ disabledReason }} 在 `.env` 中设置 `AGENT_TRACE_ENABLED=true` 后重启服务即可启用。
        开启后每次运行都会留下可回放的调用链，用于复盘「它当时到底做了什么」。
      </div>
    </el-alert>

    <StatTiles :items="tiles" />

    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>执行轨迹</span>
        <span class="ip-muted ip-mono">
          后端 {{ list?.backend || '—' }} · 事故信号由服务端确定性判定，前端只做展示
        </span>
        <el-button size="small" :icon="'Refresh'" @click="loadList">刷新</el-button>
      </div>

      <div class="ip-trace__controls">
        <el-radio-group v-model="kindFilter" size="small">
          <el-radio-button value="">全部类型</el-radio-button>
          <el-radio-button value="chat">会话</el-radio-button>
          <el-radio-button value="plan">端到端建计划</el-radio-button>
        </el-radio-group>
        <el-radio-group v-model="statusFilter" size="small">
          <el-radio-button value="">全部终态</el-radio-button>
          <el-radio-button v-for="status in list?.statuses ?? []" :key="status" :value="status">
            {{ traceRunStatusLabel(status) }}
          </el-radio-button>
        </el-radio-group>
        <el-tag v-if="statusFilter" size="small" effect="plain" type="info">
          客户端筛选：后端按类型返回，终态在本地过滤（列表行不含 span 明细，无需回源）
        </el-tag>
      </div>

      <el-table
        :data="statusFilter ? items.filter((item) => item.status === statusFilter) : items"
        size="small"
        border
        row-key="traceId"
        :row-class-name="rowClassName"
        v-loading="loadingList"
        @row-click="(row: TraceSummary) => openTrace(row.traceId)"
      >
        <el-table-column label="轨迹" min-width="280">
          <template #default="{ row }">
            <div class="ip-trace__row-head">
              <el-tag size="small" effect="plain" :type="traceRunStatusTag(row.status)">
                {{ traceRunStatusLabel(row.status) }}
              </el-tag>
              <el-tag size="small" effect="plain">{{ traceKindLabel(row.kind) }}</el-tag>
              <el-tag v-if="row.hasIncident" size="small" effect="plain" type="danger">含事故信号</el-tag>
              <el-tag v-if="row.incidentCaseId" size="small" effect="plain" type="success">
                已沉淀 {{ row.incidentCaseId }}
              </el-tag>
            </div>
            <div class="ip-trace__preview">{{ row.contentPreview || '（无结论文本）' }}</div>
            <div class="ip-mono ip-muted ip-trace__id">{{ row.traceId }}</div>
          </template>
        </el-table-column>
        <el-table-column label="工具链" min-width="200">
          <template #default="{ row }">
            <span v-if="row.toolChain.length === 0" class="ip-muted">未调用工具</span>
            <div v-else class="ip-trace__chain">
              <el-tag v-for="name in row.toolChain" :key="name" size="small" effect="plain">
                {{ name }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="步数" width="70" align="center">
          <template #default="{ row }"><span class="ip-mono">{{ row.steps }}</span></template>
        </el-table-column>
        <el-table-column label="时间片" width="80" align="center">
          <template #default="{ row }"><span class="ip-mono">{{ row.spanCount }}</span></template>
        </el-table-column>
        <el-table-column label="护栏 / 反思" width="110" align="center">
          <template #default="{ row }">
            <span class="ip-mono">
              <span :class="{ 'ip-trace__bad': row.guardCount > 0 }">{{ row.guardCount }}</span>
              / {{ row.reflectionCount }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="待确认" width="80" align="center">
          <template #default="{ row }">
            <span class="ip-mono" :class="{ 'ip-trace__warn': row.pendingCount > 0 }">{{ row.pendingCount }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="90" align="right">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ formatDuration(row.durationMs) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="150" align="right">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ formatTime(row.createdAt * 1000) }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="disabledReason ? '执行轨迹未启用' : '暂无轨迹（跑一次对话或评测即会生成）'" />
        </template>
      </el-table>
    </div>

    <div v-if="detail" v-loading="detailLoading" class="ip-panel">
      <div class="ip-panel__title">
        <span>调用链瀑布图</span>
        <span class="ip-muted ip-mono">
          {{ detail.traceId }} · 总时长 {{ formatDuration(detail.durationMs) }} · 存储上限
          {{ list?.backend || '—' }}
        </span>
        <div class="ip-trace__actions">
          <el-button size="small" :icon="'VideoPlay'" :loading="replaying" @click="runReplay">
            {{ replaying ? '重跑中…' : '按原请求重跑' }}
          </el-button>
          <el-button
            size="small"
            type="primary"
            :icon="'CollectionTag'"
            :disabled="!!detail.incidentCaseId"
            @click="openSink"
          >
            {{ detail.incidentCaseId ? `已沉淀 ${detail.incidentCaseId}` : '沉淀为回归用例' }}
          </el-button>
        </div>
      </div>

      <el-alert v-if="detail.error" type="error" :closable="false" class="ip-trace__alert">
        <template #title>编排失败（已落库，可复现）</template>
        <div class="ip-trace__alert-body">{{ detail.error }}</div>
      </el-alert>

      <div class="ip-trace__meta">
        <span class="ip-trace__meta-item">
          <span class="ip-muted">原始请求</span>
          <span class="ip-mono">{{ detail.request?.message || '（未记录）' }}</span>
        </span>
        <span v-if="detail.failedTools.length > 0" class="ip-trace__meta-item">
          <span class="ip-muted">失败工具</span>
          <span class="ip-mono ip-trace__bad">{{ detail.failedTools.join('、') }}</span>
        </span>
        <span v-if="detail.planId" class="ip-trace__meta-item">
          <span class="ip-muted">产出计划</span>
          <span class="ip-mono">{{ detail.planId }}</span>
        </span>
      </div>

      <div class="ip-trace__waterfall">
        <div
          v-for="span in detail.spans"
          :key="span.index"
          class="ip-trace__span"
          :class="{ 'ip-trace__span--active': span.index === selectedIndex }"
          @click="selectedIndex = span.index"
        >
          <div class="ip-trace__span-head">
            <span class="ip-mono ip-trace__span-index">{{ span.index }}</span>
            <el-tag size="small" effect="plain" :type="traceKindTag(span.kind)">{{ traceKindLabel(span.kind) }}</el-tag>
            <span class="ip-trace__span-title">{{ span.title }}</span>
            <el-tag size="small" effect="plain" :type="traceSpanStatusTag(span.status)">
              {{ traceSpanStatusLabel(span.status) }}
            </el-tag>
            <el-tag v-if="span.code" size="small" effect="plain" type="danger">{{ span.code }}</el-tag>
            <span class="ip-muted ip-trace__span-step">步 {{ span.step }}</span>
          </div>
          <div class="ip-trace__span-track">
            <div class="ip-trace__bar" :style="barStyle(span)" :class="{ 'ip-trace__bar--active': span.index === selectedIndex }">
              <span class="ip-trace__bar-fill" :style="{ background: barTone(span) }" />
              <span class="ip-trace__bar-label ip-mono">{{ formatDuration(span.durationMs) }}</span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="selectedIndex !== null" class="ip-trace__step">
        <div class="ip-trace__step-head">
          <span class="ip-trace__step-title">
            第 {{ selectedIndex }} 片：{{ traceKindLabel(detail.spans.find((s) => s.index === selectedIndex)?.kind ?? '') }}
          </span>
          <span class="ip-muted ip-mono">逐 step 回放：该时间片的输入 / 输出 / 说明</span>
        </div>
        <template v-for="span in detail.spans.filter((s) => s.index === selectedIndex)" :key="span.index">
          <div v-if="span.detail" class="ip-trace__text">{{ span.detail }}</div>
          <div class="ip-trace__io">
            <div class="ip-trace__io-block">
              <div class="ip-trace__io-label">输入</div>
              <pre class="ip-trace__code">{{ JSON.stringify(span.input, null, 2) }}</pre>
            </div>
            <div class="ip-trace__io-block">
              <div class="ip-trace__io-label">输出</div>
              <pre class="ip-trace__code">{{ JSON.stringify(span.output, null, 2) }}</pre>
            </div>
          </div>
        </template>
      </div>

      <div v-if="replay" class="ip-trace__replay">
        <div class="ip-trace__replay-head">
          <span>回放参数（原始请求原样重跑，无需重新手写需求）</span>
          <span class="ip-muted ip-mono">类型 {{ replay.kind }} · 原终态 {{ replay.status }}</span>
        </div>
        <pre class="ip-trace__code">{{ JSON.stringify(replay.input, null, 2) }}</pre>
        <div v-if="replayResult" class="ip-trace__replay-result">
          <div class="ip-trace__replay-line">
            <el-tag size="small" effect="plain" type="success">重跑完成</el-tag>
            <span class="ip-muted">新轨迹</span>
            <span class="ip-mono">{{ replayResult.traceId }}</span>
            <span class="ip-muted">步数 {{ replayResult.steps }} · 工具 {{ replayResult.toolCalls.length }}</span>
            <el-button size="small" text type="primary" @click="openTrace(replayResult.traceId || '')">
              打开新轨迹
            </el-button>
            <el-button size="small" text @click="router.push('/evals')">前往评测台</el-button>
          </div>
          <pre class="ip-trace__code">{{ replayResult.content }}</pre>
        </div>
        <el-alert v-else-if="replay.kind === 'plan'" type="info" :closable="false">
          <template #title>计划类轨迹不在本页重跑</template>
          <div class="ip-trace__alert-body">
            重跑会新建一份计划（真实写入），因此不在调试器里做「惊喜式写操作」。
            可复制上方的 requirement 到「新建计划」页执行。
          </div>
        </el-alert>
      </div>
    </div>

    <el-dialog v-model="sinkVisible" title="沉淀为回归用例" width="520px" append-to-body>
      <div class="ip-trace__sink">
        <p class="ip-trace__sink-desc">
          断言由服务端从本条轨迹的客观事实**确定性推导**：失败过的工具要求不再失败、触发过的护栏要求归零、
          反思归因到哪一类就断言哪一类。沉淀后立即并入评测运行集与 CI 门禁。
        </p>
        <div class="ip-trace__sink-field">
          <span class="ip-trace__sink-label">用例标题</span>
          <el-input v-model="sinkTitle" size="small" maxlength="200" placeholder="缺省由轨迹自动生成" />
        </div>
        <div class="ip-trace__sink-field">
          <span class="ip-trace__sink-label">备注（可留空）</span>
          <el-input v-model="sinkNote" size="small" type="textarea" :rows="2" maxlength="200" placeholder="例如：线上事故 INC-42" />
        </div>
        <div v-if="detail" class="ip-trace__sink-preview">
          <div class="ip-trace__io-label">即将登记的输入</div>
          <pre class="ip-trace__code">{{ JSON.stringify(detail.request, null, 2) }}</pre>
        </div>
      </div>
      <template #footer>
        <el-button size="small" @click="cancelSink">取消</el-button>
        <el-button size="small" type="primary" :loading="sinking" @click="confirmSink">登记并纳入门禁</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ip-trace__alert {
  margin-bottom: 12px;
}

.ip-trace__alert-body {
  margin-top: 4px;
  font-size: 12.5px;
  line-height: 1.7;
  word-break: break-word;
}

.ip-trace__controls {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.ip-trace__row-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.ip-trace__preview {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-secondary);
  word-break: break-word;
}

.ip-trace__id {
  margin-top: 2px;
  font-size: 11px;
}

.ip-trace__chain {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.ip-trace__bad {
  color: var(--ip-danger);
  font-weight: 600;
}

.ip-trace__warn {
  color: var(--ip-warning);
  font-weight: 600;
}

.ip-trace__row--incident :deep(td) {
  background: color-mix(in srgb, var(--ip-danger) 6%, transparent);
}

.ip-trace__row--active :deep(td) {
  background: color-mix(in srgb, var(--ip-text) 8%, transparent);
}

.ip-trace__actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}

.ip-trace__meta {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  font-size: 12px;
}

.ip-trace__meta-item {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  max-width: 100%;
  word-break: break-word;
}

.ip-trace__waterfall {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-trace__span {
  padding: 4px 6px;
  border-radius: 4px;
  cursor: pointer;
}

.ip-trace__span:hover {
  background: color-mix(in srgb, var(--ip-text) 5%, transparent);
}

.ip-trace__span--active {
  background: color-mix(in srgb, var(--ip-text) 9%, transparent);
}

.ip-trace__span-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}

.ip-trace__span-index {
  width: 22px;
  font-size: 11px;
  color: var(--ip-text-muted);
}

.ip-trace__span-title {
  font-weight: 600;
  color: var(--ip-text);
}

.ip-trace__span-step {
  font-size: 11px;
  margin-left: auto;
}

.ip-trace__span-track {
  position: relative;
  height: 12px;
  margin: 2px 0 0 30px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--ip-text) 6%, transparent);
}

.ip-trace__bar {
  position: absolute;
  top: 0;
  height: 12px;
  border-radius: 6px;
  overflow: visible;
}

.ip-trace__bar-fill {
  display: block;
  height: 12px;
  border-radius: 6px;
  opacity: 0.85;
}

.ip-trace__bar--active .ip-trace__bar-fill {
  opacity: 1;
}

.ip-trace__bar-label {
  position: absolute;
  top: -1px;
  left: calc(100% + 6px);
  font-size: 10.5px;
  white-space: nowrap;
  color: var(--ip-text-muted);
}

.ip-trace__step,
.ip-trace__replay {
  margin-top: 14px;
  padding: 10px 12px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
}

.ip-trace__step-head,
.ip-trace__replay-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.ip-trace__step-title,
.ip-trace__replay-head span:first-child {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ip-text);
}

.ip-trace__text {
  font-size: 12px;
  line-height: 1.75;
  color: var(--ip-text-secondary);
  word-break: break-word;
}

.ip-trace__io {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 8px;
}

.ip-trace__io-block {
  flex: 1 1 280px;
  min-width: 240px;
}

.ip-trace__io-label {
  margin-bottom: 4px;
  font-size: 11.5px;
  color: var(--ip-text-muted);
}

.ip-trace__code {
  max-height: 260px;
  margin: 0;
  padding: 8px 10px;
  overflow: auto;
  border: 1px solid var(--ip-border);
  border-radius: 4px;
  background: var(--ip-surface);
  font-family: var(--ip-font-mono);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ip-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.ip-trace__replay-result {
  margin-top: 10px;
}

.ip-trace__replay-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
  font-size: 12px;
}

.ip-trace__sink {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ip-trace__sink-desc {
  margin: 0;
  font-size: 12px;
  line-height: 1.75;
  color: var(--ip-text-secondary);
}

.ip-trace__sink-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ip-trace__sink-label {
  font-size: 11.5px;
  color: var(--ip-text-muted);
}
</style>
