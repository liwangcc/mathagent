<template>
  <div class="bank-page page-shell">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <div class="card-head-left">
            <span class="head-title">题库</span>
            <el-tag v-if="educationStage" size="small">{{ profileStageLabel }}数学</el-tag>
            <span class="toolbar-divider" aria-hidden="true" />
            <el-input
              v-model="keyword"
              placeholder="搜索题目 / 答案"
              clearable
              class="bank-search"
              @keyup.enter="onSearch"
              @clear="onSearch"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
            <el-select
              v-model="chapter"
              :disabled="!educationStage"
              placeholder="全部章节"
              clearable
              class="bank-chapter"
              @change="onSearch"
              @clear="onSearch"
            >
              <el-option
                v-for="ch in chapters"
                :key="ch.chapter"
                :label="ch.chapter"
                :value="ch.chapter"
              />
            </el-select>
            <el-button type="primary" @click="onSearch">查询</el-button>
            <HelpButton
              title="题库搜索"
              content="可按题目或答案关键词、章节筛选。公库题目供所有老师使用，私库题目只属于当前账号。"
            />
          </div>
          <div class="card-head-right">
            <el-button @click="goPastPapers">
              <el-icon><Files /></el-icon>整卷真题
            </el-button>
            <el-button type="primary" @click="goPhotoImport">
              <el-icon><Camera /></el-icon>拍照导入
            </el-button>
            <el-button :disabled="!educationStage" :loading="exporting" @click="onExport">
              <el-icon><Download /></el-icon>导出私库
            </el-button>
            <HelpButton
              title="私库导出"
              content="导出的 JSON 只包含当前账号可导出的私库题目。发送给管理员审核后才能进入公库，请勿通过不安全渠道分享学生隐私。"
            />
          </div>
        </div>
      </template>

      <p class="bank-scope-note">题库、章节和导出均使用个人资料中的{{ profileStageLabel }}学段，无需重复选择。</p>

      <el-table :data="rows" v-loading="loading" stripe style="width:100%">
        <el-table-column label="题目" min-width="280">
          <template #default="{ row }">
            <AnswerDisplay :question="row.question" />
          </template>
        </el-table-column>
        <el-table-column label="答案" min-width="400">
          <template #default="{ row }">
            <AnswerDisplay :text="row.answer" />
          </template>
        </el-table-column>
        <el-table-column label="知识点" min-width="200">
          <template #default="{ row }">
            <el-tag v-if="hasKnowledge(row)" size="small">{{ row.topic }}</el-tag>
            <div v-else class="knowledge-missing">
              <el-tag
                size="small"
                :type="classificationState(row) === 'failed' ? 'danger' : 'info'"
                :title="row.classification_error || ''"
              >{{ CLASSIFICATION_LABELS[classificationState(row)] }}</el-tag>
              <el-cascader
                v-if="canDelete(row)"
                v-model="picks[row.id]"
                :options="knowledgeOptions"
                :disabled="classifyingId === row.id"
                placeholder="手动选择知识点"
                size="small"
                filterable
                class="knowledge-picker"
                @change="value => onClassify(row, value)"
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="归属" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.is_public" type="success" size="small">公库</el-tag>
            <el-tag v-else type="primary" size="small">私库</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="来源" width="140" show-overflow-tooltip />
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-popconfirm v-if="canDelete(row)" title="确定删除该题目？" @confirm="onDelete(row.id)">
              <template #reference>
                <el-button text type="danger" size="small">删除</el-button>
              </template>
            </el-popconfirm>
            <span v-else class="readonly-owner">{{ row.is_public ? '公库' : '他人私库' }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无数据" />
        </template>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @size-change="load"
          @current-change="load"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import AnswerDisplay from '@/components/AnswerDisplay.vue'
import { Camera, Download, Files, Search } from '@element-plus/icons-vue'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'
import HelpButton from '@/components/HelpButton.vue'
import { normalizeEducationStage } from '@/utils/educationStage'
import { CLASSIFICATION_LABELS, catalogOptions, classificationState, hasKnowledge } from '@/utils/knowledgeClassification'

const router = useRouter()
const auth = useAuthStore()
const educationStage = computed(() => normalizeEducationStage(auth.user?.education_stage))
const profileStageLabel = computed(() => auth.user?.education_stage === 'primary' ? '小学' : auth.user?.education_stage === 'junior' ? '初中' : auth.user?.education_stage === 'senior' ? '高中' : '未设置')
let listRequest = 0
let chapterRequest = 0
let exportRequest = 0
let disposed = false

function captureScope() {
  return { stage: educationStage.value, userId: auth.user?.id, token: auth.token }
}

function isCurrentScope(scope) {
  return !disposed && scope.stage === educationStage.value
    && scope.userId === auth.user?.id && scope.token === auth.token
}

function goPastPapers() {
  router.push('/bank/past-papers')
}

function goPhotoImport() {
  router.push('/bank/photo-import')
}

const keyword = ref('')
const chapter = ref('')
const chapters = ref([])
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const exporting = ref(false)
const picks = ref({})
const classifyingId = ref(null)
const knowledgeOptions = computed(() => catalogOptions(chapters.value))

function canDelete(row) {
  return !row.is_public && Number(row.teacher_id) === Number(auth.user?.id)
}

async function loadChapters() {
  const request = ++chapterRequest
  const scope = captureScope()
  chapters.value = []
  if (!scope.stage || !scope.userId || disposed) return
  try {
    const data = await api.bank.chapters({ education_stage: scope.stage })
    if (request !== chapterRequest || !isCurrentScope(scope)) return
    chapters.value = Array.isArray(data) ? data : data?.items || []
  } catch {
    if (request === chapterRequest && isCurrentScope(scope)) chapters.value = []
  }
}

async function load() {
  const request = ++listRequest
  const scope = captureScope()
  if (!scope.stage || !scope.userId || disposed) {
    rows.value = []
    total.value = 0
    loading.value = false
    return
  }
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize.value,
      education_stage: scope.stage,
    }
    if (chapter.value) params.chapter = chapter.value
    if (keyword.value.trim()) params.keyword = keyword.value.trim()

    const data = await api.bank.list(params)
    if (request !== listRequest || !isCurrentScope(scope)) return
    const items = data?.items || data?.questions || data || []
    rows.value = Array.isArray(items) ? items : []
    total.value = data?.total ?? rows.value.length
  } catch {
    if (request !== listRequest || !isCurrentScope(scope)) return
    rows.value = []
    total.value = 0
  } finally {
    if (request === listRequest && isCurrentScope(scope)) loading.value = false
  }
}

