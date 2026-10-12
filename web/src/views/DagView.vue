<script setup lang="ts">
/**
 * 依赖 DAG 画布（AntV X6）。
 *
 * 布局：按拓扑层级（最长路径深度）从左到右分层排布，同层节点纵向居中；
 * 高亮：
 *   - 关键路径节点与关键边（红色实线）；
 *   - 环内节点（橙色虚线，存在循环依赖时给出阻断提示）；
 *   - 孤立节点（灰色底）；
 *   - 悬空依赖（引用了不存在任务）在顶部以告警条列出。
 */
import { Graph } from '@antv/x6'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { GraphNode } from '@/api/types'
import { usePlanStore } from '@/stores/plan'
import { STATUS_LABEL, TASK_TYPE_COLOR, TASK_TYPE_LABEL } from '@/utils/format'

const store = usePlanStore()

/** 画布容器与图实例 */
const container = ref<HTMLDivElement>()
let graph: Graph | null = null
let observer: ResizeObserver | null = null

/** 仅展示关键路径 */
const criticalOnly = ref(false)

/** 当前选中的节点（右侧详情面板） */
const selected = ref<GraphNode | null>(null)

const NODE_WIDTH = 194
const NODE_HEIGHT = 56
const GAP_X = 92
const GAP_Y = 32

/** 过滤后的节点与边（criticalOnly 时只保留关键路径及其内部连线） */
const filtered = computed(() => {
  const payload = store.graph
  if (!payload) return { nodes: [] as GraphNode[], edges: [] as { source: string; target: string }[] }
  if (!criticalOnly.value) return { nodes: payload.nodes, edges: payload.edges }
  const keep = new Set(payload.nodes.filter((node) => node.critical).map((node) => node.id))
  return {
    nodes: payload.nodes.filter((node) => keep.has(node.id)),
    edges: payload.edges.filter((edge) => keep.has(edge.source) && keep.has(edge.target)),
  }
})

/* ------------------------------- 布局计算 ------------------------------- */

/** 计算每个节点的层级深度（拓扑最长路径），环内节点置于末列 */
function computeDepths(nodes: GraphNode[], edges: { source: string; target: string }[]): Map<string, number> {
  const payload = store.graph
  const depth = new Map<string, number>()
  for (const node of nodes) depth.set(node.id, 0)

  const outgoing = new Map<string, string[]>()
  for (const edge of edges) {
    const list = outgoing.get(edge.source) ?? []
    list.push(edge.target)
    outgoing.set(edge.source, list)
  }

  // 按拓扑序推进（Kahn 输出保证前置先于后置）
  const order = (payload?.topoOrder ?? nodes.map((node) => node.id)).filter((id) => depth.has(id))
  for (const id of order) {
    const current = depth.get(id) ?? 0
    for (const target of outgoing.get(id) ?? []) {
      depth.set(target, Math.max(depth.get(target) ?? 0, current + 1))
    }
  }

  // 环内节点不在拓扑序中，统一放最后一列，避免与其他列重叠
  const cycleNodes = payload?.cycleNodes ?? []
  const maxDepth = depth.size > 0 ? Math.max(...depth.values()) : 0
  for (const id of cycleNodes) {
    if (depth.has(id)) depth.set(id, maxDepth + 1)
  }
  return depth
}

/** 生成 X6 节点配置（含分层坐标） */
function buildNodeConfigs(nodes: GraphNode[], depths: Map<string, number>): Record<string, unknown>[] {
  const columns = new Map<number, string[]>()
  for (const node of nodes) {
    const depth = depths.get(node.id) ?? 0
    const list = columns.get(depth) ?? []
    list.push(node.id)
    columns.set(depth, list)
  }

  const byId = new Map(nodes.map((node) => [node.id, node]))
  // 最高列的总高度，用于各列垂直居中
  const columnHeights = [...columns.values()].map((ids) => ids.length * NODE_HEIGHT + (ids.length - 1) * GAP_Y)
  const maxColumnHeight = columnHeights.length > 0 ? Math.max(...columnHeights) : 0

  const configs: Record<string, unknown>[] = []
  for (const [depth, ids] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    const columnHeight = ids.length * NODE_HEIGHT + (ids.length - 1) * GAP_Y
    const offsetY = (maxColumnHeight - columnHeight) / 2 + 40
    ids.forEach((id, index) => {
      const node = byId.get(id)
      if (!node) return
      const stroke = node.critical ? '#f04438' : node.inCycle ? '#f79009' : TASK_TYPE_COLOR[node.taskType]
      configs.push({
        id: node.id,
        shape: 'rect',
        x: depth * (NODE_WIDTH + GAP_X) + 40,
        y: offsetY + index * (NODE_HEIGHT + GAP_Y),
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        attrs: {
          body: {
            stroke,
            strokeWidth: node.critical ? 2.5 : 1.5,
            strokeDasharray: node.inCycle ? '5 4' : null,
            fill: node.isolated ? '#f5f6f8' : '#ffffff',
            rx: 8,
            ry: 8,
          },
          label: {
            text: `${node.label}\n${TASK_TYPE_LABEL[node.taskType]} · ${node.estimatedHours}h`,
            fill: node.inCycle ? '#b54708' : '#1f2d3d',
            fontSize: 12,
            lineHeight: 17,
          },
        },
      })
    })
  }
  return configs
}

