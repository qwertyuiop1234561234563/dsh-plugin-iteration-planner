<script setup lang="ts">
/**
 * AI 助手（全局悬浮球 + 会话抽屉）。
 *
 * 能力与边界：
 *  - **对话与问答**：走后端 `/api/agent/chat`（多轮会话）。模型只能调用内部算法引擎工具，
 *    所有数值均来自引擎返回值，前端只做展示；
 *  - **写操作两阶段确认**：后端把「加任务 / 改任务 / 删任务 / 重规划 / 生成计划」登记为
 *    待确认动作**不落库**，本抽屉以确认卡片呈现，用户点「确认执行」后才调用
 *    `/api/agent/confirm` 真正写入，点「驳回」则调用 reject 分支作废（不执行任何写操作）；
 *  - **规则降级**：未配置 LLM 网关（`/agent/health` 的 `enabled=false`）时，自动切换为
 *    内置规则问答（`utils/agentFaq.ts`），不白屏、不静默失败；
 *  - **计划上下文**：把「当前正在查看的计划」随每轮消息上报（后端做白名单校验后作为可信
 *    上下文注入），因此「给这个计划加个任务」这类指代能落到正确的 planId 上；
 *  - **进阶能力工作台**：抽屉顶部可在「对话 / 多角色协作 / What-if 沙盘 / 迭代复盘 / 主动巡检」
 *    之间切换，后四者由 :component:`AgentWorkbench` 呈现（角色协作步骤流可中断、沙盘与复盘
 *    只调引擎不重算算法、巡检展示后台 Agent 的增量预警）。写类入口按 `agent:write` 权限收敛。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type {
  AgentHealth,
  AgentPendingAction,
  AgentReflection,
  AgentToolCallRecord,
  KbHit,
  MemoryItem,
} from '@/api/types'
import AgentWorkbench, { type WorkbenchMode } from '@/components/AgentWorkbench.vue'
import ReflectionTrace from '@/components/ReflectionTrace.vue'
import { useAuthStore } from '@/stores/auth'
import { useNotifyStore } from '@/stores/notify'
import { usePlanStore } from '@/stores/plan'
import { FAQ_FALLBACK, faqPrompts, matchFaq } from '@/utils/agentFaq'
import { agentToolLabel, truncate } from '@/utils/format'

const props = defineProps<{
  /** 当前正在查看的计划 ID（随对话上报，供模型解析指代） */
  planId?: string
  /** 当前计划的需求摘要（仅用于抽屉头部展示） */
  planLabel?: string
}>()

const visible = defineModel<boolean>({ default: false })

const router = useRouter()
const store = usePlanStore()
const notify = useNotifyStore()
const auth = useAuthStore()

/** 抽屉模式：chat（会话）| 进阶能力工作台四种 */
const mode = ref<'chat' | WorkbenchMode>('chat')

const MODE_OPTIONS: { label: string; value: 'chat' | WorkbenchMode }[] = [
  { label: '对话', value: 'chat' },
  { label: '多角色协作', value: 'collab' },
  { label: 'What-if 沙盘', value: 'whatif' },
  { label: '迭代复盘', value: 'retro' },
  { label: '主动巡检', value: 'patrol' },
]

/** 工作台模式（null 表示会话模式） */
const workbenchMode = computed<WorkbenchMode | null>(() => (mode.value === 'chat' ? null : mode.value))

/** 前端展示收敛：写能力以服务端权限为准（真正的拦截始终在服务端） */
const canAgentWrite = computed(() => auth.can('agent:write'))
const canMemoryWrite = computed(() => auth.can('memory:write'))

