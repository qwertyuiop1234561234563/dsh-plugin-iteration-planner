<script setup lang="ts">
/**
 * Agent 评测台（黄金集 + 双轨打分 + CI 门禁）。
 *
 * 这一页回答的是「**改完之后 Agent 到底变好了还是变坏了**」。没有它，任何 Prompt / 工具 /
 * 编排调整都只能靠手点几下凭感觉判断；有了它，改动的收益与代价是可复算的数字。
 *
 * 打分是**双轨**的：
 *  - **确定性断言**（硬门槛）：由服务端对可观测事实做断言（必须调用了哪些工具、困难路径是否
 *    出现、是否谎报完成、是否产出了真实计划 ID……），不通过即判例失败；
 *  - **LLM 评委**：只做补充性质量评分，权重较低。评委缺席（未配置 / 输出不可解析）记为
 *    `null` 而**不是 0 分** —— 否则「没接模型」会被误读成「质量很差」。
 *
 * 两个展示口径上的边界：
 *  - `error`（环境不具备，例如没接 LLM）**不进通过率分母**：环境缺 ≠ Agent 退化，否则没配
 *    LLM 的环境必然红灯，门禁会被无视；
 *  - 评测会真实驱动 Agent 跑多轮 LLM + 引擎调用，因此**默认走异步作业 + 进度流**，避免中间层
 *    超时后出现「客户端看到失败、评测其实已跑完」的假失败。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ApiClientError, api, jobStream } from '@/api/client'
import type {
  EvalCaseKind,
  EvalCaseResult,
  EvalDescribeResult,
  EvalSuiteResult,
  JobHandle,
  JobProgressItem,
} from '@/api/types'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import { EVAL_OUTCOME_LABEL, EVAL_OUTCOME_TAG, formatTime } from '@/utils/format'

/** 用例类型文案（与后端 `EvalCaseKind` 对齐） */
const KIND_LABEL: Record<string, string> = {
  plan: '端到端建计划',
  chat: '会话任务',
  reflection: '确定性自愈演练',
}

const describe = ref<EvalDescribeResult | null>(null)
const loading = ref(false)
/** 评测台未启用（503 EVALS_DISABLED）：整页降级为提示态 */
const disabledReason = ref('')

const running = ref(false)
const progress = ref<JobProgressItem[]>([])
const suite = ref<EvalSuiteResult | null>(null)
const abortController = ref<AbortController | null>(null)

/** 运行范围：按类型过滤（空 = 全部） */
const selectedKinds = ref<EvalCaseKind[]>([])
/** 运行范围：指定用例 id（空 = 全部） */
const selectedCaseIds = ref<string[]>([])
/** 是否启用 LLM 评委（服务端未配置评委时该开关无效） */
const useJudge = ref(true)

const cases = computed(() => describe.value?.cases ?? [])
const kindOptions = computed(() => {
  const kinds = new Set(cases.value.map((item) => String(item.kind)))
  return Array.from(kinds)
})

const tiles = computed<StatTile[]>(() => {
  const current = suite.value
  if (!current) {
    return [
      { label: '黄金集用例', value: describe.value?.caseCount ?? 0, tone: 'brand', hint: '改动后跑一遍即可复算' },
      {
        label: 'LLM 评委',
        value: describe.value?.judgeEnabled ? '开启' : '关闭',
        hint: describe.value?.model ? `模型 ${describe.value.model}` : '未配置模型',
      },
      {
        label: '门禁阈值',
        value: `≥${Math.round((describe.value?.gates.passRate ?? 0) * 100)}%`,
        hint: `平均分 ≥ ${describe.value?.gates.minScore ?? 0}`,
      },
      { label: '运行后端', value: describe.value?.backend || '—', hint: `作业存储 ${describe.value?.jobBackend || '—'}` },
    ]
  }
  return [
    { label: '通过 / 总数', value: `${current.passed} / ${current.total}`, tone: 'brand', hint: `异常 ${current.errored} 例（不入通过率分母）` },
    { label: '通过率', value: `${Math.round(current.passRate * 100)}%`, tone: current.gate?.passed ? 'success' : 'danger', hint: '只统计 pass / fail' },
    { label: '平均分', value: current.avgScore.toFixed(3), tone: current.avgScore >= (current.gate?.thresholds.minScore ?? 0) ? 'success' : 'danger', hint: '确定性 + 评委加权' },
    {
      label: '门禁',
      value: current.gate ? (current.gate.passed ? '通过' : '未通过') : '未判定',
      tone: current.gate?.passed ? 'success' : 'danger',
      hint: current.gate?.passed ? '达到放行阈值' : (current.gate?.reasons[0] ?? '—'),
    },
  ]
})

