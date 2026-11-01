<template>
  <div class="exercise-page page-shell page-shell--narrow">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <div>
            <span class="card-title">出题练习</span>
            <span class="card-subtitle">新建出题：{{ profileStageLabel }} · 自动跟随个人资料</span>
            <span class="card-subtitle">按标签和题型从题库抽取，可导出 A3 横向双栏 Word</span>
          </div>
          <HelpButton
            title="出题练习"
            content="选择出题来源和范围后生成练习。个人薄弱巩固按学生错题抽取，原题订正卷用于复做历史错题，班级共性强化按当前班级聚合。"
          />
        </div>
      </template>

      <div class="mode-section">
        <el-radio-group v-model="mode" size="large">
          <el-radio-button value="bank">题库抽题</el-radio-button>
          <el-radio-button value="mistake">个人薄弱巩固</el-radio-button>
          <el-radio-button value="correction">原题订正卷</el-radio-button>
          <el-radio-button value="class">班级共性强化</el-radio-button>
        </el-radio-group>
        <HelpButton
          title="出题模式"
          content="题库抽题面向指定章节；个人薄弱巩固面向一名学生；原题订正卷复用已发现的历史错题；班级共性强化按当前班级的高频薄弱点生成。"
        />
      </div>

      <div class="form-section">
        <el-form label-width="88px">
          <template v-if="mode === 'bank'">
            <el-form-item label="章节选择">
              <el-select
                v-model="bankForm.chapters"
                filterable
                multiple
                placeholder="不选则从综合题库抽取"
                class="wide-control"
              >
                <el-option
                  v-for="ch in chapters"
                  :key="ch.chapter || ch"
                  :label="ch.chapter || ch"
                  :value="ch.chapter || ch"
                />
              </el-select>
            </el-form-item>
          </template>

          <template v-else-if="mode === 'mistake' || (mode === 'correction' && correctionScope === 'personal')">
            <el-form-item label="选择学生">
              <el-select
                v-model="mistakeForm.studentId"
                filterable
                placeholder="选择学生"
                class="wide-control"
              >
                <el-option
                  v-for="student in students"
                  :key="student.id"
                  :label="student.name"
                  :value="student.id"
                />
              </el-select>
            </el-form-item>
          </template>

          <template v-else-if="mode === 'class'">
            <el-form-item label="当前班级">
              <div class="current-class">
                <span>{{ auth.currentClassName || '尚未选择班级' }}</span>
                <small>顶部导航可切换</small>
              </div>
            </el-form-item>
          </template>

          <el-form-item v-if="mode === 'correction'" label="订正范围">
            <el-radio-group v-model="correctionScope">
              <el-radio-button value="personal">个人</el-radio-button>
              <el-radio-button value="class">班级统一卷</el-radio-button>
            </el-radio-group>
          </el-form-item>

          <el-form-item v-if="mode !== 'correction'" label="选题边界">
            <el-checkbox v-model="includeExtension">包含选修 / 拓展知识点</el-checkbox>
            <small style="margin-left: 8px; color: var(--el-text-color-secondary)">默认仅已分类的主干内容；题库不足时先完善知识点分类。</small>
          </el-form-item>

          <el-form-item v-if="mode === 'correction' && correctionScope === 'class'" label="当前班级">
            <div class="current-class">
              <span>{{ auth.currentClassName || '尚未选择班级' }}</span>
              <small>按全班高频待订正原题生成统一卷</small>
            </div>
          </el-form-item>

          <el-form-item v-if="mode === 'correction'" label="抽取数量">
            <el-input-number v-model="correctionCount" :min="1" :max="22" />
          </el-form-item>

          <el-form-item v-if="mode === 'mistake' || mode === 'class'" label="章节范围">
            <el-select
              v-model="weaknessForm.chapters"
              filterable
              multiple
              placeholder="不选则使用全部薄弱范围"
              class="wide-control"
            >
              <el-option
                v-for="ch in chapters"
                :key="ch.chapter || ch"
                :label="ch.chapter || ch"
                :value="ch.chapter || ch"
              />
            </el-select>
          </el-form-item>

          <el-form-item v-if="mode === 'mistake' || mode === 'class'" label="知识点范围">
            <el-select
              v-model="weaknessForm.topics"
              filterable
              multiple
              placeholder="不选则使用全部错题知识点"
              class="wide-control"
            >
              <el-option v-for="topic in availableTopics" :key="topic" :label="topic" :value="topic" />
            </el-select>
          </el-form-item>

          <el-form-item v-if="mode !== 'correction'" label="题型分布">
            <div class="type-count-panel">
              <div class="type-count-card choice-card">
                <div class="type-label">
                  <strong>选择题</strong>
                  <span>A–D 单选或多选</span>
                </div>
                <el-input-number v-model="typeCounts.choice" :min="0" :max="20" />
              </div>
              <div class="type-count-card fill-card">
                <div class="type-label">
                  <strong>填空题</strong>
                  <span>填写最终结果</span>
                </div>
                <el-input-number v-model="typeCounts.fill" :min="0" :max="20" />
              </div>
              <div class="type-count-card solution-card">
                <div class="type-label">
                  <strong>解答题</strong>
                  <span>保留完整过程</span>
                </div>
                <el-input-number v-model="typeCounts.solution" :min="0" :max="6" />
              </div>
              <div class="count-summary">
                <span>共 {{ totalCount }} 道</span>
                <small>选择、填空各最多 20 道，解答题最多 6 道，总数最多 22 道</small>
              </div>
            </div>
          </el-form-item>

          <el-form-item>
            <el-button type="primary" :loading="generating" :disabled="generating" @click="onGenerate">
              {{ generating ? '生成中...' : (mode === 'correction' ? `抽取 ${correctionCount} 道原题` : '按题型从题库抽题') }}
            </el-button>
          </el-form-item>
        </el-form>
        <HelpButton
          title="抽题条件"
          content="未填写章节或知识点时使用全部可用范围。题型数量决定生成结构，原题订正卷则按订正数量抽取已有历史错题。"
        />
      </div>
    </el-card>

    <el-card v-if="resultQuestions.length" shadow="never" class="result-section">
      <template #header>
        <div class="card-head result-head">
          <div>
            <span class="card-title">{{ paperTitle }}</span>
            <el-tag type="success" size="small">{{ resultQuestions.length }} 道题</el-tag>
            <span class="card-subtitle">{{ stageLabel }} · 本试卷学段已固定</span>
          </div>
          <HelpButton
            title="练习结果与导出"
            content="先检查题目内容，再生成学生试卷版或答案解析版。答题卡 Word 只包含作答版面，适合另行打印。"
          />
          <div class="result-actions">
            <el-button size="small" :loading="exportingAnswerSheet" @click="onExportAnswerSheet">生成答题卡 Word</el-button>
            <el-button type="primary" size="small" :loading="exporting" @click="onExport">
              生成 A3 双栏 Word
            </el-button>
            <el-button
              v-if="paperUrl"
              type="success"
              size="small"
              @click="downloadFromUrl(paperUrl, `${paperTitle}_学生试卷版.docx`)"
            >
              下载学生试卷版
            </el-button>
            <el-button
              v-if="answerUrl"
              size="small"
              @click="downloadFromUrl(answerUrl, `${paperTitle}_答案解析版.docx`)"
            >
              下载答案解析版
            </el-button>
          </div>
        </div>
      </template>
      <div class="result-content">
        <AnswerDisplay :text="resultText" />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import api from '@/api'