/** 会话消息（工具中间态与待确认动作挂在对应助手消息上，随消息一起滚动） */
interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  ts: number
  /** 消息来源：llm（模型编排）/ rule（内置规则问答）/ error（调用失败） */
  mode?: 'llm' | 'rule' | 'error'
  /** 本轮工具调用明细（只读工具即时执行的结果回填记录） */
  toolCalls?: AgentToolCallRecord[]
  /**
   * 本轮自我诊断轨迹：工具失败 / 检索空结果时 Agent 的确定性与纠偏动作。
   *
   * 与 `toolCalls` 分开呈现 —— 工具失败是「发生了什么」，反思是「Agent 判断为什么、
   * 打算怎么改、改成功没有」。把后者显式画出来，才看得出这是自愈闭环而不是干等重试。
   */
  reflections?: AgentReflection[]
  /** 本轮对应的运行台账 ID：有待确认动作或编排失败时出现，可在「运行台账」页继续/重跑 */
  checkpointId?: string
  /** 待用户确认的写操作 */
  pending?: AgentPendingAction[]
  /** 结果关联的计划（可一键跳转） */
  planId?: string
  /** 规则问答命中的页面跳转建议（路由名） */
  routeName?: string
  /** 本轮实际引用到的知识库片段（RAG 依据，供用户核对） */
  references?: KbHit[]
  /** 本轮注入上下文的长期记忆（供用户纠偏） */
  memories?: MemoryItem[]
  /** 是否仍在流式生成中（用于展示光标与「停止生成」） */
  streaming?: boolean
}

const messages = ref<ChatMessage[]>([])
const sessionId = ref('')
const draft = ref('')
const sending = ref(false)
const confirming = ref(false)
const health = ref<AgentHealth | null>(null)
const healthLoading = ref(false)
const scroller = ref<HTMLElement>()
/** 当前流式请求的中断控制器（「停止生成」用） */
const abortController = ref<AbortController | null>(null)

let messageSeq = 0

/** 是否具备模型编排能力（未配置时降级为规则问答） */
const llmReady = computed(() => health.value?.enabled === true)

/** 助手模式文案 */
const modeText = computed(() => {
  if (healthLoading.value && !health.value) return '检测中…'
  return llmReady.value ? `模型：${health.value?.llm ?? '已配置'}` : '规则问答模式（未配置 LLM）'
})

/** 待确认动作总数（悬浮球角标） */
const pendingCount = computed(() =>
  messages.value.reduce((total, message) => total + (message.pending?.length ?? 0), 0),
)

/** 快捷提问：有模型时用开放式问题，降级时用内置问答条目 */
const quickPrompts = computed<string[]>(() => {
  if (!llmReady.value) return faqPrompts(!!props.planId).map((entry) => entry.question)
  return props.planId
    ? [
        '当前计划有哪些风险？',
        '关键路径上有哪些任务，瓶颈在哪里？',
        '给当前计划补一个接口联调任务',
        '把当前计划的开发人数调整为 2 人',
      ]
    : ['列出全部计划', '根据需求帮我生成一个迭代计划', '关键路径是怎么算出来的？', '系统会检测哪些风险？']
})

function push(partial: Omit<ChatMessage, 'id' | 'ts'>): ChatMessage {
  messageSeq += 1
  messages.value = [...messages.value, { id: `M-${messageSeq}`, ts: Date.now(), ...partial }]
  // 必须返回**响应式代理**而非原始对象：流式增量（content += delta）只有经代理写入才会触发重渲染
  return messages.value[messages.value.length - 1]
}

/** 追加一条错误消息（任何失败都可见，不静默） */
function pushError(text: string): void {
  push({ role: 'assistant', mode: 'error', content: text })
}

/** 从消息流中移除指定消息（用于撤销空的流式占位消息） */
function removeMessage(id: string): void {
  messages.value = messages.value.filter((item) => item.id !== id)
}

/** 滚动到底部（流式追加时每次增量后调用，否则打字机内容会溢出可视区） */
async function scrollToBottom(): Promise<void> {
  await nextTick()
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
}

watch(() => messages.value.length, () => void scrollToBottom())

/** 拉取 Agent 可用性（LLM 是否已配置；决定问答走模型还是内置规则） */
async function loadHealth(): Promise<void> {
  healthLoading.value = true
  try {
    health.value = await api.agentHealth()
  } catch (error) {
    health.value = null
    pushError(`无法获取 AI 助手状态：${error instanceof ApiClientError ? error.message : String(error)}`)
  } finally {
    healthLoading.value = false
  }
}

