<script lang="ts">
/** 指标瓦片数据项（供页面构造入参时复用类型） */
export interface StatTile {
  label: string
  value: string | number
  /** 数值后缀（如 `h` / `%`） */
  unit?: string
  /** 补充说明 */
  hint?: string
  /** 语义色调：danger 触发告警底色，brand 强调关键值 */
  tone?: 'default' | 'brand' | 'danger' | 'success' | 'warning'
}
</script>

<script setup lang="ts">
/**
 * 指标瓦片：治理页面（基线 / 负荷 / 燃尽 / 质量）共用的等宽指标条。
 *
 * 只做展示：数值与提示文案均由页面从后端返回值派生后传入，组件不含任何业务口径。
 */
withDefaults(defineProps<{ items: StatTile[]; columns?: number }>(), { columns: 4 })
</script>

<template>
  <div class="ip-tiles" :style="{ '--ip-tiles-cols': String(columns) }">
    <div v-for="item in items" :key="item.label" class="ip-tiles__card" :class="`ip-tiles__card--${item.tone ?? 'default'}`">
      <div class="ip-tiles__label">{{ item.label }}</div>
      <div class="ip-tiles__value">
        {{ item.value }}<span v-if="item.unit" class="ip-tiles__unit">{{ item.unit }}</span>
      </div>
      <div v-if="item.hint" class="ip-tiles__hint">{{ item.hint }}</div>
    </div>
  </div>
</template>

<style scoped>
.ip-tiles {
  display: grid;
  grid-template-columns: repeat(var(--ip-tiles-cols, 4), minmax(0, 1fr));
  gap: 12px;
}

.ip-tiles__card {
  position: relative;
  padding: 14px 16px 12px;
  background: var(--ip-surface);
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius-lg);
  box-shadow: var(--ip-shadow-sm);
  overflow: hidden;
}

.ip-tiles__card::after {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: var(--ip-border-strong);
}

.ip-tiles__card--brand::after {
  background: var(--ip-brand);
}

.ip-tiles__card--danger {
  border-color: color-mix(in srgb, var(--ip-danger) 45%, var(--ip-border));
}

.ip-tiles__card--danger::after {
  background: var(--ip-danger);
}

.ip-tiles__card--success::after {
  background: var(--ip-success);
}

.ip-tiles__card--warning::after {
  background: var(--ip-warning);
}

.ip-tiles__label {
  font-size: 12px;
  letter-spacing: 0.04em;
  color: var(--ip-text-muted);
}

.ip-tiles__value {
  margin-top: 6px;
  font-family: var(--ip-font-mono);
  font-size: 22px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--ip-text);
}

.ip-tiles__unit {
  margin-left: 3px;
  font-size: 12px;
  font-weight: 500;
  color: var(--ip-text-secondary);
}

.ip-tiles__hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--ip-text-secondary);
}

@media (max-width: 1100px) {
  .ip-tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
