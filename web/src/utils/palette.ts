/**
 * 图表视觉主题（ECharts / AntV X6 共用）。
 *
 * 控制台的设计令牌定义在 `src/styles/main.css` 的 `--ip-*`，但 ECharts 与 X6
 * 都无法直接读取 CSS 变量，所以在此以常量镜像同一套「蓝晒制图台」色系，
 * 避免各视图散落硬编码色值（枚举 → 颜色的映射仍统一在 `format.ts`）。
 */

/** 主色板（与 main.css 令牌同名同值） */
export const TOKEN = {
  ink: '#0a2236',
  ink700: '#16405e',
  ink500: '#2a6b8f',
  ink300: '#7fa6bd',
  ink200: '#b3c9d8',
  brand: '#0e6ba8',
  brandStrong: '#0a5384',
  accent: '#17a2b8',
  signal: '#e08b1a',
  danger: '#d64545',
  warning: '#e08b1a',
  success: '#2f9e6b',
  slate: '#6b8296',
  muted: '#98a2b3',
  line: '#d5dfe8',
  lineSoft: '#e8eef4',
  surface: '#ffffff',
  sunk: '#f5f8fb',
} as const

/** 数据系列序列（多系列图表按序取色） */
export const SERIES_PALETTE = [
  TOKEN.brand,
  TOKEN.accent,
  TOKEN.signal,
  TOKEN.success,
  TOKEN.ink500,
  TOKEN.danger,
  TOKEN.ink300,
  TOKEN.slate,
] as const

/** 责任人着色序列（甘特图 / 负载图，需彼此可区分） */
export const ASSIGNEE_PALETTE = [
  '#0e6ba8',
  '#17a2b8',
  '#2f9e6b',
  '#e08b1a',
  '#8b5cf6',
  '#d64545',
  '#0f766e',
  '#6b8296',
] as const

/** 图表通用字体（等宽数据字，读数对齐） */
export const CHART_FONT = "'Cascadia Mono', 'Cascadia Code', Consolas, 'Courier New', monospace"

/** 坐标轴标签 / 图例文本样式 */
export const CHART_TEXT = { color: TOKEN.slate, fontSize: 11, fontFamily: CHART_FONT }
/** 轴线 */
export const CHART_AXIS_LINE = { lineStyle: { color: TOKEN.line } }
/** 网格分隔线 */
export const CHART_SPLIT_LINE = { lineStyle: { color: TOKEN.lineSoft } }
/** 主数据色（单系列柱/线） */
export const CHART_SERIES = TOKEN.brand
/** 基准参考线（虚线批注） */
export const CHART_MARK_LINE = { type: 'dashed' as const, color: TOKEN.ink300, width: 1.5 }
/** 空态填充（如「无风险」占位扇区） */
export const CHART_EMPTY = TOKEN.lineSoft

/** AntV X6 画布：制图纸底纹 + 网格点 */
export const GRAPH_CANVAS = {
  background: TOKEN.surface,
  gridColor: '#dfe8f0',
  nodeFill: TOKEN.surface,
  nodeFillMuted: TOKEN.sunk,
  nodeText: TOKEN.ink,
  nodeTextWarn: '#8a4b0f',
  edgeColor: '#b9c9d6',
} as const