/** 规则问答：命中内置条目则作答，未命中给出兜底与配置指引 */
function answerByRule(text: string): void {
  const matched = matchFaq(text)
  if (!matched) {
    push({ role: 'assistant', mode: 'rule', content: FAQ_FALLBACK })
    return
  }
  const { entry, answer } = matched
  const canJump = canJumpTo(entry.routeName)
  push({
    role: 'assistant',
    mode: 'rule',
    content: canJump ? `${answer}\n\n可点击下方按钮直接跳转到对应页面。` : answer,
    routeName: canJump ? entry.routeName : undefined,
  })
}

/** 该路由是否可跳转：全局页面始终可去；计划级页面需当前已打开计划 */
function canJumpTo(routeName?: string): boolean {
  if (!routeName) return false
  return !routeName.startsWith('plan-') || routeName === 'plan-create' || !!props.planId
}

/** LLM 侧不可用的错误码（走到这里说明模型链路整体不可用，应降级为规则问答） */
const LLM_DOWN_CODES = new Set(['LLM_UNAVAILABLE', 'LLM_GATEWAY_ERROR'])

/**
 * 发送问题：走 **SSE 流式**编排（逐字追加渲染）。
 *
 * 事件与 UI 的对应关系：``meta`` → 会话 ID + 引用 / 记忆卡片；``delta`` → 文本增量；
 * ``tool_result`` → 工具调用明细；``pending`` → 待确认写操作；``final`` → 权威结果补全。
 * 全程可「停止生成」（AbortController），失败时保留已生成内容并给出明确提示。
 */
async function send(raw?: string): Promise<void> {
  const content = (raw ?? draft.value).trim()
  if (!content || sending.value) return
  draft.value = ''
  push({ role: 'user', content })
  if (!llmReady.value) {
    // 未配置 LLM：直接走内置规则问答（不发起无效请求）
    answerByRule(content)
    return
  }
  sending.value = true
  const controller = new AbortController()
  abortController.value = controller
  // 先占位渲染助手气泡：首帧之前展示「编排中」提示，避免出现无反馈等待
  const assistant = push({ role: 'assistant', mode: 'llm', content: '', streaming: true })
  try {
    await api.agentChatStream(
      { message: content, sessionId: sessionId.value || undefined, planId: props.planId || undefined },
      {
        onMeta: (meta) => {
          sessionId.value = meta.sessionId
          // 引用 / 记忆随首帧到达即渲染，用户可在生成过程中查看依据
          if (meta.references.length > 0) assistant.references = meta.references
          if (meta.memories.length > 0) assistant.memories = meta.memories
        },
        onDelta: (text) => {
          if (!text) return
          assistant.content += text
          void scrollToBottom()
        },
        onToolResult: (record) => {
          assistant.toolCalls = [...(assistant.toolCalls ?? []), record]
        },
        onReflect: (reflection) => {
          // 即时追加：用户能看到「Agent 正在自己纠正」，而不是等最终结果才知道发生过失败
          assistant.reflections = [...(assistant.reflections ?? []), reflection]
        },
        onPending: (actions) => {
          if (actions.length > 0) assistant.pending = actions
        },
        onFinal: (result) => {
          sessionId.value = result.sessionId
          // final 是**权威结果**：服务端已在终态做过事实校验（剔除并不存在的 planId、把虚假的
          // 「已完成」声明覆盖为确定性文案）。因此这里**整体替换**流式草稿——只在草稿为空时兜底
          // 会让被服务端纠正过的内容继续留在界面上，用户看到的仍是那句「已成功…」。
          if (result.content) assistant.content = result.content
          assistant.toolCalls = result.toolCalls.length > 0 ? result.toolCalls : assistant.toolCalls
          assistant.pending = result.pendingActions.length > 0 ? result.pendingActions : undefined
          // 反思轨迹以终态为准：服务端在收敛时已把「是否恢复」按后续调用结果回填完毕
          assistant.reflections = result.reflections && result.reflections.length > 0 ? result.reflections : assistant.reflections
          assistant.checkpointId = result.checkpointId ?? undefined
          assistant.planId = result.planId ?? undefined
          if (result.references && result.references.length > 0) assistant.references = result.references
          if (result.memories && result.memories.length > 0) assistant.memories = result.memories
        },
      },
      controller.signal,
    )
  } catch (error) {
    handleStreamFailure(assistant, content, error)
  } finally {
    assistant.streaming = false
    abortController.value = null
    sending.value = false
  }
}