import AnswerDisplay from '@/components/AnswerDisplay.vue'
import HelpButton from '@/components/HelpButton.vue'
import { cleanExerciseText } from '@/utils/latex'
import { setRouteBusy } from '@/utils/routeBusy'

const auth = useAuthStore()
const route = useRoute()
const mode = ref('bank')
const includeExtension = ref(false)
const generating = ref(false)
const exporting = ref(false)
const exportingAnswerSheet = ref(false)
const paperUrl = ref('')
const answerUrl = ref('')
const paperTitle = ref('数学练习')
const paperTeachingContext = ref({})
const profileStageLabel = computed(() => {
  const stage = auth.user?.education_stage
  return stage === 'primary' ? '小学数学' : stage === 'junior' ? '初中数学' : stage === 'senior' ? '高中数学' : '请先完善学段'
})
const stageLabel = computed(() => {
  if (resultQuestions.value.length && !paperTeachingContext.value.education_stage) return '历史未标学段'
  const stage = paperTeachingContext.value.education_stage || auth.user?.education_stage
  return stage === 'primary' ? '小学' : stage === 'junior' ? '初中' : stage === 'senior' ? '高中' : '未设置学段'
})
const resultQuestions = ref([])
const resultText = ref('')
const chapters = ref([])
const students = ref([])
let chapterRequestGeneration = 0
let studentRequestGeneration = 0
const correctionScope = ref('personal')
const correctionCount = ref(10)

