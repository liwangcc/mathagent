<template>
  <div class="grade-records-page page-shell">
    <GradingQuickDock />
    <el-card id="grading-start-anchor" shadow="never">
      <template #header>
        <div class="card-head">
          <div class="card-head-left">
            <span class="head-title">批改记录</span>
            <span class="head-subtitle num">共 {{ total }} 条</span>
            <HelpButton
              title="批改记录"
              content="可按学生和批次类型筛选，点击行查看逐题详情。删除只会把记录从本列表隐藏，已归档的统计、学情分析和报告数据保持不变。"
            />
          </div>
          <div class="card-head-right">
            <el-select
              v-model="studentId"
              filterable
              clearable
              placeholder="选择学生（可选）"
              class="student-filter"
              @change="search"
            >
              <el-option
                v-for="s in students"
                :key="s.id"
                :label="studentLabel(s)"
                :value="s.id"
              />
            </el-select>
            <el-select v-model="category" class="category-filter" @change="search">
              <el-option label="全部类型" value="" />
              <el-option label="日常作业" value="daily" />
              <el-option label="考试" value="exam" />
            </el-select>
            <el-button type="primary" @click="search">查询</el-button>
            <span class="toolbar-divider" aria-hidden="true" />
            <el-button
              type="danger"
              plain
              :disabled="selectedRows.length === 0"
              @click="handleDelete"
            >
              删除选中{{ selectedRows.length > 0 ? ` (${selectedRows.length})` : '' }}
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        id="grading-summary-anchor"
        :data="rows"
        v-loading="loading"
        :row-style="{ cursor: 'pointer' }"
        @row-click="viewDetail"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="50" />
        <el-table-column prop="id" label="ID" width="80">
          <template #default="{ row }"><span class="record-id">{{ row.id }}</span></template>
        </el-table-column>
        <el-table-column prop="student_name" label="学生姓名" min-width="120">
          <template #default="{ row }"><strong class="record-name">{{ row.student_name }}</strong></template>
        </el-table-column>
        <el-table-column label="成绩 / 正确率" width="150">
          <template #default="{ row }">
            <el-tag :type="recordMetricType(row)" size="small">{{ formatRecordMetric(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="正确/总数" width="110">
          <template #default="{ row }">
            {{ row.correct_count ?? 0 }} / {{ row.total_questions ?? (row.correct_count ?? 0) + (row.wrong_count ?? 0) }}
          </template>
        </el-table-column>
        <el-table-column prop="category" label="批次类型" min-width="160">
          <template #default="{ row }">
            <span class="batch-cell">
              {{ row.category || row.exam_name || '-' }}
              <el-tag v-if="row.grading_mode" :type="(row.grading_mode === 'auto' || row.grading_mode === 'simple') ? 'info' : 'success'" size="small" effect="plain">
                {{ (row.grading_mode === 'auto' || row.grading_mode === 'simple') ? '简单' : '精准' }}
              </el-tag>
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" min-width="160">
          <template #default="{ row }">
            <span class="record-time">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无批改记录" />
        </template>
      </el-table>
      <div v-if="total > pageSize" class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @size-change="search"
          @current-change="load"
        />
      </div>
    </el-card>

    <!-- 详情弹窗 -->
    <el-dialog
      v-model="detailVisible"
      title="批改详情"
      width="720px"
      destroy-on-close
    >
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="detail-header">
            <el-descriptions :column="3" border size="small">
              <el-descriptions-item label="学生">{{ detail.student_name }}</el-descriptions-item>
              <el-descriptions-item :label="detail.category === 'exam' ? '得分' : '正确率'">
                <el-tag :type="recordMetricType(detail)" size="small">{{ formatRecordMetric(detail) }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="正确/总数">
                {{ detail.correct_count ?? 0 }} / {{ detail.total_questions ?? '-' }}
              </el-descriptions-item>
            </el-descriptions>
            <el-button
              v-if="detail.source_assets?.source === 'answer_sheet' && detail.source_assets?.available !== false"
              class="source-button"
              type="primary"
              plain
              @click="openSourceAssets"
            >查看完整答题卡原卷</el-button>
            <el-alert
              v-else-if="detail.source_assets?.source === 'answer_sheet'"
              class="source-expired"
              type="info"
              :closable="false"
              show-icon
              title="原卷已按隐私保留策略清理；逐题批改结果、错题、得分和正确率仍保留。"
            />
          </div>

          <div v-if="detail.grading_details && detail.grading_details.length" class="detail-questions">
            <div
              v-for="(q, idx) in detail.grading_details"
              :key="idx"
              class="question-card"
              :class="`tone-${detailTagType(q)}`"
            >
              <div class="question-header">
                <span class="question-label">第 {{ q.question_no || q.question_number || idx + 1 }} 题</span>
                <el-tag
                  :type="detailTagType(q)"
                  size="small"
                >
                  {{ detailResultLabel(q) }}
                </el-tag>
                <span v-if="detail.category === 'exam' && q.max_score != null" class="question-score">得分: {{ q.awarded_score ?? '—' }} / {{ q.max_score }}</span>
                <span v-else-if="detail.category === 'exam' && q.score !== undefined" class="question-score">得分: {{ q.score }}</span>
              </div>
              <div v-if="q.question" class="question-text">
                <strong>题目：</strong><span v-html="renderLatex(q.question)" />
              </div>
              <div v-if="q.student_answer" class="answer-section">
                <div class="answer-tag student-tag">学生答案</div>
                <AnswerDisplay :text="q.student_answer" />
              </div>
              <div v-if="standardAnswer(q)" class="answer-section">
                <div class="answer-tag model-tag">标准答案</div>
                <AnswerDisplay :text="standardAnswer(q)" />
              </div>
              <div v-if="analysisText(q)" class="answer-section">
                <div class="answer-tag analysis-tag">解析</div>
                <AnswerDisplay :text="analysisText(q)" />
              </div>
            </div>
          </div>
          <el-empty v-else description="暂无题目详情" />
        </template>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import GradingQuickDock from '@/components/GradingQuickDock.vue'
import { useRouter } from 'vue-router'
import api from '@/api'
import { renderLatex } from '@/utils/latex'
import AnswerDisplay from '@/components/AnswerDisplay.vue'
import HelpButton from '@/components/HelpButton.vue'

const studentId = ref('')
const router = useRouter()
const students = ref([])
const rows = ref([])
const loading = ref(false)
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const selectedRows = ref([])
const category = ref('')
const page = ref(1)
const pageSize = ref(50)
const total = ref(0)

function studentLabel(student) {
  return [student.name, student.student_no, student.class_name].filter(Boolean).join(' · ')
}

function detailResultLabel(q) {
  if (q.is_correct === true || q.result === 'correct') return '正确'
  if (q.is_correct === false || q.result === 'wrong') return '错误'
  if (q.result === 'needs_review') return '待复核'
  return '部分正确'
}

function detailTagType(q) {
  if (q.is_correct === true || q.result === 'correct') return 'success'
  if (q.is_correct === false || q.result === 'wrong') return 'danger'
  return 'warning'
}

function standardAnswer(q) {
  return q.correct_answer || q.standard_answer || ''
}

function analysisText(q) {
  return q.analysis || q.explanation || q.hint || ''
}
function recordRate(row) {
  if (row?.correct_rate != null) return Number(row.correct_rate)
  if (Number(row?.scoring_version || 1) < 2 && row?.score != null) {
    const legacy = Number(row.score)
    return legacy > 1 ? legacy / 100 : legacy
  }
  if (row?.category === 'exam' && row?.score != null && Number(row?.max_score) > 0) {
    return Number(row.score) / Number(row.max_score)
  }
  return null
}

function recordMetricType(row) {
  const rate = recordRate(row)
  if (rate == null || !Number.isFinite(rate)) return 'info'
  const pct = rate * 100
  if (pct >= 90) return 'success'
  if (pct >= 60) return 'warning'
  return 'danger'
}

function formatRecordMetric(row) {
  if (row?.category === 'exam') {
    if (row.score == null) return '待复核'
    if (Number(row.scoring_version || 1) < 2) {
      const legacy = Number(row.score)
      const points = legacy <= 1 ? legacy * 100 : legacy
      return `${Math.round(points)} / 100 分`
    }
    const maxValue = Number(row.max_score)
    const maximum = row.max_score != null
      ? ` / ${Number.isInteger(maxValue) ? maxValue : maxValue.toFixed(1)}`
      : ''
    return `${Number(row.score)}${maximum} 分`
  }
  const rate = recordRate(row)
  return rate == null ? '待复核' : `${Math.round(rate * 100)}%`
}

function formatTime(t) {
  if (!t) return '-'
  try {
    const d = new Date(t)
    if (isNaN(d.getTime())) return t
    return d.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  } catch {
    return t
  }
}

async function loadStudents() {
  try {
    const data = await api.student.listAll()
    students.value = data.items || data.students || []
  } catch {
    students.value = []
  }
}

async function load() {
  loading.value = true
  try {
    const data = await api.grade.results({
      student_id: studentId.value || undefined,
      category: category.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = Array.isArray(data) ? data : data.items || data.results || []
    total.value = Array.isArray(data) ? data.length : Number(data.total ?? rows.value.length)
  } catch {
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function handleSelectionChange(selection) {
  selectedRows.value = selection
}

async function handleDelete() {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('请先选择要删除的记录')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将从批改记录列表中隐藏选中的 ${selectedRows.value.length} 条记录。已归档的统计、学情分析和报告数据不受影响；隐藏后无法在页面中恢复。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return // 用户取消
  }
  const ids = selectedRows.value.map(r => r.id)
  try {
    const res = await api.grade.delete(ids)
    ElMessage.success(`已删除 ${res.deleted} 条记录`)
    selectedRows.value = []
    if (rows.value.length <= ids.length && page.value > 1) page.value -= 1
    load()
  } catch {
    // The shared Axios interceptor already presents the server error once.
  }
}

async function viewDetail(row, column, event) {
  if (column?.type === 'selection' || event?.target?.closest?.('.el-checkbox')) return
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    const data = await api.grade.detail(row.id)
    detail.value = data
  } catch {
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

function openSourceAssets() {
  const sessionId = detail.value?.source_assets?.session_id
  if (!sessionId) return
  detailVisible.value = false
  router.push({ name: 'answer-sheet', query: { session: sessionId } })
}

onMounted(() => {
  loadStudents()
  load()
})
</script>

<style scoped>
.student-filter {
  width: 220px;
}

.category-filter {
  width: 120px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.record-id,
.record-time {
  color: var(--ink-muted);
  font-variant-numeric: tabular-nums;
}

.record-name {
  color: var(--ink);
  font-weight: var(--fw-semibold);
}

.batch-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.detail-header {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 18px;
}

.detail-header :deep(.el-descriptions) {
  width: 100%;
}

.source-expired {
  width: 100%;
}

.detail-questions {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.question-card {
  border: 1px solid var(--line);
  border-left: 3px solid var(--line);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  background: var(--card);
  animation: ma-rise-in var(--dur-3) var(--ease-emphasized) backwards;
}

.question-card.tone-success {
  border-left-color: var(--success);
}

.question-card.tone-warning {
  border-left-color: var(--warning);
}

.question-card.tone-danger {
  border-left-color: var(--danger);
}

.question-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.question-label {
  font-weight: var(--fw-semibold);
  font-size: var(--fs-md);
  color: var(--ink);
}

.question-score {
  margin-left: auto;
  font-size: var(--fs-sm);
  color: var(--ink-muted);
  font-variant-numeric: tabular-nums;
}

.question-text {
  font-size: 13px;
  color: var(--ink-soft);
  margin-top: 4px;
  line-height: 1.6;
}

.question-text.analysis {
  color: var(--primary-dark);
}
.answer-section {
  margin-top: 10px;
}

.answer-tag {
  display: inline-block;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 4px 4px 0 0;
  margin-bottom: -1px;
  position: relative;
  z-index: 1;
}

.student-tag {
  background: var(--surface-subtle);
  color: var(--ink-soft);
  border: 1px solid var(--border);
}

.model-tag {
  background: var(--primary-soft);
  color: var(--primary-dark);
  border: 1px solid var(--primary-soft);
}

.error-tag {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
  border: 1px solid var(--el-color-danger-light-9);
}

.analysis-tag {
  background: var(--accent-light);
  color: var(--accent-dark);
  border: 1px solid var(--accent-light);
}

@media (max-width: 680px) {
  .card-head-right {
    display: grid;
    grid-template-columns: 1fr 1fr;
    width: 100%;
  }

  .card-head-right :deep(.el-select) {
    grid-column: 1 / -1;
    width: 100%;
    max-width: none !important;
  }

  .toolbar-divider {
    display: none;
  }

  .question-header {
    align-items: flex-start;
    flex-wrap: wrap;
  }
}
</style>