/** 停止生成：中断 SSE 连接（已产出的内容保留） */
function stopGenerating(): void {
  abortController.value?.abort()
}

/** 流式失败处理：区分「用户取消 / 部分内容已产出 / 模型链路不可用」三种情形 */
function handleStreamFailure(message: ChatMessage, prompt: string, error: unknown): void {
  const apiError = error instanceof ApiClientError ? error : null
  if (apiError?.code === 'ABORTED') {
    // 用户主动停止：保留已生成内容；若一个字都没产出则移除空气泡
    if (!message.content) removeMessage(message.id)
    return
  }
  const reason = apiError?.message ?? String(error)
  if (message.content) {
    // 已产出部分内容：保留并追加中断说明，避免用户已看到的答案被清空
    message.content += `\n\n（响应中断：${reason}）`
    return
  }
  if (apiError && LLM_DOWN_CODES.has(apiError.code)) {
    // 模型链路不可用（未配置 / 网关不可达 / 上游异常）：即时降级，避免用户卡在报错上
    health.value = health.value ? { ...health.value, enabled: false } : null
    removeMessage(message.id)
    pushError(
      `LLM 网关当前不可用（${apiError.message}），已降级为内置规则问答。修复模型网关配置后重新打开抽屉即可恢复模型问答。`,
    )
    answerByRule(prompt)
    return
  }
  message.mode = 'error'
  message.content = reason
}

/** Enter 发送（Shift + Enter 换行由 `.exact` 排除）
 *
 * 中文输入法组合态（拼音尚未上屏，``isComposing`` 为真）时不发送，也不拦截默认行为，
 * 否则用户在候选词阶段按 Enter 会把半截拼音当成问题提交。
 */
function onEnterKey(event: KeyboardEvent): void {
  if (event.isComposing) return
  event.preventDefault()
  void send()
}

/** 确认执行待确认的写操作，并按执行结果刷新平台数据 */
async function confirmPending(message: ChatMessage): Promise<void> {
  const actions = message.pending ?? []
  if (!sessionId.value || actions.length === 0 || confirming.value) return
  confirming.value = true
  try {
    const result = await api.agentConfirm(sessionId.value)
    message.pending = result.remainingActions.length > 0 ? result.remainingActions : undefined
    const succeeded = result.executed.filter((record) => record.ok)
    const failed = result.executed.filter((record) => !record.ok)
    const lines = result.executed.map(
      (record) => `- ${agentToolLabel(record.name)}：${record.ok ? '已执行' : `失败（${record.error ?? '未知原因'}）`}`,
    )
    push({
      role: 'assistant',
      mode: 'llm',
      content:
        result.executed.length === 0
          ? '没有可执行的写操作。'
          : `已执行 ${succeeded.length}/${result.executed.length} 项写操作，算法引擎已完成全量重规划：\n${lines.join('\n')}`,
      toolCalls: result.executed,
      planId: result.planId ?? undefined,
    })
    // 写操作已落库：刷新计划列表与当前计划（引擎同时会 WebSocket 广播，store 侧已去重）
    await store.loadPlans()
    if (result.planId && store.currentPlanId === result.planId) await store.refreshCurrent()
    notify.push({
      level: failed.length > 0 ? 'warning' : 'success',
      title: 'AI 助手写操作已执行',
      detail: `${succeeded.length}/${result.executed.length} 项成功${failed.length > 0 ? ` · ${failed.length} 项失败` : ''}`,
      planId: result.planId ?? undefined,
    })
    if (failed.length > 0) ElMessage.warning(`有 ${failed.length} 项写操作执行失败，详见会话记录`)
    else ElMessage.success('写操作已执行，计划已重新规划')
  } catch (error) {
    pushError(`确认执行失败：${error instanceof ApiClientError ? error.message : String(error)}`)
  } finally {
    confirming.value = false
  }
}

