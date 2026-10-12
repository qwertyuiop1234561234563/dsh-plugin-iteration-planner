<script setup lang="ts">
/**
 * 新建计划页：录入业务需求与全局约束，触发完整规划流水线。
 *
 * 流水线（后端固定顺序）：
 *   需求拆解 → 工时缓冲修正 → Kahn 拓扑环检测 → AOE 关键路径排期 → 风险扫描 → 燃尽数据 → 完整计划
 */
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { FormInstance, FormRules } from 'element-plus'
import { usePlanStore } from '@/stores/plan'

const store = usePlanStore()
const router = useRouter()
const formRef = ref<FormInstance>()
const submitting = ref(false)

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
            <el-button type="primary" :loading="submitting" :icon="'Promotion'" @click="submit">
              生成迭代计划
            </el-button>
            <el-button @click="router.push({ name: 'plan-list' })">返回列表</el-button>
          </el-form-item>
        </el-form>
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
</style>