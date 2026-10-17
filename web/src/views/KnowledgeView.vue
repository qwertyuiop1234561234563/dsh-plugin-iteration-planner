<script setup lang="ts">
/**
 * 知识库（RAG）：企业资料的入库、检视与检索试跑。
 *
 * 口径边界：
 *  - 本页只做「素材管理与检索可视化」，不改变任何算法口径；检索结果由后端
 *    完成「稠密向量 + BM25 → RRF 融合」，前端仅按返回顺序展示并标注得分；
 *  - 入库支持「上传文件」与「粘贴文本」两条路径，均复用后端统一的解析 / 分块 /
 *    向量化流水线（md/txt/csv/json/html/pdf/docx）；
 *  - 未配置向量化模型时后端返回 503 EMBEDDING_UNAVAILABLE，本页据此展示引导，
 *    而不是把它伪装成「知识库为空」。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type { KbDocument, KbDocumentDetail, KbHit } from '@/api/types'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import { formatIso } from '@/utils/format'

/** 上传可接受的后缀（与后端 `KB_DOCUMENT_EXTENSIONS` 对齐，仅用于文件选择器过滤） */
const UPLOAD_ACCEPT = '.md,.markdown,.txt,.csv,.json,.html,.htm,.pdf,.docx'

const stats = ref<Awaited<ReturnType<typeof api.kbStats>> | null>(null)
const documents = ref<KbDocument[]>([])
const loading = ref(false)
/** 向量化未启用（后端 503 EMBEDDING_UNAVAILABLE）：整页降级为引导态 */
const disabled = ref(false)
const disabledReason = ref('')

/* ---------------------------------- 上传 ---------------------------------- */

const uploadVisible = ref(false)
const uploading = ref(false)
const uploadForm = ref({ title: '', source: '', tags: '' })
const uploadInput = ref<HTMLInputElement>()
const pickedFile = ref<File | null>(null)

/* --------------------------------- 粘贴文本 --------------------------------- */

const createVisible = ref(false)
const creating = ref(false)
const createForm = ref({ title: '', content: '', source: '', tags: '' })

/* --------------------------------- 文档详情 --------------------------------- */

const detailVisible = ref(false)
const detail = ref<KbDocumentDetail | null>(null)
const detailLoading = ref(false)

/* --------------------------------- 检索试跑 --------------------------------- */

const query = ref('')
const topK = ref<number>(5)
const hybrid = ref(true)
const searching = ref(false)
const searchMode = ref('')
const hits = ref<KbHit[]>([])

const tiles = computed<StatTile[]>(() => [
  { label: '文档', value: stats.value?.documentCount ?? 0, tone: 'brand' },
  { label: '分块', value: stats.value?.chunkCount ?? 0, hint: `共 ${(stats.value?.totalChars ?? 0).toLocaleString()} 字符` },
  { label: '分块策略', value: `${stats.value?.chunkSize ?? 0} / ${stats.value?.chunkOverlap ?? 0}`, hint: '块大小 / 重叠字符' },
  { label: '检索模式', value: stats.value?.hybrid ? '混合' : '纯向量', hint: stats.value?.embedder || '未启用向量化' },
])

/** 统一处理「向量化未启用」：置位引导态，避免把配置缺失当业务空数据 */
function handleError(error: unknown, prefix: string): void {
  const message = error instanceof ApiClientError ? error.message : String(error)
  if (error instanceof ApiClientError && error.code === 'EMBEDDING_UNAVAILABLE') {
    disabled.value = true
    disabledReason.value = message
    return
  }
  ElMessage.error(`${prefix}：${message}`)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [stat, list] = await Promise.all([api.kbStats(), api.kbDocuments()])
    stats.value = stat
    documents.value = list.items
    disabled.value = false
    disabledReason.value = ''
  } catch (error) {
    documents.value = []
    stats.value = null
    handleError(error, '加载知识库失败')
  } finally {
    loading.value = false
  }
}

/* ---------------------------------- 上传 ---------------------------------- */

function openUpload(): void {
  uploadForm.value = { title: '', source: '', tags: '' }
  pickedFile.value = null
  uploadVisible.value = true
}

