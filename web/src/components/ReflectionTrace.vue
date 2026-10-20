<script setup lang="ts">
/**
 * 自反思轨迹（plan → act → observe → reflect 闭环中「reflect」这一步的可视化）。
 *
 * 为什么值得单独成组件：反思轨迹会出现在多个位置（对话气泡、运行台账详情、评测结果），
 * 且它的语义不是「又一条日志」而是**可复算的诊断结论** —— 类别与策略由服务端规则确定性
 * 产出（错误码 → 类别 → 策略），因此界面上要能一眼看出「Agent 自己发现了什么、怎么改的、
 * 改成功没有」。
 *
 * 展示口径：
 *  - 一次反思 = 一条时间轴节点：类别（原因）+ 策略（动作）+ 第几次尝试 + 是否已恢复；
 *  - `recovered` 由服务端按**可观测事实**回填（后续同工具调用成功），不是模型自述；
 *  - 熔断降级（`degrade`）单独标红：它意味着这轮不再重试，结论会如实说明未取得结果。
 */
import type { AgentReflection } from '@/api/types'
import {
  REFLECT_CATEGORY_TAG,
  reflectCategoryLabel,
  reflectStrategyLabel,
} from '@/utils/format'

const props = defineProps<{
  reflections: AgentReflection[]
  /** 折叠面板标题（缺省展示条数） */
  title?: string
}>()

/** 是否已全部恢复（用于标题上的整体状态标签） */
function allRecovered(): boolean {
  return props.reflections.every((item) => item.recovered)
}
</script>

<template>
  <el-collapse v-if="reflections.length > 0" class="ip-reflect">
    <el-collapse-item :name="title ?? 'reflections'">
      <template #title>
        <span class="ip-reflect__title">
          <el-icon><MagicStick /></el-icon>
          自我诊断 {{ reflections.length }} 次
          <el-tag v-if="allRecovered()" size="small" type="success" effect="plain">已自行纠正</el-tag>
          <el-tag v-else size="small" type="warning" effect="plain">存在未恢复项</el-tag>
        </span>
      </template>

      <ol class="ip-reflect__list">
        <li v-for="item in reflections" :key="item.index" class="ip-reflect__item">
          <div class="ip-reflect__head">
            <span class="ip-reflect__idx ip-mono">#{{ item.index }}</span>
            <el-tag size="small" effect="plain" :type="REFLECT_CATEGORY_TAG[item.category] ?? 'info'">
              {{ reflectCategoryLabel(item.category) }}
            </el-tag>
            <el-tag size="small" effect="plain" :type="item.strategy === 'degrade' ? 'danger' : 'primary'">
              {{ reflectStrategyLabel(item.strategy) }}
            </el-tag>
            <el-tag size="small" effect="plain">第 {{ item.attempt }} 次</el-tag>
            <el-tag size="small" effect="plain" :type="item.recovered ? 'success' : 'info'">
              {{ item.recovered ? '已恢复' : '未恢复' }}
            </el-tag>
            <span class="ip-reflect__where ip-mono">步骤 {{ item.step }} · {{ item.tool }}</span>
          </div>
          <div class="ip-reflect__body">
            <div class="ip-reflect__reason">{{ item.detail }}</div>
          </div>
        </li>
      </ol>
    </el-collapse-item>
  </el-collapse>
</template>

<style scoped>
.ip-reflect {
  margin-top: 8px;
}

.ip-reflect__title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ip-text-secondary);
}

.ip-reflect__list {
  margin: 0;
  padding: 0 0 0 2px;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ip-reflect__item {
  padding: 8px 10px;
  border: 1px solid var(--ip-border);
  /* 左侧色条把「诊断」与普通日志区分开，扫一眼即可定位 */
  border-left: 3px solid var(--ip-warning, #d97706);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-reflect__head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.ip-reflect__idx {
  font-size: 11px;
  color: var(--ip-text-muted);
}

.ip-reflect__where {
  margin-left: auto;
  font-size: 11px;
  color: var(--ip-text-muted);
}

.ip-reflect__reason {
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
