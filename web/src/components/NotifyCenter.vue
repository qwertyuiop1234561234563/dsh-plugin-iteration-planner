<script setup lang="ts">
/**
 * 通知中心：顶部铃铛 + 未读角标 + 事件列表。
 *
 * 数据来自 `stores/notify`（已本地持久化）。点击条目会优先跳转到关联计划，
 * 并自动标记已读；列表只保留最近 100 条，避免无界增长。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useNotifyStore, type NotifyItem } from '@/stores/notify'
import { relativeTime } from '@/utils/format'

const notify = useNotifyStore()
const router = useRouter()

/** 面板显隐（点击条目后自动收起） */
const popoverVisible = ref(false)

/** 角标上限显示（超过 99 显示 99+） */
const badge = computed(() => (notify.unread > 99 ? '99+' : String(notify.unread)))

/** 级别 → Element Plus tag 样式 */
const LEVEL_TAG: Record<NotifyItem['level'], 'primary' | 'success' | 'warning' | 'danger'> = {
  info: 'primary',
  success: 'success',
  warning: 'warning',
  danger: 'danger',
}

/** 级别 → 中文文案 */
const LEVEL_LABEL: Record<NotifyItem['level'], string> = {
  info: '通知',
  success: '完成',
  warning: '注意',
  danger: '风险',
}

function open(item: NotifyItem): void {
  notify.markRead(item.id)
  if (item.planId) {
    void router.push({ name: item.routeName ?? 'plan-overview', params: { planId: item.planId } })
    popoverVisible.value = false
  }
}
</script>

<template>
  <el-popover v-model:visible="popoverVisible" placement="bottom-end" :width="400" trigger="click">
    <template #reference>
      <el-badge :value="notify.unread > 0 ? badge : ''" :max="99" :offset="[-2, 4]">
        <el-button :icon="'Bell'" circle :title="'通知中心（' + notify.unread + ' 条未读）'" />
      </el-badge>
    </template>

    <div class="ip-notify">
      <div class="ip-notify__head">
        <span class="ip-notify__title">通知中心</span>
        <span class="ip-muted">{{ notify.unread }} 条未读 / 共 {{ notify.list.length }} 条</span>
      </div>

      <div class="ip-notify__actions">
        <el-button link type="primary" size="small" :disabled="notify.unread === 0" @click="notify.markAllRead()">
          全部标记已读
        </el-button>
        <el-button link type="danger" size="small" :disabled="notify.list.length === 0" @click="notify.clear()">
          清空
        </el-button>
      </div>

      <el-scrollbar max-height="360px">
        <div v-if="notify.list.length === 0" class="ip-notify__empty">
          暂无通知。计划的新建、重规划、任务变更与其他会话的实时变更都会出现在这里。
        </div>
        <div
          v-for="item in notify.list"
          :key="item.id"
          class="ip-notify__item"
          :class="{ 'is-unread': !item.read }"
          role="button"
          tabindex="0"
          @click="open(item)"
          @keydown.enter="open(item)"
        >
          <span class="ip-notify__dot" :class="`is-${item.level}`" />
          <span class="ip-notify__body">
            <span class="ip-notify__row">
              <el-tag :type="LEVEL_TAG[item.level]" size="small" effect="plain">{{ LEVEL_LABEL[item.level] }}</el-tag>
              <span class="ip-notify__name">{{ item.title }}</span>
              <span class="ip-notify__time">{{ relativeTime(item.ts) }}</span>
            </span>
            <span v-if="item.detail" class="ip-notify__detail">{{ item.detail }}</span>
          </span>
          <span class="ip-notify__ops">
            <el-button link type="info" size="small" @click.stop="notify.markRead(item.id)">已读</el-button>
            <el-button link type="danger" size="small" @click.stop="notify.remove(item.id)">删除</el-button>
          </span>
        </div>
      </el-scrollbar>
    </div>
  </el-popover>
</template>

<style scoped>
.ip-notify {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ip-notify__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.ip-notify__title {
  font-family: var(--ip-font-display);
  font-size: 14px;
  font-weight: 600;
  color: var(--ip-text);
}

.ip-notify__actions {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
  border-bottom: 1px solid var(--ip-border);
  padding-bottom: 4px;
}

.ip-notify__empty {
  padding: 22px 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-muted);
  text-align: center;
}

.ip-notify__item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  padding: 8px 6px;
  border: none;
  border-bottom: 1px dashed var(--ip-border);
  background: none;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.ip-notify__item:hover {
  background: var(--ip-surface-sunk);
}

.ip-notify__item.is-unread .ip-notify__name {
  font-weight: 700;
  color: var(--ip-text);
}

.ip-notify__dot {
  flex: none;
  width: 7px;
  height: 7px;
  margin-top: 6px;
  border-radius: 50%;
  background: var(--ip-info);
}

.ip-notify__dot.is-success {
  background: var(--ip-success);
}

.ip-notify__dot.is-warning {
  background: var(--ip-signal);
}

.ip-notify__dot.is-danger {
  background: var(--ip-danger);
}

.ip-notify__body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ip-notify__row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ip-notify__name {
  flex: 1;
  font-size: 12px;
  color: var(--ip-text-secondary);
}

.ip-notify__time {
  flex: none;
  font-family: var(--ip-font-mono);
  font-size: 10px;
  color: var(--ip-text-muted);
}

.ip-notify__detail {
  font-size: 11px;
  line-height: 1.5;
  color: var(--ip-text-muted);
  word-break: break-word;
}

.ip-notify__ops {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 0;
  opacity: 0;
  transition: opacity 0.15s ease;
}

.ip-notify__item:hover .ip-notify__ops {
  opacity: 1;
}
</style>