function onPickFile(event: Event): void {
  const input = event.target as HTMLInputElement
  pickedFile.value = input.files?.[0] ?? null
  // 未填标题时用文件名兜底（后端缺省也取文件名，这里让用户可见）
  if (pickedFile.value && !uploadForm.value.title.trim()) uploadForm.value.title = pickedFile.value.name
}

async function submitUpload(): Promise<void> {
  if (!pickedFile.value) {
    ElMessage.warning('请先选择要入库的文件')
    return
  }
  uploading.value = true
  try {
    const doc = await api.kbUploadDocument({
      file: pickedFile.value,
      title: uploadForm.value.title.trim(),
      source: uploadForm.value.source.trim(),
      tags: uploadForm.value.tags.trim(),
    })
    ElMessage.success(`已入库「${doc.title || doc.filename}」（${doc.chunkCount} 个分块）`)
    uploadVisible.value = false
    await load()
  } catch (error) {
    handleError(error, '上传失败')
  } finally {
    uploading.value = false
  }
}

/* -------------------------------- 粘贴文本 -------------------------------- */

function openCreate(): void {
  createForm.value = { title: '', content: '', source: '', tags: '' }
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  if (!createForm.value.title.trim() || !createForm.value.content.trim()) {
    ElMessage.warning('请填写标题与正文')
    return
  }
  creating.value = true
  try {
    const doc = await api.kbCreateDocument({
      title: createForm.value.title.trim(),
      content: createForm.value.content,
      source: createForm.value.source.trim() || undefined,
      tags: splitTags(createForm.value.tags),
    })
    ElMessage.success(`已入库「${doc.title}」（${doc.chunkCount} 个分块）`)
    createVisible.value = false
    await load()
  } catch (error) {
    handleError(error, '新建文档失败')
  } finally {
    creating.value = false
  }
}

/** 标签输入（中英文逗号均可）→ 数组 */
function splitTags(raw: string): string[] {
  return raw
    .replace(/，/g, ',')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

/* -------------------------------- 文档详情 -------------------------------- */

async function openDetail(row: KbDocument): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await api.kbGetDocument(row.docId)
  } catch (error) {
    detailVisible.value = false
    handleError(error, '读取文档详情失败')
  } finally {
    detailLoading.value = false
  }
}