function handleError(error: unknown, prefix: string): void {
  const message = error instanceof ApiClientError ? error.message : String(error)
  if (error instanceof ApiClientError && error.code === 'EVALS_DISABLED') {
    disabledReason.value = message
    return
  }
  ElMessage.error(`${prefix}：${message}`)
}

async function loadDescribe(): Promise<void> {
  loading.value = true
  try {
    describe.value = await api.evalsDescribe()
    disabledReason.value = ''
    useJudge.value = describe.value.judgeEnabled
  } catch (error) {
    describe.value = null
    handleError(error, '加载评测台失败')
  } finally {
    loading.value = false
  }
}

/** 用例类型标签样式 */
function outcomeTag(outcome: string): 'primary' | 'success' | 'info' | 'warning' | 'danger' {
  return EVAL_OUTCOME_TAG[outcome] ?? 'info'
}

function outcomeLabel(outcome: string): string {
  return EVAL_OUTCOME_LABEL[outcome] ?? outcome
}

/** 单例是否有失败断言（表格里直接给出原因，不用展开） */
function failedAssertions(row: EvalCaseResult): string {
  const failed = row.assertions.filter((item) => !item.ok)
  if (failed.length === 0) return ''
  return failed.map((item) => `${item.name}：${item.detail}`).join('；')
}

/**
 * 跑一遍评测。
 *
 * 默认走**异步作业**：受理后拿到 jobId，用既有 SSE 进度流订阅阶段推进，终态在
 * `snapshot.result.data` 里取回完整结果。若后端返回的是同步结果（未受理为作业），
 * 则直接渲染。
 */
async function run(): Promise<void> {
  if (running.value) return
  running.value = true
  suite.value = null
  progress.value = []
  const controller = new AbortController()
  abortController.value = controller
  try {
    const result = await api.evalsRun(
      {
        kinds: selectedKinds.value.length > 0 ? selectedKinds.value : undefined,
        caseIds: selectedCaseIds.value.length > 0 ? selectedCaseIds.value : undefined,
        judge: useJudge.value,
        enqueue: true,
      },
      controller.signal,
    )
    if (!('jobId' in result)) {
      // 同步返回：直接渲染（离线 reflection 演练会走这条快路）
      suite.value = result
      return
    }
    const handle = result as JobHandle
    await jobStream(
      handle.jobId,
      {
        onProgress: (item) => {
          progress.value = [...progress.value, item]
        },
        onDone: (snapshot) => {
          const data = (snapshot.result as { data?: EvalSuiteResult } | null)?.data
          if (data) {
            suite.value = data
            return
          }
          ElMessage.warning('评测作业已完成，但结果明细超出作业结果上限被裁剪；请缩小运行范围后重跑')
        },
        onError: (error) => ElMessage.error(`评测作业失败：${error.message}`),
      },
      controller.signal,
    )
  } catch (error) {
    handleError(error, '触发评测失败')
  } finally {
    running.value = false
  }
}

/** 用例 id 勾选切换（用于只跑少数用例的快速回归） */
function toggleCase(caseId: string): void {
  selectedCaseIds.value = selectedCaseIds.value.includes(caseId)
    ? selectedCaseIds.value.filter((item) => item !== caseId)
    : [...selectedCaseIds.value, caseId]
}

onMounted(loadDescribe)
</script>