/** 驳回待确认的写操作（作废，不执行；会话上下文保留） */
async function rejectPending(message: ChatMessage): Promise<void> {
  const count = message.pending?.length ?? 0
  if (!sessionId.value || count === 0 || confirming.value) return
  confirming.value = true
  try {
    await api.agentReject(sessionId.value)
    message.pending = undefined
    push({ role: 'assistant', mode: 'llm', content: `已驳回 ${count} 项写操作，计划未被修改。` })
  } catch (error) {
    pushError(`驳回失败：${error instanceof ApiClientError ? error.message : String(error)}`)
  } finally {
    confirming.value = false
  }
}

/** 清空会话：同时清掉后端上下文与本地消息 */
async function clearSession(): Promise<void> {
  const current = sessionId.value
  sessionId.value = ''
  messages.value = []
  if (!current) return
  try {
    await api.agentResetSession(current)
  } catch {
    // 会话可能已过期：清空本地即可，无需打扰用户
  }
}

/** 跳转到规则问答建议的页面 */
function jumpByRoute(message: ChatMessage): void {
  const routeName = message.routeName
  if (!routeName) return
  if (routeName === 'plan-create') {
    void router.push({ name: 'plan-create' })
  } else if (props.planId) {
    void router.push({ name: routeName, params: { planId: props.planId } })
  } else {
    ElMessage.warning('请先打开一个迭代计划，再跳转到该视图')
    return
  }
  visible.value = false
}

/** 跳转到对话结果关联的计划 */
function openPlan(planId: string): void {
  void router.push({ name: 'plan-overview', params: { planId } })
  visible.value = false
}

/** 工具调用是否有失败项（折叠标题上做失败标记） */
function hasFailedCall(message: ChatMessage): boolean {
  return (message.toolCalls ?? []).some((record) => !record.ok)
}

/** 跳转运行台账页（断点续跑）：台账是持久化的，关掉抽屉 / 重启服务后依然可在此继续 */
function openCheckpoints(): void {
  void router.push({ name: 'checkpoints' })
  visible.value = false
}

onMounted(() => {
  void loadHealth()
})

// 打开抽屉时同步一次可用性（用户可能在抽屉外配置了 LLM 或切换了服务）
watch(visible, (open) => {
  if (open) void loadHealth()
})
</script>

