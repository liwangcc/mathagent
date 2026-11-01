<template>
  <div class="pending-answers-page page-shell">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <div class="card-head-left">
            <span class="head-title">待入库答案</span>
            <el-tag type="info" size="small" class="num">共 {{ total }} 条</el-tag>
            <HelpButton
              title="待入库答案"
              content="这里是从批阅或识别流程中暂存、尚未写入题库的答案。确认入库前请检查题目和标准答案。忽略只会让这道题不再出现在此列表，不会改动任何学生的批改记录或成绩。"
            />
          </div>
          <div class="card-head-right">
            <span v-if="selectedRows.length" class="selection-hint num">已选 {{ selectedRows.length }} 条</span>
            <el-button
              type="danger"
              plain
              :disabled="selectedRows.length === 0"
              :loading="deleting"
              @click="onBatchDelete"
            >
              批量忽略 ({{ selectedRows.length }})
            </el-button>
            <el-button
              type="success"
              :disabled="selectedRows.length === 0"
              :loading="confirming"
              @click="onBatchConfirm"
            >
              批量入库 ({{ selectedRows.length }})
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        :data="rows"
        v-loading="loading"
        stripe
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="50" />
        <el-table-column label="原学段" width="120">
          <template #default="{ row }"><span class="stage-cell">{{ row.teaching_context?.education_stage === 'primary' ? '小学数学' : row.teaching_context?.education_stage === 'junior' ? '初中数学' : row.teaching_context?.education_stage === 'senior' ? '高中数学' : '历史未标学段' }}</span></template>
        </el-table-column>
        <el-table-column label="题目预览" min-width="200">
          <template #default="{ row }">
            <div v-html="renderLatex(row.question_preview)" />
          </template>
        </el-table-column>
        <el-table-column label="学生答案" width="140">
          <template #default="{ row }">
            <div v-html="renderLatex(row.student_answer)" />
          </template>
        </el-table-column>
        <el-table-column label="知识点" min-width="240">
          <template #default="{ row }">
            <div class="knowledge-cell">
              <el-tag v-if="row.knowledge_source === 'grading'" size="small" type="success" effect="plain">沿用批改分类</el-tag>
              <el-tag v-else-if="row.knowledge_source === 'teacher'" size="small" effect="plain">手动选择</el-tag>
              <el-tag v-else-if="row.classification_status === 'pending'" size="small" type="info">{{ CLASSIFICATION_LABELS.pending }}</el-tag>
              <el-tag v-else size="small" type="danger">{{ CLASSIFICATION_LABELS.failed }}</el-tag>
              <el-cascader
                v-model="row.knowledge"
                :options="catalogFor(row)"
                :disabled="!row.teaching_context?.education_stage"
                :placeholder="row.teaching_context?.education_stage ? '手动选择知识点' : '历史未标学段，无法选择'"
                size="small"
                clearable
                filterable
                class="knowledge-picker"
                @change="value => onPickKnowledge(row, value)"
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="AI标准答案" min-width="240">
          <template #default="{ row }">
            <el-input
              v-model="row.ai_answer"
              size="small"
              placeholder="AI生成的标准答案"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="onConfirmOne(row)">确认入库</el-button>
            <el-button text type="danger" size="small" @click="onDeleteOne(row)">忽略</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无待入库答案" />
        </template>
      </el-table>
      <div v-if="total > pageSize" class="pager">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          background
          @current-change="load"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { renderLatex } from '@/utils/latex'
import HelpButton from '@/components/HelpButton.vue'
import { CLASSIFICATION_LABELS, catalogOptions } from '@/utils/knowledgeClassification'

const rows = ref([])
const loading = ref(false)
const confirming = ref(false)
const deleting = ref(false)
const selectedRows = ref([])
const catalogs = ref({})
const page = ref(1)
const pageSize = 50
const total = ref(0)
// Unsaved answer/knowledge edits survive reloads (after confirming another
// row, ignoring, or paging) until the row itself leaves the list.
const drafts = new Map()

function rowKey(row) {
  return `${row.question}|${JSON.stringify(row.teaching_context || {})}`
}

function rememberDrafts() {
  for (const row of rows.value) {
    drafts.set(rowKey(row), { ai_answer: row.ai_answer, knowledge: row.knowledge, knowledge_source: row.knowledge_source })
  }
}

function catalogFor(row) {
  return catalogs.value[row.teaching_context?.education_stage] || []
}

async function loadCatalogs(items) {
  const stages = [...new Set(items.map(row => row.teaching_context?.education_stage).filter(Boolean))]
  await Promise.all(stages.filter(stage => !catalogs.value[stage]).map(async (stage) => {
    try {
      const data = await api.bank.chapters({ education_stage: stage })
      catalogs.value = { ...catalogs.value, [stage]: catalogOptions(Array.isArray(data) ? data : data?.items) }
    } catch {
      /* the status tag still shows; only the picker stays empty */
    }
  }))
}