/** 生成 X6 连线配置（关键边红色加粗） */
function buildEdgeConfigs(
  nodes: GraphNode[],
  edges: { source: string; target: string }[],
): Record<string, unknown>[] {
  const byId = new Map(nodes.map((node) => [node.id, node]))
  return edges.map((edge) => {
    const source = byId.get(edge.source)
    const target = byId.get(edge.target)
    // 关键边判定：两端均为关键任务，且紧邻（target.es == source.es + source 工时）
    const criticalEdge =
      !!source &&
      !!target &&
      source.critical &&
      target.critical &&
      Math.abs(target.es - (source.es + source.estimatedHours)) < 1e-6
    const stroke = criticalEdge ? '#f04438' : '#c3cad9'
    return {
      source: { cell: edge.source },
      target: { cell: edge.target },
      attrs: {
        line: {
          stroke,
          strokeWidth: criticalEdge ? 2.2 : 1.2,
          targetMarker: { name: 'block', width: 8, height: 6, fill: stroke },
        },
      },
      router: { name: 'normal' },
      connector: { name: 'rounded', args: { radius: 12 } },
      zIndex: criticalEdge ? 2 : 1,
    }
  })
}

/* ------------------------------- 渲染控制 ------------------------------- */

function disposeGraph(): void {
  observer?.disconnect()
  observer = null
  graph?.dispose()
  graph = null
}

function render(): void {
  disposeGraph()
  const element = container.value
  const payload = store.graph
  if (!element || !payload) return

  graph = new Graph({
    container: element,
    background: { color: '#ffffff' },
    grid: { visible: true, type: 'dot', size: 14, args: { color: '#e8ecf3' } },
    panning: { enabled: true, eventTypes: ['leftMouseDown', 'mouseWheel'] },
    mousewheel: { enabled: true, modifiers: ['ctrl', 'meta'], minScale: 0.25, maxScale: 2.5 },
    interacting: { nodeMovable: true, edgeMovable: false },
  })

  const { nodes, edges } = filtered.value
  const depths = computeDepths(nodes, edges)
  graph.addNodes(buildNodeConfigs(nodes, depths))
  graph.addEdges(buildEdgeConfigs(nodes, edges))

  graph.on('node:click', ({ node }) => {
    selected.value = nodes.find((item) => item.id === node.id) ?? null
  })
  graph.on('blank:click', () => {
    selected.value = null
  })

  graph.zoomToFit({ padding: 24, maxScale: 1 })
}

function zoom(factor: number): void {
  if (!graph) return
  graph.zoom(factor)
}

function fit(): void {
  graph?.zoomToFit({ padding: 24, maxScale: 1 })
}

/* ------------------------------- 详情联动 ------------------------------- */

/** 选中任务的后置任务名称 */
const successors = computed(() => {
  const payload = store.graph
  if (!payload || !selected.value) return []
  return payload.edges
    .filter((edge) => edge.source === selected.value!.id)
    .map((edge) => store.taskIndex.get(edge.target)?.taskName ?? edge.target)
})

/** 选中任务的前置任务名称 */
const predecessors = computed(() => {
  const payload = store.graph
  if (!payload || !selected.value) return []
  return payload.edges
    .filter((edge) => edge.target === selected.value!.id)
    .map((edge) => store.taskIndex.get(edge.source)?.taskName ?? edge.source)
})

onMounted(() => {
  render()
  if (container.value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => graph?.resize(container.value!.clientWidth, container.value!.clientHeight))
    observer.observe(container.value)
  }
})

onBeforeUnmount(disposeGraph)