<template>
  <div>
    <!-- 悬浮球：任意页面可唤起（角标为待确认写操作数量） -->
    <el-badge :value="pendingCount" :hidden="pendingCount === 0" type="warning" class="ip-agent-ball__badge">
      <button class="ip-agent-ball" type="button" title="AI 助手（Ctrl / ⌘ + Shift + A）" @click="visible = true">
        <el-icon :size="20"><MagicStick /></el-icon>
      </button>
    </el-badge>

    <el-drawer v-model="visible" title="AI 助手" size="620px" append-to-body destroy-on-close>
      <div class="ip-agent">
        <div class="ip-agent__meta">
          <el-tag size="small" effect="plain" :type="llmReady ? 'success' : 'info'">{{ modeText }}</el-tag>
          <el-tag v-if="planId" size="small" effect="plain" type="info">
            当前计划：{{ truncate(planLabel || planId, 20) }}
          </el-tag>
          <el-tag v-if="health?.knowledgeEnabled" size="small" effect="plain" type="success">知识库已启用</el-tag>
          <el-tag v-if="health?.memoryEnabled" size="small" effect="plain" :type="health?.memoryAutoExtract ? 'success' : 'info'">
            记忆{{ health?.memoryAutoExtract ? '自动提取' : '已启用' }}
          </el-tag>
          <span class="ip-agent__spacer" />
          <el-button
            size="small"
            :icon="'Delete'"
            :disabled="mode !== 'chat' || messages.length === 0"
            @click="clearSession"
          >
            清空会话
          </el-button>
        </div>

        <!-- 模式切换：对话 + 四项进阶能力（工作台见 AgentWorkbench） -->
        <el-radio-group v-model="mode" size="small" class="ip-agent__modes">
          <el-radio-button v-for="option in MODE_OPTIONS" :key="option.value" :value="option.value">
            {{ option.label }}
          </el-radio-button>
        </el-radio-group>

        <template v-if="!workbenchMode">
        <el-alert v-if="!llmReady && !healthLoading" type="info" :closable="false" class="ip-agent__notice">
          未配置 LLM 网关，当前为<strong>内置规则问答</strong>模式：可回答算法口径与平台功能问题，并给出页面跳转。
          配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 后重启服务即可启用模型问答与「改计划」能力；
          配置 EMBEDDING_API_KEY（或复用 LLM_API_KEY）后可启用知识库引用与长期记忆。
        </el-alert>

        <div ref="scroller" class="ip-agent__stream">
          <el-empty v-if="messages.length === 0" :image-size="72" description="问我计划相关的问题，或让我帮你改计划（写操作会先请你确认）" />

          <div v-for="message in messages" :key="message.id" class="ip-agent__msg" :class="`is-${message.role}`">
            <div class="ip-agent__bubble">
              <pre class="ip-agent__text">{{ message.content }}<span v-if="message.streaming" class="ip-agent__caret" aria-hidden="true" /></pre>
              <span v-if="message.streaming && !message.content" class="ip-agent__typing">正在调用算法引擎工具编排中…</span>

              <!-- 待确认写操作卡片：确认后才真正落库 -->
              <div v-if="message.pending && message.pending.length > 0" class="ip-agent__pending">
                <div class="ip-agent__pending-head">
                  <el-icon><WarningFilled /></el-icon>
                  <span>待确认的写操作（{{ message.pending.length }} 项）— 确认前不会修改任何数据</span>
                </div>
                <ul class="ip-agent__pending-list">
                  <li v-for="action in message.pending" :key="action.actionId">
                    <el-tag size="small" effect="plain">{{ agentToolLabel(action.tool) }}</el-tag>
                    <span>{{ action.label }}</span>
                  </li>
                </ul>
                <div class="ip-agent__pending-actions">
                  <el-button type="primary" size="small" :loading="confirming" @click="confirmPending(message)">确认执行</el-button>
                  <el-button size="small" :disabled="confirming" @click="rejectPending(message)">驳回</el-button>
                </div>
              </div>

              <!-- 规则问答的页面跳转建议 -->
              <div v-if="message.routeName" class="ip-agent__actions">
                <el-button size="small" type="primary" plain @click="jumpByRoute(message)">前往对应页面</el-button>
              </div>

              <!-- 对话结果关联的计划 -->
              <div v-else-if="message.planId" class="ip-agent__actions">
                <el-button size="small" type="primary" plain @click="openPlan(message.planId)">查看计划总览</el-button>
              </div>

              <!-- 参考依据：本轮实际命中并注入的知识库片段（便于核对结论来源） -->
              <div v-if="message.references && message.references.length > 0" class="ip-agent__context">
                <div class="ip-agent__context-head">
                  <el-icon><Collection /></el-icon>
                  <span>参考知识库片段（{{ message.references.length }}）</span>
                </div>
                <div v-for="(ref, index) in message.references" :key="ref.chunkId ?? index" class="ip-agent__context-item">
                  <div class="ip-agent__context-title">
                    <span class="ip-agent__context-name">{{ ref.title || ref.docId || '未命名文档' }}</span>
                    <span class="ip-agent__context-score ip-mono">score {{ ref.score.toFixed(4) }}</span>
                  </div>
                  <div class="ip-agent__context-text">{{ truncate(ref.text, 200) }}</div>
                </div>
              </div>

              <!-- 本轮注入上下文的长期记忆（发现跑偏可去「长期记忆」页停用） -->
              <div v-if="message.memories && message.memories.length > 0" class="ip-agent__context">
                <div class="ip-agent__context-head">
                  <el-icon><Star /></el-icon>
                  <span>本轮召回记忆（{{ message.memories.length }}）</span>
                </div>
                <div v-for="item in message.memories" :key="item.memoryId" class="ip-agent__context-item">
                  <div class="ip-agent__context-text">{{ truncate(item.text, 200) }}</div>
                </div>
              </div>

              <!-- 工具调用过程（只读工具即时执行；写工具此处显示为待确认） -->
              <el-collapse v-if="message.toolCalls && message.toolCalls.length > 0" class="ip-agent__calls">
                <el-collapse-item :name="message.id">
                  <template #title>
                    <span class="ip-agent__calls-title">
                      工具调用 {{ message.toolCalls.length }} 次
                      <el-tag v-if="hasFailedCall(message)" size="small" type="danger" effect="plain">含失败项</el-tag>
                    </span>
                  </template>
                  <div v-for="(call, index) in message.toolCalls" :key="index" class="ip-agent__call">
                    <el-tag size="small" effect="plain" :type="call.ok ? 'success' : 'danger'">
                      {{ call.ok ? '成功' : '失败' }}
                    </el-tag>
                    <span class="ip-agent__call-name">{{ agentToolLabel(call.name) }}</span>
                    <span v-if="call.error" class="ip-agent__call-err">{{ call.error }}</span>
                  </div>
                </el-collapse-item>
              </el-collapse>

              <!-- 自我诊断轨迹：Agent 自己发现失败原因并纠偏的过程（确定性归因，非模型自述） -->
              <ReflectionTrace
                v-if="message.reflections && message.reflections.length > 0"
                :reflections="message.reflections"
                :title="`reflect-${message.id}`"
              />

              <!-- 本轮已登记运行台账：可在「运行台账」页断点续跑（重启 / 换副本后依然在） -->
              <div v-if="message.checkpointId" class="ip-agent__ledger">
                <el-icon><Files /></el-icon>
                <span>本次运行已登记台账</span>
                <el-button size="small" text type="primary" @click="openCheckpoints">去续跑</el-button>
              </div>
            </div>
            <div class="ip-agent__role">{{ message.role === 'user' ? '我' : 'AI 助手' }}</div>
          </div>
        </div>

        <div v-if="messages.length === 0" class="ip-agent__prompts">
          <span class="ip-agent__prompts-label">试试这样问：</span>
          <el-button v-for="prompt in quickPrompts" :key="prompt" size="small" text bg @click="send(prompt)">{{ prompt }}</el-button>
        </div>

        <div class="ip-agent__composer">
          <el-input
            v-model="draft"
            type="textarea"
            :rows="3"
            resize="none"
            :disabled="sending"
            placeholder="描述你的诉求，例如：当前计划有哪些高风险？ / 给当前计划补一个接口联调任务"
            @keydown.enter.exact="onEnterKey($event)"
          />
          <div class="ip-agent__composer-foot">
            <span class="ip-agent__hint">Enter 发送 · Shift + Enter 换行 · 写操作需确认后执行</span>
            <el-button v-if="sending" size="small" :icon="'VideoPause'" @click="stopGenerating">停止生成</el-button>
            <el-button type="primary" size="small" :icon="'Promotion'" :loading="sending" @click="send()">发送</el-button>
          </div>
        </div>
        </template>

        <!-- 进阶能力工作台：协作 / 沙盘 / 复盘 / 巡检（数值全部来自后端与算法引擎） -->
        <div v-else class="ip-agent__workbench">
          <AgentWorkbench
            :mode="workbenchMode"
            :plan-id="props.planId"
            :plan-label="props.planLabel"
            :can-write="canAgentWrite"
            :can-write-memory="canMemoryWrite"
            @open-plan="openPlan"
          />
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* 悬浮球：固定在右下角，避开侧栏与内容区 */
.ip-agent-ball__badge {
  position: fixed;
  right: 26px;
  bottom: 26px;
  z-index: 20;
}

