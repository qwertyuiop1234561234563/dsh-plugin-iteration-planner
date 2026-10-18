<script setup lang="ts">
/**
 * 新建计划页：录入业务需求与全局约束，触发完整规划流水线。
 *
 * 流水线（后端固定顺序）：
 *   需求拆解 → 工时缓冲修正 → Kahn 拓扑环检测 → AOE 关键路径排期 → 风险扫描 → 燃尽数据 → 完整计划
 */
import { computed, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { usePlanStore } from '@/stores/plan'
import type { JobPhase } from '@/api/types'
import { BUILTIN_TEMPLATES, newTemplateId, readCustomTemplates, writeCustomTemplates } from '@/utils/templates'
import type { IterationTemplate } from '@/utils/templates'
import { addDays, formatISODate, parseISODate } from '@/utils/workday'

const store = usePlanStore()
const router = useRouter()
const formRef = ref<FormInstance>()
const submitting = ref(false)

/* ------------------------------ 异步作业进度 ------------------------------ */

/** 秒级时钟：作业可能跑几分钟，「已提交 mm:ss」必须自己走，否则用户以为页面卡死 */
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | null = null
ticker = setInterval(() => {
  now.value = Date.now()
}, 1000)
onUnmounted(() => {
  if (ticker) clearInterval(ticker)
  ticker = null
})

const JOB_STATUS_TEXT: Record<string, string> = {
  pending: '排队中',
  running: '处理中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

/** 各阶段对应的进度百分比（阶段是离散的，进度条只表达「走到哪一段」） */
const PHASE_PERCENT: Record<JobPhase, number> = { queued: 10, engine: 60, settled: 90 }

const jobStatusText = computed(() => JOB_STATUS_TEXT[store.writeJob?.status ?? 'pending'] ?? '处理中')

const jobTagType = computed(() => {
  const status = store.writeJob?.status
  if (status === 'succeeded') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'info'
  return 'warning'
})

const jobProgressStatus = computed(() => {
  const status = store.writeJob?.status
  if (status === 'succeeded') return 'success' as const
  if (status === 'failed') return 'exception' as const
  return undefined
})

const jobPercent = computed(() => {
  const job = store.writeJob
  if (!job) return 0
  if (job.status === 'succeeded') return 100
  const last = job.progress[job.progress.length - 1]
  if (!last) return job.status === 'pending' ? 2 : 5
  return PHASE_PERCENT[last.phase as JobPhase] ?? 60
})

const cancellable = computed(() => {
  const status = store.writeJob?.status
  return status === 'pending' || status === 'running'
})

const elapsedText = computed(() => {
  const startedAt = store.writeJob?.startedAt ?? now.value
  const seconds = Math.max(0, Math.floor((now.value - startedAt) / 1000))
  const minutes = Math.floor(seconds / 60)
  return `${minutes.toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`
})

/** 样例需求：便于快速体验完整流程 */
const SAMPLE_REQUIREMENT = [
  '为电商中台开发【订单中心 v2.1】迭代：',
  '1) 完成订单创建、支付回调与库存扣减能力；',
  '2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；',
  '3) 支持订单查询与报表统计；',
  '4) 优化下单链路性能，并搭建压测环境；',
  '5) 产出接口文档与部署说明。',
].join('')

function todayISO(offsetDays = 0): string {
  const date = new Date()
  date.setDate(date.getDate() + offsetDays)
  return date.toISOString().slice(0, 10)
}

const form = reactive({
  requirement: SAMPLE_REQUIREMENT,
  iterationStartDate: todayISO(),
  iterationDeadline: todayISO(18),
  devMemberCount: 3,
})

/** 校验：截止日期必须不早于起始日期 */
const rules: FormRules = {
  requirement: [
    { required: true, message: '请填写业务需求描述', trigger: 'blur' },
    { min: 10, message: '需求描述建议不少于 10 个字符，以便拆解出有效任务', trigger: 'blur' },
  ],
  iterationStartDate: [{ required: true, message: '请选择迭代起始日期', trigger: 'change' }],
  iterationDeadline: [
    { required: true, message: '请选择迭代截止日期', trigger: 'change' },
    {
      validator: (_rule, value: string, callback: (error?: Error) => void) => {
        if (value && form.iterationStartDate && value < form.iterationStartDate) {
          callback(new Error('截止日期不能早于起始日期'))
          return
        }
        callback()
      },
      trigger: 'change',
    },
  ],
  devMemberCount: [{ required: true, message: '请填写可用开发人数', trigger: 'change' }],
}

/** 缓冲规则说明（来自后端枚举元数据，保证与算法一致） */
const bufferRules = computed(() => store.enums?.bufferRules ?? [])

/* ------------------------------- 迭代模板库 ------------------------------ */

/** 自定义模板（localStorage 持久化） */
const customTemplates = ref<IterationTemplate[]>(readCustomTemplates())

/** 全部模板：内置 + 自定义 */
const allTemplates = computed<IterationTemplate[]>(() => [...BUILTIN_TEMPLATES, ...customTemplates.value])

/** 当前选中的模板（用于高亮） */
const selectedTemplateId = ref('')

/** ISO 日期加减自然日（模板窗口长度按自然日计） */
function shiftISO(iso: string, days: number): string {
  return formatISODate(addDays(parseISODate(iso), days))
}

/** 应用模板：填入需求文本 / 开发人数，并按窗口长度重算截止日期 */
function applyTemplate(template: IterationTemplate): void {
  selectedTemplateId.value = template.id
  form.requirement = template.requirement
  form.devMemberCount = template.devMemberCount
  form.iterationDeadline = shiftISO(form.iterationStartDate, Math.max(1, template.windowDays) - 1)
}

/** 把当前表单保存为自定义模板 */
async function saveAsTemplate(): Promise<void> {
  if (!form.requirement.trim()) {
    ElMessage.warning('请先填写业务需求，再保存为模板')
    return
  }
  let name = ''
  try {
    const result = await ElMessageBox.prompt('为当前填写内容命名，保存后可在模板库一键复用', '保存为迭代模板', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputPlaceholder: '例如：结算中心重构迭代',
      inputValidator: (value: string) => (value && value.trim().length >= 2 ? true : '模板名称至少 2 个字符'),
    })
    name = result.value.trim()
  } catch {
    return // 用户取消
  }

  const windowDays = Math.max(
    1,
    Math.round(
      (parseISODate(form.iterationDeadline).getTime() - parseISODate(form.iterationStartDate).getTime()) / 86_400_000,
    ) + 1,
  )
  const template: IterationTemplate = {
    id: newTemplateId(),
    name,
    description: `自定义模板 · ${form.devMemberCount} 人 · 窗口 ${windowDays} 天`,
    requirement: form.requirement,
    devMemberCount: form.devMemberCount,
    windowDays,
    builtin: false,
  }
  customTemplates.value = [template, ...customTemplates.value]
  writeCustomTemplates(customTemplates.value)
  selectedTemplateId.value = template.id
  ElMessage.success(`模板「${name}」已保存`)
}

/** 删除自定义模板（内置模板不可删除） */
async function removeTemplate(template: IterationTemplate): Promise<void> {
  if (template.builtin) return
  try {
    await ElMessageBox.confirm(`确认删除模板「${template.name}」？`, '删除模板', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  customTemplates.value = customTemplates.value.filter((item) => item.id !== template.id)
  writeCustomTemplates(customTemplates.value)
  if (selectedTemplateId.value === template.id) selectedTemplateId.value = ''
}

async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    const created = await store.createPlan({
      requirement: form.requirement,
      iterationStartDate: form.iterationStartDate,
      iterationDeadline: form.iterationDeadline,
      devMemberCount: form.devMemberCount,
    })
    if (created) await router.push({ name: 'plan-overview', params: { planId: created.planId } })
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="ip-page">
    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>迭代模板库</span>
        <el-button size="small" :icon="'Collection'" @click="saveAsTemplate">将当前填写保存为模板</el-button>
      </div>
      <div class="ip-template-grid">
        <button
          v-for="template in allTemplates"
          :key="template.id"
          type="button"
          class="ip-template"
          :class="{ 'is-active': selectedTemplateId === template.id }"
          @click="applyTemplate(template)"
        >
          <span class="ip-template__head">
            <span class="ip-template__name">{{ template.name }}</span>
            <el-tag size="small" effect="plain" :type="template.builtin ? 'info' : 'success'">
              {{ template.builtin ? '内置' : '自定义' }}
            </el-tag>
          </span>
          <span class="ip-template__desc">{{ template.description }}</span>
          <span class="ip-template__meta">{{ template.devMemberCount }} 人 · 窗口 {{ template.windowDays }} 天</span>
          <el-icon
            v-if="!template.builtin"
            class="ip-template__del"
            title="删除该模板"
            @click.stop="removeTemplate(template)"
          >
            <Delete />
          </el-icon>
        </button>
      </div>
      <p class="ip-muted" style="margin: 10px 0 0">
        模板只预置「需求文本 + 迭代窗口 + 开发人数」，任务拆解与依赖仍由引擎的规划流水线生成，模板不介入任何算法口径。
      </p>
    </div>

    <div class="ip-grid ip-grid--2">
      <div class="ip-panel">
        <div class="ip-panel__title">需求与迭代约束</div>
        <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
          <el-form-item label="业务需求" prop="requirement">
            <el-input
              v-model="form.requirement"
              type="textarea"
              :rows="10"
              maxlength="4000"
              show-word-limit
              placeholder="粘贴版本需求文档 / 迭代需求描述，越具体越利于任务拆解与风险识别"
            />
            <el-button link type="primary" size="small" @click="form.requirement = SAMPLE_REQUIREMENT">
              填入样例需求
            </el-button>
          </el-form-item>

          <el-form-item label="迭代起始" prop="iterationStartDate">
            <el-date-picker
              v-model="form.iterationStartDate"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="迭代起始日期"
              style="width: 100%"
            />
          </el-form-item>

          <el-form-item label="迭代截止" prop="iterationDeadline">
            <el-date-picker
              v-model="form.iterationDeadline"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="迭代截止日期"
              style="width: 100%"
            />
          </el-form-item>

          <el-form-item label="开发人数" prop="devMemberCount">
            <el-input-number v-model="form.devMemberCount" :min="1" :max="50" />
            <span class="ip-muted" style="margin-left: 10px">用于人员过载校验（单日 ≤ 8 小时）</span>
          </el-form-item>

          <el-form-item>
            <el-button
              type="primary"
              :loading="submitting"
              :disabled="submitting"
              :icon="'Promotion'"
              @click="submit"
            >
              生成迭代计划
            </el-button>
            <el-button :disabled="submitting" @click="router.push({ name: 'plan-list' })">返回列表</el-button>
          </el-form-item>
        </el-form>

        <!-- 进度面板：长耗时写已改为「提交即返回 + 进度流」，必须让用户看见它在跑，并允许放弃 -->
        <div v-if="store.writeJob" class="ip-job">
          <div class="ip-job__head">
            <span class="ip-job__title">{{ store.writeJob.label }}</span>
            <el-tag size="small" effect="plain" :type="jobTagType">{{ jobStatusText }}</el-tag>
          </div>
          <el-progress :percentage="jobPercent" :status="jobProgressStatus" :stroke-width="6" />
          <p class="ip-job__message">{{ store.writeJob.message }}</p>
          <div class="ip-job__foot">
            <span class="ip-muted">已提交 {{ elapsedText }}</span>
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="!cancellable"
              @click="store.cancelWrite()"
            >
              取消
            </el-button>
          </div>
        </div>
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">规划流水线说明</div>
        <el-steps direction="vertical" :active="7" finish-status="success" style="height: auto">
          <el-step title="需求拆解" description="LLM 主路径拆解为原子任务；失败自动降级规则实现" />
          <el-step title="工时缓冲修正" description="按固定规则上浮工时，作为风险缓冲" />
          <el-step title="Kahn 拓扑环检测" description="存在循环依赖立即阻断，提示修正依赖" />
          <el-step title="AOE 关键路径排期" description="计算各任务 es / ls / slack 与总工期" />
          <el-step title="风险扫描" description="硬编码规则 + LLM 语义风险识别" />
          <el-step title="燃尽数据" description="生成燃尽图原始数据点" />
          <el-step title="输出完整计划" description="结构化 JSON + 团队可读报表" />
        </el-steps>

        <el-divider content-position="left">工时缓冲规则</el-divider>
        <ul class="ip-rule-list">
          <li v-for="rule in bufferRules" :key="rule.key">
            {{ rule.label }}（×{{ (1 + rule.ratio).toFixed(2) }}）
          </li>
        </ul>
        <p class="ip-muted" style="margin-bottom: 0">
          多条规则命中同一任务时按「先类型、后优先级」顺序相乘。
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ip-rule-list {
  margin: 0;
  padding-left: 18px;
  line-height: 1.9;
  font-size: 13px;
  color: var(--ip-text-secondary);
}

.ip-template-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 10px;
}

.ip-template {
  position: relative;
  display: grid;
  gap: 5px;
  padding: 10px 12px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.ip-template:hover {
  border-color: var(--ip-brand);
  background: var(--ip-surface-sunk);
}

.ip-template.is-active {
  border-color: var(--ip-brand);
  box-shadow: inset 0 0 0 1px var(--ip-brand);
}

.ip-template__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-right: 16px;
}

.ip-template__name {
  font-family: var(--ip-font-display);
  font-size: 13.5px;
  font-weight: 700;
  color: var(--ip-text);
}

.ip-template__desc {
  font-size: 12px;
  line-height: 1.55;
  color: var(--ip-text-secondary);
}

.ip-template__meta {
  font-family: var(--ip-font-mono);
  font-size: 11px;
  color: var(--ip-text-muted);
}

.ip-template__del {
  position: absolute;
  top: 8px;
  right: 8px;
  font-size: 14px;
  color: var(--ip-text-muted);
}

.ip-template__del:hover {
  color: var(--ip-danger);
}
</style>