async function removeDocument(row: KbDocument): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除文档「${row.title || row.filename}」及其全部 ${row.chunkCount} 个分块？删除后检索不再命中该资料。`,
      '删除文档',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await api.kbDeleteDocument(row.docId)
    ElMessage.success(`已删除文档及 ${result.deletedChunks} 个分块`)
    await load()
  } catch (error) {
    handleError(error, '删除失败')
  }
}

/* -------------------------------- 检索试跑 -------------------------------- */

async function runSearch(): Promise<void> {
  const text = query.value.trim()
  if (!text) {
    ElMessage.warning('请输入检索关键词或问题')
    return
  }
  searching.value = true
  try {
    const result = await api.kbSearch({ query: text, topK: topK.value, hybrid: hybrid.value })
    hits.value = result.items
    searchMode.value = result.mode
    if (result.items.length === 0) ElMessage.info('未命中任何片段，可尝试更换关键词或补充资料')
  } catch (error) {
    hits.value = []
    searchMode.value = ''
    handleError(error, '检索失败')
  } finally {
    searching.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="ip-page">
    <el-alert v-if="disabled" type="warning" :closable="false" class="ip-kb__alert">
      <template #title>向量化模型未启用，知识库不可用</template>
      <div class="ip-kb__alert-body">
        {{ disabledReason || '请配置 EMBEDDING_API_KEY 后重启服务；若向量化与 LLM 是同一端点，可留空 EMBEDDING_BASE_URL 以复用 LLM_API_KEY。' }}
        支持任意 OpenAI 兼容 /embeddings 端点（默认硅基流动 BAAI/bge-m3，长期免费），向量库使用 Chroma 本地持久化。
      </div>
    </el-alert>

    <StatTiles :items="tiles" />

    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>文档清单</span>
        <div class="ip-kb__actions">
          <el-button size="small" :icon="'Refresh'" :disabled="disabled" @click="load">刷新</el-button>
          <el-button size="small" :icon="'Upload'" :disabled="disabled" @click="openUpload">上传文件</el-button>
          <el-button size="small" type="primary" :icon="'Plus'" :disabled="disabled" @click="openCreate">粘贴文本</el-button>
        </div>
      </div>

      <el-table v-loading="loading" :data="documents" size="small" border>
        <el-table-column label="标题 / 文件" min-width="240">
          <template #default="{ row }">
            <div class="ip-kb__title">{{ row.title || row.filename || '—' }}</div>
            <div class="ip-muted ip-kb__sub">{{ row.filename }}<span v-if="row.source"> · {{ row.source }}</span></div>
          </template>
        </el-table-column>
        <el-table-column label="格式" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ (row.extension || '—').toUpperCase() }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分块" width="90" align="right">
          <template #default="{ row }">
            <span class="ip-mono">{{ row.chunkCount }}</span>
          </template>
        </el-table-column>
        <el-table-column label="字符" width="110" align="right">
          <template #default="{ row }">
            <span class="ip-mono">{{ row.charCount.toLocaleString() }}</span>
          </template>
        </el-table-column>
        <el-table-column label="标签" min-width="150">
          <template #default="{ row }">
            <span v-if="row.tags.length === 0" class="ip-muted">—</span>
            <el-tag v-for="tag in row.tags" :key="tag" size="small" effect="plain" class="ip-kb__tag">{{ tag }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="入库时间" width="150">
          <template #default="{ row }">
            <span class="ip-mono ip-muted">{{ row.createdAt ? formatIso(row.createdAt) : '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" align="center" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openDetail(row)">查看</el-button>
            <el-button size="small" text type="danger" @click="removeDocument(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="disabled ? '向量化模型未启用' : '知识库为空，先上传资料或粘贴文本'" />
        </template>
      </el-table>
    </div>

    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>检索试跑</span>
        <span v-if="searchMode" class="ip-muted ip-mono">
          模式：{{ searchMode === 'hybrid' ? '混合检索（向量 + BM25 → RRF）' : '纯向量检索' }}
        </span>
      </div>

      <div class="ip-kb__search">
        <el-input
          v-model="query"
          placeholder="输入问题或关键词，例如：迭代缓冲系数是怎么定的？"
          clearable
          :disabled="disabled"
          @keydown.enter="runSearch"
        />
        <el-input-number v-model="topK" :min="1" :max="20" size="default" style="width: 120px" />
        <el-switch v-model="hybrid" active-text="混合检索" :disabled="disabled" />
        <el-button type="primary" :icon="'Search'" :loading="searching" :disabled="disabled" @click="runSearch">检索</el-button>
      </div>

      <div v-if="hits.length > 0" class="ip-kb__hits">
        <div v-for="(hit, index) in hits" :key="hit.chunkId ?? index" class="ip-kb__hit">
          <div class="ip-kb__hit-head">
            <el-tag size="small" effect="plain" type="primary">#{{ index + 1 }}</el-tag>
            <span class="ip-kb__hit-title">{{ hit.title || hit.docId || '未命名文档' }}</span>
            <span class="ip-muted ip-mono">分块 {{ hit.chunkIndex }}</span>
            <span class="ip-kb__hit-score ip-mono">score {{ hit.score.toFixed(4) }}</span>
          </div>
          <div class="ip-kb__hit-text">{{ hit.text }}</div>
        </div>
      </div>
      <el-empty v-else-if="!disabled" :image-size="64" description="输入关键词后点击「检索」，查看哪些片段会被召回" />
    </div>

    <!-- 上传文件 -->
    <el-dialog v-model="uploadVisible" title="上传文件建库" width="560px" append-to-body destroy-on-close>
      <el-form label-width="88px">
        <el-form-item label="文件">
          <input ref="uploadInput" type="file" :accept="UPLOAD_ACCEPT" class="ip-kb__file" @change="onPickFile" />
          <div class="ip-muted ip-kb__hint">支持 {{ UPLOAD_ACCEPT }}；PDF / DOCX 会在服务端抽取正文后分块。</div>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="uploadForm.title" placeholder="缺省取文件名" maxlength="200" />
        </el-form-item>
        <el-form-item label="来源">
          <el-input v-model="uploadForm.source" placeholder="如：架构 Wiki / 需求评审记录" maxlength="200" />
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="uploadForm.tags" placeholder="逗号分隔，如：规范,接口,测试" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitUpload">入库</el-button>
      </template>
    </el-dialog>

    <!-- 粘贴文本 -->
    <el-dialog v-model="createVisible" title="粘贴文本建库" width="720px" append-to-body destroy-on-close>
      <el-form label-width="88px">
        <el-form-item label="标题">
          <el-input v-model="createForm.title" placeholder="文档标题（用于检索结果标注）" maxlength="200" />
        </el-form-item>
        <el-form-item label="正文">
          <el-input v-model="createForm.content" type="textarea" :rows="12" placeholder="粘贴纯文本或 Markdown；服务端会按语义边界自动分块" />
        </el-form-item>
        <el-form-item label="来源">
          <el-input v-model="createForm.source" placeholder="如：团队约定 / 外部文档链接" maxlength="200" />
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="createForm.tags" placeholder="逗号分隔" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">入库</el-button>
      </template>
    </el-dialog>

    <!-- 文档详情 -->
    <el-dialog v-model="detailVisible" title="文档详情" width="760px" append-to-body destroy-on-close>
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="ip-kb__detail-meta">
            <div class="ip-kb__title">{{ detail.title || detail.filename }}</div>
            <div class="ip-muted ip-kb__sub">
              {{ detail.filename }} · {{ detail.chunkCount }} 个分块 · {{ detail.charCount.toLocaleString() }} 字符
              <span v-if="detail.createdAt"> · {{ formatIso(detail.createdAt) }}</span>
            </div>
          </div>
          <div class="ip-kb__chunks">
            <div v-for="chunk in detail.chunks" :key="chunk.chunkId ?? chunk.chunkIndex" class="ip-kb__chunk">
              <div class="ip-kb__chunk-head">分块 {{ chunk.chunkIndex }}</div>
              <pre class="ip-kb__chunk-text">{{ chunk.text }}</pre>
            </div>
          </div>
        </template>
        <el-empty v-else-if="!detailLoading" description="未读取到文档内容" />
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.ip-kb__alert {
  margin-bottom: 12px;
}

.ip-kb__alert-body {
  margin-top: 4px;
  font-size: 12.5px;
  line-height: 1.7;
}

.ip-kb__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.ip-kb__title {
  font-weight: 600;
  color: var(--ip-text);
}

.ip-kb__sub {
  margin-top: 2px;
  font-size: 12px;
  word-break: break-all;
}

.ip-kb__tag {
  margin-right: 4px;
}

.ip-kb__search {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.ip-kb__hits {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ip-kb__hit {
  padding: 10px 12px;
  border: 1px solid var(--ip-border);
  border-left: 3px solid var(--ip-brand);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-kb__hit-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.ip-kb__hit-title {
  font-weight: 600;
  color: var(--ip-text);
}

.ip-kb__hit-score {
  margin-left: auto;
  font-size: 11px;
  color: var(--ip-text-muted);
}

.ip-kb__hit-text {
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--ip-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.ip-kb__file {
  font-size: 13px;
}

.ip-kb__hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
}

.ip-kb__detail-meta {
  margin-bottom: 10px;
}

.ip-kb__chunks {
  max-height: 52vh;
  overflow: auto;
  padding-right: 4px;
}

.ip-kb__chunk {
  padding: 8px 0;
  border-top: 1px dashed var(--ip-border);
}

.ip-kb__chunk-head {
  font-family: var(--ip-font-mono);
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ip-text-muted);
}

.ip-kb__chunk-text {
  margin: 6px 0 0;
  font-family: var(--ip-font-sans);
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--ip-text);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