.ip-agent-ball {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border: 1px solid var(--ip-border-strong);
  border-radius: 50%;
  color: #fff;
  cursor: pointer;
  background: linear-gradient(135deg, var(--ip-brand) 0%, var(--ip-accent) 100%);
  box-shadow: var(--ip-shadow);
  transition: transform 0.16s ease, box-shadow 0.16s ease;
}

.ip-agent-ball:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 10px rgba(10, 34, 54, 0.22), 0 18px 36px -18px rgba(10, 34, 54, 0.5);
}

.ip-agent {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 420px;
}

.ip-agent__spacer {
  flex: 1;
}

.ip-agent__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.ip-agent__notice {
  margin-bottom: 10px;
}

/* 模式切换（对话 / 协作 / 沙盘 / 复盘 / 巡检） */
.ip-agent__modes {
  margin-bottom: 10px;
  flex-wrap: wrap;
}

/* 进阶能力工作台：撑满抽屉剩余高度并独立滚动 */
.ip-agent__workbench {
  flex: 1;
  overflow: auto;
  padding-right: 4px;
  min-height: 220px;
}

/* 消息流：撑满中部，独立滚动 */
.ip-agent__stream {
  flex: 1;
  overflow: auto;
  padding-right: 4px;
  min-height: 220px;
}