const bankForm = reactive({ chapters: [] })
const mistakeForm = reactive({ studentId: '' })
const weaknessForm = reactive({ chapters: [], topics: [] })
const typeCounts = reactive({
  choice: 4,
  fill: 4,
  solution: 2,
})
const totalCount = computed(
  () => Number(typeCounts.choice || 0) + Number(typeCounts.fill || 0) + Number(typeCounts.solution || 0),
)
const availableTopics = computed(() => {
  const values = []
  for (const chapter of chapters.value) {
    if (!chapter || typeof chapter !== 'object') continue
    if (weaknessForm.chapters.length && !weaknessForm.chapters.includes(chapter.chapter)) continue
    for (const topic of chapter.topics || []) {
      if (topic && !values.includes(topic)) values.push(topic)
    }
  }
  return values
})

const SECTION_META = {
  choice: { title: '一、选择题', instruction: '每小题至少有一个选项符合题意' },
  fill: { title: '二、填空题', instruction: '请将答案填写在题中横线上' },
  solution: { title: '三、解答题', instruction: '请写出必要的计算过程或推理步骤' },
}

function errorDetail(error, fallback) {
  return error?.response?.data?.detail || error?.message || fallback
}

function buildPayload() {
  const payload = {
    mode: mode.value === 'mistake' ? 'personal' : mode.value,
    include_extension: includeExtension.value,
  }
  if (mode.value !== 'correction') {
    payload.question_count = totalCount.value
    payload.type_counts = {
      choice: Number(typeCounts.choice || 0),
      fill: Number(typeCounts.fill || 0),
      solution: Number(typeCounts.solution || 0),
    }
  } else {
    payload.question_count = Number(correctionCount.value)
    payload.correction_scope = correctionScope.value
  }
  if (mode.value === 'bank') {
    payload.chapters = [...bankForm.chapters]
  } else if (mode.value === 'mistake' || (mode.value === 'correction' && correctionScope.value === 'personal')) {
    payload.student_id = Number(mistakeForm.studentId)
  } else {
    payload.class_id = Number(auth.currentClassId)
    payload.class_name = auth.currentClassName
  }
  if (mode.value === 'mistake' || mode.value === 'class') {
    payload.chapters = [...weaknessForm.chapters]
    payload.topics = [...weaknessForm.topics]
  }
  return payload
}

function validateGenerateForm() {
  if (mode.value !== 'correction' && totalCount.value < 1) {
    ElMessage.warning('至少需要选择一种题型并生成 1 道题')
    return false
  }
  if (mode.value !== 'correction' && totalCount.value > 22) {
    ElMessage.warning('单次出题最多 22 道，请调整题型数量')
    return false
  }
  if ((mode.value === 'mistake' || (mode.value === 'correction' && correctionScope.value === 'personal')) && !mistakeForm.studentId) {
    ElMessage.warning('请先选择学生')
    return false
  }
  if ((mode.value === 'class' || (mode.value === 'correction' && correctionScope.value === 'class')) && !auth.currentClassName) {
    ElMessage.warning('请先在顶部导航选择班级')
    return false
  }
  return true
}

function buildPreview(title, questions) {
  const lines = [`# ${title}`, '']
  let number = 1
  for (const type of ['choice', 'fill', 'solution']) {
    const items = questions.filter(item => item.type === type)
    if (!items.length) continue
    const meta = SECTION_META[type]
    lines.push(`## ${meta.title}`, `_${meta.instruction}_`, '')
    for (const question of items) {
      lines.push(`**${number}.** ${cleanExerciseText(question.question)}`)
      if (type === 'choice') {
        lines.push(
          `A. ${cleanExerciseText(question.options?.A || '')}　　B. ${cleanExerciseText(question.options?.B || '')}`,
          `C. ${cleanExerciseText(question.options?.C || '')}　　D. ${cleanExerciseText(question.options?.D || '')}`,
        )
      }
      lines.push(`**答案：** ${cleanExerciseText(question.answer)}`)
      lines.push(`**解析：** ${cleanExerciseText(question.solution)}`)
      lines.push(`**知识点：** ${cleanExerciseText(question.knowledge_point || '综合')}`, '')
      number += 1
    }
  }
  return lines.join('\n')
}