// The grading workflow already classified most questions; reuse that label.
function gradingKnowledge(row) {
  return row.classification_status === 'classified' && row.chapter && row.topic ? [row.chapter, row.topic] : []
}

function withKnowledge(row) {
  const reused = gradingKnowledge(row)
  return { ...row, knowledge: reused, knowledge_source: reused.length ? 'grading' : '' }
}

function onPickKnowledge(row, value) {
  const reused = gradingKnowledge(row)
  const picked = Array.isArray(value) && value.length === 2 ? value : reused
  row.knowledge = picked
  const sameAsGrading = picked[0] === reused[0] && picked[1] === reused[1]
  row.knowledge_source = !picked.length ? '' : sameAsGrading ? 'grading' : 'teacher'
}

function knowledgePayload(row) {
  const [chapter = '', topic = ''] = row.knowledge || []
  return { chapter, topic }
}

async function load() {
  rememberDrafts()
  loading.value = true
  try {
    let data = await api.bank.pendingAnswers({ page: page.value, page_size: pageSize })
    total.value = Number(data?.total ?? data?.count ?? 0) || 0
    if (page.value > 1 && !(data?.items || []).length && total.value) {
      page.value = Math.ceil(total.value / pageSize)
      data = await api.bank.pendingAnswers({ page: page.value, page_size: pageSize })
    }
    const items = data?.items || data?.answers || data || []
    rows.value = Array.isArray(items)
      ? items.map(withKnowledge).map(row => ({ ...row, ...(drafts.get(rowKey(row)) || {}) }))
      : []
    loadCatalogs(rows.value)
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}

function onSelectionChange(selection) {
  selectedRows.value = selection
}

async function onConfirmOne(row) {
  confirming.value = true
  try {
    await api.bank.confirmAnswers({ answers: [{ ...row, answer: row.ai_answer, ...knowledgePayload(row) }] })
    ElMessage.success(row.knowledge?.length ? '已入库' : '已入库，知识点将由后台继续分类')
    notifyPendingChanged()
    load()
  } catch {
    /* error handled by interceptor */
  } finally {
    confirming.value = false
  }
}

async function onBatchConfirm() {
  confirming.value = true
  try {
    await api.bank.confirmAnswers({ answers: selectedRows.value.map(r => ({ ...r, answer: r.ai_answer, ...knowledgePayload(r) })) })
    const unlabelled = selectedRows.value.filter(r => !r.knowledge?.length).length
    ElMessage.success(`已批量入库 ${selectedRows.value.length} 条` + (unlabelled ? `，其中 ${unlabelled} 条知识点将由后台继续分类` : ''))
    notifyPendingChanged()
    load()
  } catch {
    /* error handled by interceptor */
  } finally {
    confirming.value = false
  }
}

// Dismissal only hides the suggestion; the server never edits grade records.
const DISMISS_NOTE = '忽略后这道题不再出现在待入库列表（其他学生记录中的同一道题也一并隐藏），学生的批改记录和成绩不会改变。'

function notifyPendingChanged() {
  window.dispatchEvent(new Event('mathagent:pending-answers-changed'))
}

async function onDeleteOne(row) {
  try {
    await ElMessageBox.confirm(DISMISS_NOTE, '忽略这道题', {
      confirmButtonText: '忽略',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  deleting.value = true
  try {
    await api.bank.deletePendingAnswers({ questions: [row.question || row.question_preview], sources: [{ grading_id: row.grading_id, detail_index: row.detail_index }] })
    ElMessage.success('已忽略')
    notifyPendingChanged()
    load()
  } catch {
    /* error handled by interceptor */
  } finally {
    deleting.value = false
  }
}

async function onBatchDelete() {
  try {
    await ElMessageBox.confirm(`将忽略选中的 ${selectedRows.value.length} 道题。${DISMISS_NOTE}`, '批量忽略', {
      confirmButtonText: '忽略',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  deleting.value = true
  try {
    const questions = selectedRows.value.map(r => r.question || r.question_preview)
    await api.bank.deletePendingAnswers({ questions, sources: selectedRows.value.map(row => ({ grading_id: row.grading_id, detail_index: row.detail_index })) })
    ElMessage.success(`已忽略 ${selectedRows.value.length} 道题`)
    notifyPendingChanged()
    load()
  } catch {
    /* error handled by interceptor */
  } finally {
    deleting.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.selection-hint {
  color: var(--primary-dark);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  animation: ma-fade-in var(--dur-2) var(--ease-out) backwards;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.stage-cell {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.knowledge-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.knowledge-picker {
  width: 100%;
}

.pending-answers-page :deep(.el-table .katex) {
  color: var(--ink);
}

@media (max-width: 680px) {
  .card-head {
    align-items: stretch;
    flex-direction: column;
  }

  .card-head-right {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
}
</style>