<template>
  <div class="ip-page">
    <el-alert v-if="disabledReason" type="warning" :closable="false" class="ip-eval__alert">
      <template #title>评测台未启用</template>
      <div class="ip-eval__alert-body">
        {{ disabledReason }} 在 `.env` 中设置 `EVALS_ENABLED=true` 后重启服务即可启用。
        评测台会让 Agent 真实跑一遍黄金集，用来判断「改了 Prompt / 工具之后是变好还是变坏」。
      </div>
    </el-alert>

    <StatTiles :items="tiles" />

    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>运行评测</span>
        <span class="ip-muted ip-mono">默认异步作业执行，可实时看到阶段推进</span>
        <el-button size="small" :icon="'Refresh'" @click="loadDescribe">刷新</el-button>
      </div>

      <div class="ip-eval__controls">
        <div class="ip-eval__control">
          <span class="ip-eval__control-label">用例类型</span>
          <el-checkbox-group v-model="selectedKinds" size="small">
            <el-checkbox-button v-for="kind in kindOptions" :key="kind" :value="kind">
              {{ KIND_LABEL[kind] ?? kind }}
            </el-checkbox-button>
          </el-checkbox-group>
        </div>
        <div class="ip-eval__control">
          <span class="ip-eval__control-label">LLM 评委</span>
          <el-switch v-model="useJudge" size="small" :disabled="!describe?.judgeEnabled" />
          <span class="ip-muted ip-eval__control-hint">
            {{ describe?.judgeEnabled ? '评委只做补充评分，硬门槛仍由确定性断言判定' : '未配置模型，仅确定性断言打分' }}
          </span>
        </div>
        <div class="ip-eval__control">
          <span class="ip-eval__control-label">快速回归</span>
          <span class="ip-muted ip-eval__control-hint">
            已选 {{ selectedCaseIds.length }} 例{{ selectedCaseIds.length > 0 ? '（仅跑选中用例）' : '（默认全部）' }}
          </span>
          <el-button v-if="selectedCaseIds.length > 0" size="small" text @click="selectedCaseIds = []">清空选择</el-button>
        </div>
        <el-button
          type="primary"
          size="small"
          :icon="'VideoPlay'"
          :loading="running"
          :disabled="!!disabledReason"
          @click="run"
        >
          {{ running ? '评测中…' : '跑一遍' }}
        </el-button>
      </div>

      <div v-if="progress.length > 0" class="ip-eval__progress">
        <div v-for="(item, index) in progress" :key="index" class="ip-eval__progress-item">
          <el-tag size="small" effect="plain">{{ item.phase }}</el-tag>
          <span class="ip-eval__progress-msg">{{ item.message }}</span>
          <span class="ip-mono ip-muted">{{ formatTime(item.at * 1000) }}</span>
        </div>
      </div>
    </div>

    <el-alert
      v-if="suite && suite.gate && !suite.gate.passed"
      type="error"
      :closable="false"
      class="ip-eval__alert"
    >
      <template #title>门禁未通过（CI 会据此拒绝放行）</template>
      <ul class="ip-eval__reasons">
        <li v-for="reason in suite.gate.reasons" :key="reason">{{ reason }}</li>
      </ul>
    </el-alert>
    <el-alert
      v-else-if="suite && suite.gate && suite.gate.passed"
      type="success"
      :closable="false"
      class="ip-eval__alert"
      title="门禁通过：通过率与平均分均达到阈值"
    />

    <el-alert v-if="suite && suite.notes.length > 0" type="info" :closable="false" class="ip-eval__alert">
      <template #title>运行说明</template>
      <ul class="ip-eval__reasons">
        <li v-for="note in suite.notes" :key="note">{{ note }}</li>
      </ul>
    </el-alert>

    <div v-if="suite" class="ip-panel">
      <div class="ip-panel__title">
        <span>评测结果</span>
        <span class="ip-muted ip-mono">
          runId {{ suite.runId }} · 耗时 {{ (suite.durationMs / 1000).toFixed(1) }}s · 模型 {{ suite.model || '—' }}
        </span>
      </div>

      <el-table :data="suite.cases" size="small" border row-key="caseId">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="ip-eval__expand">
              <div v-for="assertion in row.assertions" :key="assertion.name" class="ip-eval__assert">
                <el-tag size="small" effect="plain" :type="assertion.ok ? 'success' : 'danger'">
                  {{ assertion.ok ? '通过' : '不通过' }}
                </el-tag>
                <span class="ip-eval__assert-name">{{ assertion.name }}</span>
                <el-tag v-if="assertion.hard" size="small" effect="plain" type="warning">硬门槛</el-tag>
                <span class="ip-muted ip-eval__assert-detail">{{ assertion.detail }}</span>
              </div>
              <div v-if="row.judgeRationale" class="ip-eval__judge">
                <span class="ip-eval__judge-label">评委意见（{{ row.judgeLevel || '—' }}）</span>
                <div class="ip-eval__text">{{ row.judgeRationale }}</div>
              </div>
              <div v-if="row.content" class="ip-eval__judge">
                <span class="ip-eval__judge-label">Agent 结论</span>
                <div class="ip-eval__text">{{ row.content }}</div>
              </div>
              <div v-if="row.error" class="ip-eval__error">环境异常：{{ row.error }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="用例" min-width="220">
          <template #default="{ row }">
            <div class="ip-eval__title">{{ row.title }}</div>
            <div class="ip-mono ip-muted ip-eval__case-id">{{ row.caseId }}</div>
            <div v-if="failedAssertions(row)" class="ip-eval__fail-reason">{{ failedAssertions(row) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="130" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ KIND_LABEL[row.kind] ?? row.kind }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="结论" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="outcomeTag(row.outcome)">{{ outcomeLabel(row.outcome) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="确定性分" width="90" align="center">
          <template #default="{ row }">
            <span class="ip-mono">{{ row.deterministicScore.toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="评委分" width="90" align="center">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.judgeScore === null ? '—（缺席）' : row.judgeScore.toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="得分" width="80" align="center">
          <template #default="{ row }">
            <span class="ip-mono ip-eval__score">{{ row.score.toFixed(2) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="步数 / 工具" width="90" align="center">
          <template #default="{ row }">
            <span class="ip-mono">{{ row.steps }} / {{ row.toolChain.length }}</span>
          </template>
        </el-table-column>
        <el-table-column label="产出计划" width="140">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.planId || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="90" align="right">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ (row.durationMs / 1000).toFixed(1) }}s</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>黄金集清单</span>
        <span class="ip-muted ip-mono">点击「快速回归」只跑关心的少数用例</span>
      </div>
      <el-table v-loading="loading" :data="cases" size="small" border>
        <el-table-column label="用例" min-width="240">
          <template #default="{ row }">
            <div class="ip-eval__title">{{ row.title }}</div>
            <div class="ip-mono ip-muted ip-eval__case-id">{{ row.id }}</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="130" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ KIND_LABEL[row.kind] ?? row.kind }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="依赖" width="150">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.dependsOn || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="评什么（确定性断言）" min-width="260">
          <template #default="{ row }">
            <div class="ip-eval__expect">
              <el-tag v-for="(value, key) in row.expect" :key="key" size="small" effect="plain">
                {{ key }}
              </el-tag>
            </div>
            <div v-if="row.rubric" class="ip-muted ip-eval__rubric">评委维度：{{ row.rubric }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text :type="selectedCaseIds.includes(row.id) ? 'danger' : 'primary'" @click="toggleCase(row.id)">
              {{ selectedCaseIds.includes(row.id) ? '取消选择' : '快速回归' }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="disabledReason ? '评测台未启用' : '暂无黄金集用例'" />
        </template>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.ip-eval__alert {
  margin-bottom: 12px;
}

.ip-eval__alert-body {
  margin-top: 4px;
  font-size: 12.5px;
  line-height: 1.7;
}

.ip-eval__reasons {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.75;
}

.ip-eval__controls {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.ip-eval__control {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ip-eval__control-label {
  font-size: 12px;
  color: var(--ip-text-muted);
}

.ip-eval__control-hint {
  font-size: 11.5px;
}

.ip-eval__progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-eval__progress-item {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}

.ip-eval__progress-msg {
  color: var(--ip-text);
}

.ip-eval__title {
  font-size: 12.5px;
  color: var(--ip-text);
}

.ip-eval__case-id {
  margin-top: 2px;
  font-size: 11px;
}

.ip-eval__fail-reason {
  margin-top: 4px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ip-danger);
}

.ip-eval__score {
  font-weight: 600;
}

.ip-eval__expand {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 4px 8px;
}

.ip-eval__assert {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}

.ip-eval__assert-name {
  font-weight: 600;
  color: var(--ip-text);
}

.ip-eval__assert-detail {
  font-size: 11.5px;
}

.ip-eval__judge-label {
  font-size: 11.5px;
  color: var(--ip-text-muted);
}

.ip-eval__text {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.75;
  color: var(--ip-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.ip-eval__error {
  font-size: 12px;
  color: var(--ip-danger);
}

.ip-eval__expect {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.ip-eval__rubric {
  margin-top: 4px;
  font-size: 11.5px;
  line-height: 1.6;
}
</style>