watch(() => [store.graph, criticalOnly.value], render, { deep: false })
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!store.plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div v-if="store.graph?.hasCycle" class="ip-panel ip-alert-panel">
        <el-alert type="error" :closable="false" show-icon>
          <template #title>检测到循环依赖，已阻断关键路径计算</template>
          <div class="ip-cycle-list">
            环内任务：
            <el-tag v-for="id in store.graph.cycleNodes" :key="id" type="danger" size="small" effect="plain" style="margin-right: 6px">
              {{ store.taskIndex.get(id)?.taskName ?? id }}
            </el-tag>
            <span class="ip-muted">请前往「任务管理」调整前置依赖后重新规划。</span>
          </div>
        </el-alert>
      </div>

      <div v-if="store.graph && store.graph.dangling.length > 0" class="ip-panel ip-alert-panel">
        <el-alert type="warning" :closable="false" show-icon title="存在悬空依赖（引用了不存在的任务）">
          <div class="ip-cycle-list">
            <div v-for="item in store.graph.dangling" :key="item.taskId" class="ip-muted">
              {{ store.taskIndex.get(item.taskId)?.taskName ?? item.taskId }} →
              <span class="ip-mono">{{ item.missing.join(', ') }}</span>
            </div>
          </div>
        </el-alert>
      </div>

      <div class="ip-grid ip-dag-grid">
        <div class="ip-panel">
          <div class="ip-panel__title">
            <span>依赖图（{{ filtered.nodes.length }} 节点 / {{ filtered.edges.length }} 边）</span>
            <div class="ip-actions">
              <el-checkbox v-model="criticalOnly">仅关键路径</el-checkbox>
              <el-button-group>
                <el-button :icon="'ZoomIn'" @click="zoom(1.15)" />
                <el-button :icon="'ZoomOut'" @click="zoom(0.87)" />
                <el-button :icon="'FullScreen'" @click="fit" />
              </el-button-group>
            </div>
          </div>
          <div ref="container" class="ip-dag-canvas" />
          <div class="ip-legend">
            <span class="ip-legend__item"><i class="ip-legend__dot" style="background: #f04438" />关键路径</span>
            <span class="ip-legend__item"><i class="ip-legend__dot" style="background: #f79009" />环内任务</span>
            <span class="ip-legend__item"><i class="ip-legend__dot" style="background: #c3cad9" />普通节点</span>
            <span class="ip-legend__item ip-muted">按住左键拖拽平移，Ctrl + 滚轮缩放</span>
          </div>
        </div>

        <div class="ip-panel">
          <div class="ip-panel__title"><span>任务详情</span></div>
          <el-empty v-if="!selected" description="点击画布节点查看详情" :image-size="70" />
          <el-descriptions v-else :column="1" border size="small">
            <el-descriptions-item label="任务名称">{{ selected.label }}</el-descriptions-item>
            <el-descriptions-item label="任务 ID"><span class="ip-mono">{{ selected.id }}</span></el-descriptions-item>
            <el-descriptions-item label="类型">{{ TASK_TYPE_LABEL[selected.taskType] }}</el-descriptions-item>
            <el-descriptions-item label="状态">{{ STATUS_LABEL[selected.status] }}</el-descriptions-item>
            <el-descriptions-item label="优先级">{{ selected.priority }}</el-descriptions-item>
            <el-descriptions-item label="预估工时">{{ selected.estimatedHours }}h</el-descriptions-item>
            <el-descriptions-item label="排期">
              <span class="ip-mono">es {{ selected.es }} / ls {{ selected.ls }} / slack {{ selected.slack }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="责任人">{{ selected.assignee || '—' }}</el-descriptions-item>
            <el-descriptions-item label="关键任务">{{ selected.critical ? '是' : '否' }}</el-descriptions-item>
            <el-descriptions-item label="前置任务">{{ predecessors.length ? predecessors.join('、') : '—' }}</el-descriptions-item>
            <el-descriptions-item label="后置任务">{{ successors.length ? successors.join('、') : '—' }}</el-descriptions-item>
          </el-descriptions>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-alert-panel {
  padding: 14px 18px;
}

.ip-cycle-list {
  margin-top: 6px;
  font-size: 12.5px;
  line-height: 1.9;
}

.ip-dag-grid {
  grid-template-columns: minmax(0, 1fr) 340px;
  align-items: start;
}

.ip-dag-canvas {
  width: 100%;
  height: 640px;
  border: 1px solid var(--ip-border);
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}

.ip-legend {
  display: flex;
  align-items: center;
  gap: 18px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--ip-text-secondary);
  flex-wrap: wrap;
}

.ip-legend__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.ip-legend__dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}

@media (max-width: 1200px) {
  .ip-dag-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>