async function onGenerate() {
  if (generating.value) return
  if (!validateGenerateForm()) return

  generating.value = true
  resultQuestions.value = []
  resultText.value = ''
  paperUrl.value = ''
  answerUrl.value = ''
  paperTeachingContext.value = {}
  try {
    const data = await api.exercise.generate(buildPayload())
    const questions = Array.isArray(data?.exercises) ? data.exercises : []
    if (!questions.length) throw new Error('生成结果中没有可用题目')
    paperTitle.value = data.title || `${stageLabel.value}数学练习`
    paperTeachingContext.value = { ...(data.teaching_context || {}) }
    resultQuestions.value = questions
    resultText.value = buildPreview(paperTitle.value, questions)
    if (data.shortage_message) ElMessage.warning(data.shortage_message)
    else ElMessage.success(`已生成 ${questions.length} 道题`)
  } catch (error) {
    ElMessage.error(errorDetail(error, '生成失败，请稍后重试'))
  } finally {
    generating.value = false
  }
}

async function onExport() {
  if (!resultQuestions.value.length) return
  exporting.value = true
  try {
    const response = await api.exercise.exportDocx({
      title: paperTitle.value,
      questions: resultQuestions.value,
      teaching_context: paperTeachingContext.value,
      layout: 'a3_landscape_2col',
    })
    paperUrl.value = response.paper_url || response.print_url || ''
    answerUrl.value = response.answer_url || response.download_url || ''
    if (!paperUrl.value) throw new Error('服务未返回学生试卷下载地址')
    await downloadFromUrl(paperUrl.value, `${paperTitle.value}_学生试卷版.docx`)
    ElMessage.success('A3 双栏试卷与答案解析已生成')
  } catch (error) {
    ElMessage.error('导出失败：' + errorDetail(error, '未知错误'))
  } finally {
    exporting.value = false
  }
}

async function onExportAnswerSheet() {
  if (!resultQuestions.value.length) return
  exportingAnswerSheet.value = true
  try {
    const response = await api.exercise.exportAnswerSheetDocx({ title: paperTitle.value, questions: resultQuestions.value, teaching_context: paperTeachingContext.value })
    if (!response.download_url) throw new Error('服务未返回答题卡下载地址')
    await downloadFromUrl(response.download_url, `${paperTitle.value}_数学答题卡.docx`)
    ElMessage.success('A3 横向双面三栏答题卡已生成，正反面均含身份填写区')
  } catch (error) {
    ElMessage.error('答题卡导出失败：' + errorDetail(error, '未知错误'))
  } finally {
    exportingAnswerSheet.value = false
  }
}

