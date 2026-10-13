/**
 * 图表视觉主题（ECharts / AntV X6 共用）。
 *
 * 控制台的设计令牌定义在 `src/styles/main.css` 的 `--ip-*`，但 ECharts 与 X6
 * 都无法直接读取 CSS 变量，所以在此以常量镜像同一套「蓝晒制图台」色系，
 * 避免各视图散落硬编码色值（枚举 → 颜色的映射仍统一在 `format.ts`）。
 *
 * 深色模式联动：本模块的**中性色**（文本 / 坐标轴 / 分隔线 / 容器色）以 `reactive`
 * 承载，`applyChartTheme(dark)` 原地改写属性值。视图内的 computed 读取这些属性时
 * 会被依赖追踪，主题切换后图表自动重算 —— 因此各视图无需为深色模式写额外分支。
 *
 * 注意：`GRAPH_CANVAS`（DAG 画布）**不参与**深色切换：画布保持「制图纸」本色，
 * 在夜间台面上形成一张被照亮的图纸，这是刻意的视觉选择。
 */

import { reactive } from 'vue'

/** 数据系列色（品牌 / 信号色不随主题变化，保持蓝晒身份） */
const SIGNAL = {
  brand: '#0e6ba8',
  brandStrong: '#0a5384',
  accent: '#17a2b8',
  signal: '#e08b1a',
  danger: '#d64545',
  warning: '#e08b1a',
  success: '#2f9e6b',
} as const

/** 中性色档位（同一组键在明 / 暗两种主题下取值不同） */
interface NeutralScale {
  ink: string
  ink700: string
  ink500: string
  ink300: string
  ink200: string
  slate: string
  muted: string
  line: string
  lineSoft: string
  surface: string
  sunk: string
}

/** 中性色：随主题切换（light / dark） */
const NEUTRAL: { light: NeutralScale; dark: NeutralScale } = {
  light: {
    ink: '#0a2236',
    ink700: '#16405e',
    ink500: '#2a6b8f',
    ink300: '#7fa6bd',
    ink200: '#b3c9d8',
    slate: '#6b8296',
    muted: '#98a2b3',
    line: '#d5dfe8',
    lineSoft: '#e8eef4',
    surface: '#ffffff',
    sunk: '#f5f8fb',
  },
  dark: {
    ink: '#dce9f2',
    ink700: '#b3c9d8',
    ink500: '#7fa6bd',
    ink300: '#5d8099',
    ink200: '#33546d',
    slate: '#8fa8bb',
    muted: '#7d95a8',
    line: '#2a4d68',
    lineSoft: '#1c3a52',
    surface: '#0e2438',
    sunk: '#0a1c2c',
  },
}

/** 主色板（与 main.css 令牌同名同值；中性项随主题变化） */
export const TOKEN = reactive({
  ...NEUTRAL.light,
  ...SIGNAL,
})

/** 数据系列序列（多系列图表按序取色） */
export const SERIES_PALETTE = reactive<string[]>([
  SIGNAL.brand,
  SIGNAL.accent,
  SIGNAL.signal,
  SIGNAL.success,
  NEUTRAL.light.ink500,
  SIGNAL.danger,
  NEUTRAL.light.ink300,
  NEUTRAL.light.slate,
])

/** 责任人着色序列（甘特图 / 负载图，需彼此可区分；不随主题变化） */
export const ASSIGNEE_PALETTE = reactive<string[]>([
  '#0e6ba8',
  '#17a2b8',
  '#2f9e6b',
  '#e08b1a',
  '#8b5cf6',
  '#d64545',
  '#0f766e',
  '#6b8296',
])

/** 图表通用字体（等宽数据字，读数对齐） */
export const CHART_FONT = "'Cascadia Mono', 'Cascadia Code', Consolas, 'Courier New', monospace"

/** 坐标轴标签 / 图例文本样式 */
export const CHART_TEXT = reactive({ color: NEUTRAL.light.slate, fontSize: 11, fontFamily: CHART_FONT })
/** 轴线 */
export const CHART_AXIS_LINE = reactive({ lineStyle: { color: NEUTRAL.light.line } })
/** 网格分隔线 */
export const CHART_SPLIT_LINE = reactive({ lineStyle: { color: NEUTRAL.light.lineSoft } })
/** 主数据色（单系列柱/线） */
export const CHART_SERIES = SIGNAL.brand
/** 基准参考线（虚线批注） */
export const CHART_MARK_LINE: { type: 'dashed'; color: string; width: number } = reactive({
  type: 'dashed' as const,
  color: NEUTRAL.light.ink300,
  width: 1.5,
})
/** 空态填充（如「无风险」占位扇区） */
export const CHART_EMPTY = NEUTRAL.light.lineSoft

/** AntV X6 画布：制图纸底纹 + 网格点（固定浅色，形成「被照亮的图纸」） */
export const GRAPH_CANVAS = {
  background: '#ffffff',
  gridColor: '#dfe8f0',
  nodeFill: '#ffffff',
  nodeFillMuted: '#f5f8fb',
  nodeText: '#0a2236',
  nodeTextWarn: '#8a4b0f',
  edgeColor: '#b9c9d6',
} as const

/** 把中性色与轴样式切换到指定主题（原地改写，驱动 reactive 依赖更新） */
export function applyChartTheme(dark: boolean): void {
  const neutral = dark ? NEUTRAL.dark : NEUTRAL.light
  Object.assign(TOKEN, neutral)
  CHART_TEXT.color = neutral.slate
  CHART_AXIS_LINE.lineStyle.color = neutral.line
  CHART_SPLIT_LINE.lineStyle.color = neutral.lineSoft
  CHART_MARK_LINE.color = neutral.ink300
  // 系列序列中的中性档位同步替换，保持多系列图在深色底上的可辨识度
  const series = [
    SIGNAL.brand,
    SIGNAL.accent,
    SIGNAL.signal,
    SIGNAL.success,
    neutral.ink500,
    SIGNAL.danger,
    neutral.ink300,
    neutral.slate,
  ]
  SERIES_PALETTE.splice(0, SERIES_PALETTE.length, ...series)
}