function onProfileScopeChange() {
  // Invalidate all in-flight reads before clearing the old account/stage view.
  listRequest += 1
  chapterRequest += 1
  exportRequest += 1
  exporting.value = false
  rows.value = []
  total.value = 0
  chapter.value = ''
  page.value = 1
  loadChapters()
  load()
}

function onSearch() {
  page.value = 1
  load()
}

async function onDelete(id) {
  const scope = captureScope()
  try {
    await api.bank.remove(id)
    if (!isCurrentScope(scope)) return
    ElMessage.success('已删除')
    if (rows.value.length === 1 && page.value > 1) {
      page.value -= 1
    }
    load()
  } catch {
    /* error handled by interceptor */
  }
}


// Unclassified or failed rows: the owner picks the knowledge point by hand.
async function onClassify(row, value) {
  if (!Array.isArray(value) || value.length !== 2) return
  const scope = captureScope()
  classifyingId.value = row.id
  try {
    await api.bank.update(row.id, { chapter: value[0], topic: value[1] })
    if (!isCurrentScope(scope)) return
    ElMessage.success('已设置知识点')
    load()
  } catch {
    /* error handled by interceptor */
  } finally {
    classifyingId.value = null
  }
}

async function onExport() {
  const scope = captureScope()
  if (!scope.stage || !scope.userId || disposed) return
  const request = ++exportRequest
  exporting.value = true
  try {
    const resp = await api.bank.exportPrivate({ education_stage: scope.stage })
    const data = await resp.json().catch(() => ({}))
    if (request !== exportRequest || !isCurrentScope(scope)) return
    // A failed export answers with an error body; never save that as the export file.
    if (!resp.ok) {
      const detail = data?.detail
      throw new Error((typeof detail === 'string' ? detail : detail?.message) || `导出失败（${resp.status}）`)
    }
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'mathagent_bank_export.json'
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('已导出 ' + (data.count || 0) + ' 道私库题目')
  } catch (err) {
    if (request === exportRequest && isCurrentScope(scope)) ElMessage.error(err.message || '导出失败')
  } finally {
    if (request === exportRequest && isCurrentScope(scope)) exporting.value = false
  }
}

watch(() => [auth.user?.id, auth.token, educationStage.value], onProfileScopeChange, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => {
  disposed = true
  listRequest += 1
  chapterRequest += 1
  exportRequest += 1
})
</script>

<style scoped>
.bank-search {
  width: 240px;
}

.bank-chapter {
  width: 200px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

/* Question and answer cells read as text, not as boxes nested inside the table. */
.bank-page :deep(.el-table .answer-display) {
  padding: 2px 0;
  border: 0;
  background: transparent;
}

.bank-page :deep(.el-table .answer-display .question-block) {
  font-weight: var(--fw-medium);
}

.card-head-right :deep(.el-button > span > .el-icon) {
  margin-right: 6px;
}

.readonly-owner {
  color: var(--ink-faint);
  font-size: var(--fs-xs);
}

.knowledge-missing {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.knowledge-picker {
  width: 100%;
}

.muted {
  color: var(--ink-muted);
}

.bank-scope-note {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  margin: 0 0 14px;
}

.bank-scope-note::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--primary);
}

@media (max-width: 760px) {
  .card-head {
    align-items: stretch;
    flex-direction: column;
  }

  .card-head-left,
  .card-head-right,
  .card-head-left .el-input,
  .card-head-left .el-select {
    width: 100%;
    max-width: none !important;
  }

  .toolbar-divider {
    display: none;
  }
}
</style>