async function downloadFromUrl(url, filename) {
  try {
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${auth.token}` },
    })
    if (!response.ok) throw new Error('下载失败')
    const blob = await response.blob()
    const anchor = document.createElement('a')
    anchor.href = URL.createObjectURL(blob)
    anchor.download = filename
    anchor.click()
    URL.revokeObjectURL(anchor.href)
  } catch (error) {
    ElMessage.error('下载失败：' + errorDetail(error, '未知错误'))
    throw error
  }
}

async function loadChapters() {
  const generation = ++chapterRequestGeneration
  chapters.value = []
  try {
    const data = await api.bank.chapters()
    if (generation !== chapterRequestGeneration) return
    chapters.value = Array.isArray(data) ? data : data?.items || []
  } catch {
    if (generation === chapterRequestGeneration) chapters.value = []
  }
}

async function loadStudents() {
  const generation = ++studentRequestGeneration
  students.value = []
  try {
    const data = await api.student.listAll()
    if (generation !== studentRequestGeneration) return
    students.value = data.items || data.students || data || []
  } catch {
    if (generation === studentRequestGeneration) students.value = []
  }
}

watch(() => [auth.user?.id, auth.user?.education_stage], () => {
  bankForm.chapters = []
  weaknessForm.chapters = []
  weaknessForm.topics = []
  mistakeForm.studentId = ''
  includeExtension.value = false
  if (!auth.user?.id || !['primary', 'junior', 'senior'].includes(auth.user?.education_stage)) {
    chapterRequestGeneration += 1
    studentRequestGeneration += 1
    chapters.value = []
    students.value = []
    return
  }
  loadChapters()
  loadStudents()
}, { flush: 'sync' })

// The usage tour reads this so it never triggers the leave-confirm below.
const leaveInterruptsWork = computed(() => generating.value || exporting.value)
watch(leaveInterruptsWork, (busy) => setRouteBusy('exercise', busy), { immediate: true })
onUnmounted(() => setRouteBusy('exercise', false))

onBeforeRouteLeave((to, from, next) => {
  if (leaveInterruptsWork.value) {
    if (confirm('练习卷正在生成或导出，离开会中断。确定离开吗？')) next()
    else next(false)
  } else {
    next()
  }
})

onMounted(() => {
  if (route.query.mode === 'correction') mode.value = 'correction'
  const requestedStudentId = Number(route.query.student_id)
  if (Number.isInteger(requestedStudentId) && requestedStudentId > 0) mistakeForm.studentId = requestedStudentId
  const savedCorrectionPaper = sessionStorage.getItem('mathagent_generated_correction_paper')
  if (mode.value === 'correction' && savedCorrectionPaper) {
    try {
      const data = JSON.parse(savedCorrectionPaper)
      const questions = Array.isArray(data?.exercises) ? data.exercises : []
      if (questions.length) {
        paperTitle.value = data.title || '原题订正卷'
        paperTeachingContext.value = { ...(data.teaching_context || {}) }
        resultQuestions.value = questions
        resultText.value = buildPreview(paperTitle.value, questions)
      }
    } catch {
      ElMessage.warning('暂存的订正卷无法恢复，请重新生成')
    } finally {
      sessionStorage.removeItem('mathagent_generated_correction_paper')
    }
  }
  loadChapters()
  loadStudents()
})
</script>

<style scoped>
.card-head > div:first-child {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
}

.card-title {
  color: var(--ink);
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
}

.card-subtitle {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.card-subtitle + .card-subtitle::before {
  content: '·';
  margin-right: 10px;
  color: var(--ink-faint);
}

/* Mode switch is the page's primary choice: one segmented control, no extra tray behind it. */
.mode-section {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 20px;
  overflow-x: auto;
}

.form-section {
  padding: 22px 22px 18px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--card-2);
  animation: ma-fade-in var(--dur-3) var(--ease-out) backwards;
}

.wide-control {
  width: 100%;
  max-width: 560px;
}

.current-class {
  display: flex;
  align-items: baseline;
  gap: 8px;
  color: var(--ink);
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
}

.current-class small {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
}

.type-count-panel {
  display: grid;
  width: min(100%, 760px);
  grid-template-columns: repeat(3, minmax(170px, 1fr));
  gap: 12px;
}

.type-count-card {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 13px 14px 13px 18px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--card);
  transition: border-color var(--dur-2) var(--ease-out), box-shadow var(--dur-3) var(--ease-out);
}

.type-count-card:hover {
  box-shadow: var(--shadow-sm);
}

.type-count-card::before {
  content: '';
  position: absolute;
  inset: 12px auto 12px 0;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--type-tone, var(--line));
}

.type-count-card :deep(.el-input-number) {
  width: 112px;
}

.type-label {
  display: flex;
  flex-direction: column;
  min-width: 64px;
  line-height: 1.35;
}

.type-label strong {
  color: var(--ink);
  font-size: var(--fs-md);
}

.type-label span {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

/* Question-type identity hues (categorical, not status). */
.choice-card {
  --type-tone: #357c95;
}

.fill-card {
  --type-tone: #b67832;
}

.solution-card {
  --type-tone: #6d62a8;
}

.count-summary {
  display: flex;
  align-items: baseline;
  grid-column: 1 / -1;
  gap: 10px;
  color: var(--ink);
}

.count-summary span {
  font-weight: var(--fw-semibold);
  font-variant-numeric: tabular-nums;
}

.count-summary small {
  color: var(--ink-muted);
}

.result-section {
  animation: ma-rise-in var(--dur-4) var(--ease-emphasized) backwards;
}

.result-head {
  align-items: flex-start;
}

.result-actions {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}

.result-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.result-content {
  font-size: 14px;
  line-height: 1.8;
}

@media (max-width: 820px) {
  .type-count-panel {
    grid-template-columns: 1fr;
  }

  .count-summary {
    grid-column: 1;
  }
}

@media (max-width: 640px) {
  .card-head,
  .result-head {
    align-items: stretch;
    flex-direction: column;
  }

  .result-actions {
    justify-content: flex-start;
  }

  .form-section {
    padding: 16px;
  }

  .type-count-card {
    align-items: flex-start;
    flex-direction: column;
  }

  .type-count-card :deep(.el-input-number) {
    width: 100%;
  }
}
</style>