.ip-agent__msg {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 14px;
}

.ip-agent__msg.is-user {
  align-items: flex-end;
}

.ip-agent__bubble {
  max-width: 96%;
  padding: 9px 12px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius-lg);
  background: var(--ip-surface-sunk);
}

.ip-agent__msg.is-user .ip-agent__bubble {
  background: color-mix(in srgb, var(--ip-brand) 10%, transparent);
  border-color: color-mix(in srgb, var(--ip-brand) 28%, transparent);
}

.ip-agent__msg.is-assistant .ip-agent__bubble {
  background: var(--ip-surface);
}

.ip-agent__text {
  margin: 0;
  font-family: var(--ip-font-sans);
  font-size: 13px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--ip-text);
}

.ip-agent__role {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ip-text-muted);
}

/* 待确认写操作卡片 */
.ip-agent__pending {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid color-mix(in srgb, var(--ip-signal) 45%, transparent);
  border-radius: var(--ip-radius);
  background: color-mix(in srgb, var(--ip-signal) 8%, transparent);
}

.ip-agent__pending-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ip-text);
}

.ip-agent__pending-list {
  margin: 8px 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.9;
  color: var(--ip-text);
}

.ip-agent__pending-list li {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.ip-agent__pending-actions {
  display: flex;
  gap: 8px;
}

.ip-agent__actions {
  margin-top: 10px;
}

/* 参考依据 / 召回记忆卡片 */
.ip-agent__context {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-agent__context-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ip-text-secondary);
}

.ip-agent__context-item {
  padding: 6px 0 0;
}

.ip-agent__context-item + .ip-agent__context-item {
  margin-top: 4px;
  border-top: 1px dashed var(--ip-border);
  padding-top: 8px;
}

.ip-agent__context-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 2px;
}

.ip-agent__context-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--ip-text);
}

.ip-agent__context-score {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--ip-text-muted);
}

.ip-agent__context-text {
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-secondary);
  white-space: normal;
  word-break: break-word;
}

/* 工具调用明细 */
.ip-agent__calls {
  margin-top: 8px;
  --el-collapse-header-bg-color: transparent;
  --el-collapse-content-bg-color: transparent;
  --el-collapse-border-color: var(--ip-border);
}

.ip-agent__calls-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ip-text-secondary);
}

.ip-agent__call {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 0;
  font-size: 12px;
  color: var(--ip-text);
}

.ip-agent__call-name {
  font-weight: 600;
}

.ip-agent__call-err {
  color: var(--ip-danger);
}

/* 台账提示条：本轮有待确认动作 / 编排失败时出现，引导去「运行台账」页续跑 */
.ip-agent__ledger {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding: 6px 10px;
  border: 1px dashed var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
  font-size: 12px;
  color: var(--ip-text-muted);
}

.ip-agent__typing {
  font-size: 12.5px;
  color: var(--ip-text-muted);
}

/* 流式输出光标：跟随在已生成文本末尾，随内容增长自然下移 */
.ip-agent__caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: currentColor;
  animation: ip-agent-blink 1s steps(2, start) infinite;
}

@keyframes ip-agent-blink {
  to {
    visibility: hidden;
  }
}

/* 快捷提问 */
.ip-agent__prompts {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 0 4px;
  border-top: 1px dashed var(--ip-border);
  margin-top: 8px;
}

.ip-agent__prompts-label {
  font-size: 12px;
  color: var(--ip-text-muted);
}

/* 输入区 */
.ip-agent__composer {
  margin-top: 8px;
}

.ip-agent__composer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}

.ip-agent__hint {
  font-size: 11px;
  color: var(--ip-text-muted);
}
</style>
