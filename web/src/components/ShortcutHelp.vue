<script setup lang="ts">
/**
 * 快捷键帮助面板（`?` 打开）。
 *
 * 快捷键的**唯一实现处**在 `layouts/DefaultLayout.vue` 的全局 keydown 监听，
 * 这里只负责展示说明，避免说明与实现漂移（列表与实现一一对应）。
 */

const visible = defineModel<boolean>({ default: false })

/** 快捷键说明条目（与布局层的实现保持一致） */
const SHORTCUTS: Array<{ keys: string; label: string; note: string }> = [
  { keys: 'Ctrl / ⌘ + K', label: '全局搜索', note: '检索页面、计划与当前计划的任务' },
  { keys: 'Ctrl / ⌘ + Shift + L', label: '切换主题', note: '蓝晒日间 / 夜间台面互切' },
  { keys: 'Ctrl / ⌘ + Shift + E', label: '导出任务 CSV', note: '导出当前计划的全部任务（Excel 可直接打开）' },
  { keys: 'Ctrl / ⌘ + Shift + H', label: '打印当前计划报告', note: '跳转打印友好报告页并唤起打印' },
  { keys: 'Ctrl / ⌘ + Shift + I', label: '导入任务数据', note: '粘贴 / 上传 CSV、JSON，追加到当前计划或新建计划' },
  { keys: 'Ctrl / ⌘ + Shift + A', label: 'AI 助手', note: '唤起会话抽屉，可问答或改计划（写操作需确认后执行）' },
  { keys: 'F1', label: '打开本帮助', note: '任意状态（含输入框内）均可触发' },
  { keys: '?', label: '打开本帮助', note: '非输入状态下按 Shift + / 触发（中文输入法可能拦截，可用 F1）' },
  { keys: 'Esc', label: '关闭弹层', note: '关闭搜索 / 帮助 / 对话框' },
]
</script>

<template>
  <el-dialog v-model="visible" title="快捷键与操作说明" width="560px" append-to-body>
    <el-table :data="SHORTCUTS" size="small" :show-header="false">
      <el-table-column width="190">
        <template #default="{ row }">
          <span class="ip-shortcut__keys">{{ row.keys }}</span>
        </template>
      </el-table-column>
      <el-table-column>
        <template #default="{ row }">
          <div class="ip-shortcut__label">{{ row.label }}</div>
          <div class="ip-shortcut__note">{{ row.note }}</div>
        </template>
      </el-table-column>
    </el-table>
    <p class="ip-muted" style="margin: 12px 0 0">
      提示：全局快捷键在任意页面可用；在输入框内编辑文本时，仅 `Ctrl/⌘ + K` 与 `Ctrl/⌘ + Shift + *` 组合键生效，
      `?` 会被视为普通字符。
    </p>
  </el-dialog>
</template>

<style scoped>
.ip-shortcut__keys {
  display: inline-block;
  font-family: var(--ip-font-mono);
  font-size: 11px;
  padding: 3px 8px;
  color: var(--ip-text);
  background: var(--ip-surface-sunk);
  border: 1px solid var(--ip-border-strong);
  border-radius: 3px;
}

.ip-shortcut__label {
  font-size: 13px;
  color: var(--ip-text);
}

.ip-shortcut__note {
  font-size: 11px;
  color: var(--ip-text-muted);
}
</style>
