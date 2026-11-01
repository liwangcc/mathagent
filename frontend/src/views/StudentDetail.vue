<template>
  <div class="student-detail-page page-shell">
    <section class="hero-band profile-hero">
      <div class="profile-hero__bar">
        <el-button class="is-outline back-button" :icon="ArrowLeft" @click="goBack">返回班级</el-button>
        <div class="header-actions">
          <span class="profile-stage-label">{{ historyStageLabel(educationStage) }} · 跟随个人资料</span>
          <el-button class="is-outline" :icon="Refresh" @click="load">刷新</el-button>
          <el-dropdown :disabled="reporting" trigger="click" @command="generateReport">
            <el-button class="is-solid" :loading="reporting" :disabled="reporting">
              {{ reporting ? '生成中...' : '生成报告' }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="student"><el-icon><Document /></el-icon>学生个人报告</el-dropdown-item>
                <el-dropdown-item command="parent_letter"><el-icon><Message /></el-icon>致家长信</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <div class="profile-hero__identity">
        <div class="student-avatar">{{ studentInitial }}</div>
        <div class="student-identity-copy">
          <div class="profile-kicker">学生成长档案</div>
          <h2>{{ detail.student.name || '学生详情' }}</h2>
          <p>{{ studentMetaLine }}</p>
        </div>
        <HelpButton
          title="学生成长档案"
          content="这里集中查看学生的月考趋势、错题率、薄弱知识点和订正进展。状态与统计只基于当前班级中已归档的批阅结果。"
        />
        <div class="status-pill" :class="growthStatus.className">
          <span>当前状态</span>
          <strong>{{ growthStatus.label }}</strong>
          <small>{{ growthStatus.note }}</small>
        </div>
      </div>

      <div class="hero-metrics profile-hero__metrics">
        <div class="hero-metric" :class="heroTone(wrongRateTone)">
          <span class="hero-metric__value">{{ formatRate(detail.wrong_rate) }}</span>
          <span class="hero-metric__label">错题率 · {{ wrongRateCopy }}</span>
        </div>
        <div class="hero-metric" :class="hasMistakes ? heroTone(correctionRateTone) : ''">
          <span class="hero-metric__value">{{ hasMistakes ? formatRate(detail.correction_rate) : '—' }}</span>
          <span class="hero-metric__label">纠错率 · {{ hasMistakes ? correctionRateCopy : '暂无错题，无需纠错' }}</span>
        </div>
        <div class="hero-metric" :class="heroTone(pendingTone)">
          <span class="hero-metric__value">{{ pendingWeaknesses.length }}<small>个</small></span>
          <span class="hero-metric__label">未纠正薄弱点 · {{ pendingWeaknesses.length ? '建议安排同类题巩固' : '当前没有待处理项' }}</span>
        </div>
        <div class="hero-metric">
          <span class="hero-metric__value">{{ latestExam ? latestExam.score : '-' }}<small v-if="latestExam">分</small></span>
          <span class="hero-metric__label">最近月考 · <em :class="examDeltaClass">{{ examDeltaText }}</em></span>
        </div>
      </div>
    </section>

    <el-alert
      v-if="loadError"
      class="load-error"
      type="error"
      show-icon
      :closable="false"
    >
      <template #default>
        <div class="load-error-content">
          <span>{{ loadError }}</span>
          <el-button link type="danger" :disabled="loading" @click="load">重试</el-button>
        </div>
      </template>
    </el-alert>

    <div v-loading="loading" class="detail-grid">


      <el-card shadow="never" class="chart-card">
        <template #header>
          <div class="card-head">
            <span>月考成绩</span>
            <el-tag size="small" type="info">{{ detail.exam_scores.length }} 次</el-tag>
            <small v-if="mixedExamStages">不同学段不连线比较</small>
            <HelpButton title="月考成绩" content="折线图按月考时间展示成绩变化。没有月考导入记录时不会用日常作业分数替代。" />
          </div>
        </template>
        <div v-if="detail.exam_scores.length" class="score-chart-wrap">
          <svg class="score-chart" viewBox="0 0 640 240" role="img" aria-label="月考成绩折线图">
            <line x1="42" y1="22" x2="42" y2="198" class="axis" />
            <line x1="42" y1="198" x2="612" y2="198" class="axis" />
            <g v-for="tick in yTicks" :key="tick.value">
              <line :x1="42" :x2="612" :y1="tick.y" :y2="tick.y" class="grid-line" />
              <text x="28" :y="tick.y + 4" class="tick-label">{{ tick.value }}</text>
            </g>
            <defs>
              <linearGradient id="scoreAreaFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="currentColor" stop-opacity="0.22" />
                <stop offset="100%" stop-color="currentColor" stop-opacity="0" />
              </linearGradient>
            </defs>
            <path v-if="!mixedExamStages && scoreAreaPath" :d="scoreAreaPath" class="score-area" fill="url(#scoreAreaFill)" />
            <polyline v-if="!mixedExamStages" :points="scoreLinePoints" pathLength="1" class="score-line" />
            <g v-for="point in scorePoints" :key="point.label" class="score-point">
              <title>{{ point.shortName }}：{{ point.score }} 分</title>
              <circle :cx="point.x" :cy="point.y" r="14" class="score-hit" />
              <circle :cx="point.x" :cy="point.y" r="4.5" class="score-dot" />
              <text :x="point.x" y="220" text-anchor="middle" class="x-label">{{ point.shortName }}</text>
              <text :x="point.x" :y="point.y - 12" text-anchor="middle" class="score-label">{{ point.score }}</text>
            </g>
          </svg>
        </div>
        <el-empty v-else description="暂无月考成绩" :image-size="80" />
      </el-card>
      <aside class="insight-rail ma-stagger" aria-label="成长洞察">
        <div class="insight-item">
          <span>当前状态</span>
          <strong :class="growthStatus.className">{{ growthStatus.label }}</strong>
          <small>{{ growthStatus.note }}</small>
        </div>
        <div class="insight-item">
          <span>最近月考</span>
          <strong>{{ latestExam ? `${latestExam.score} 分` : '-' }}</strong>
          <small :class="examDeltaClass">{{ examDeltaText }}</small>
        </div>
        <div class="insight-item">
          <span>薄弱点首次出现</span>
          <strong>{{ firstWeaknessSeen }}</strong>
          <small>最近出现 {{ latestWeaknessSeen }}</small>
        </div>
      </aside>


      <el-card shadow="never" class="weakness-card">
        <template #header>
          <div class="card-head weakness-head">
            <div>
              <span>薄弱点追踪</span>
              <small>按状态、首次出现和最近出现判断是否需要继续跟进</small>
            </div>
            <el-radio-group v-model="weaknessFilter" size="small">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="pending">未纠正</el-radio-button>
              <el-radio-button value="corrected">已纠正</el-radio-button>
            </el-radio-group>
            <HelpButton title="薄弱点追踪" content="薄弱点按知识点聚合。标记已纠正表示教师确认该知识点已处理；系统自动判定为已纠正的知识点也可重新标记为未纠正。教师的选择与自动判定一致时，恢复为自动判定。" />
          </div>
        </template>
        <el-table
          :data="filteredWeaknesses"
          stripe
          :header-cell-style="{ background: 'var(--surface-subtle)', color: 'var(--ink-muted)' }"
        >
          <el-table-column label="知识点" min-width="200">
            <template #default="{ row }">
              <div class="topic-cell">
                <strong>{{ row.topic || '未知知识点' }}</strong>
                <div class="topic-meta">
                  <small>{{ historyStageLabel(row.education_stage) }} · {{ row.chapter || '未分章' }}</small>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="70">
            <template #default="{ row }">
              <el-tag v-if="row.manual" size="small" type="info">手动</el-tag>
              <span v-else class="auto-source">自动</span>
            </template>
          </el-table-column>
          <el-table-column prop="occurrences" label="次数" width="80" sortable />
          <el-table-column label="建议动作" min-width="150">
            <template #default="{ row }">
              <span class="action-cell">{{ row.suggested_action || '同类题巩固' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="row.status === 'corrected' ? 'success' : 'danger'" size="small">
                {{ row.status === 'corrected' ? '已纠正' : '未纠正' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="首次出现" width="100">
            <template #default="{ row }">{{ formatDate(row.first_seen) }}</template>
          </el-table-column>
          <el-table-column label="最近出现" width="100">
            <template #default="{ row }">{{ formatDate(row.last_seen) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="120" fixed="right">
            <template #default="{ row }">
              <el-button
                text
                :type="row.status === 'corrected' ? 'warning' : 'primary'"
                size="small"
                :loading="markingKey === topicKey(row)"
                @click="toggleWeakness(row)"
              >
                {{ row.status === 'corrected' ? '标记未纠正' : '标记已纠正' }}
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无薄弱点" />
          </template>
        </el-table>
      </el-card>

      <el-card shadow="never" class="weakness-card">
        <template #header>
          <div class="card-head weakness-head">
            <div>
              <span>错题追踪</span>
              <small>按完整原题归档；任一小问错误时整道大题仍为待订正</small>
            </div>
            <el-button type="primary" plain :loading="reporting" :disabled="reporting || !pendingWrongQuestions.length" @click="generateCorrectionPaper">
              {{ reporting ? '生成中...' : '生成原题订正卷' }}
            </el-button>
            <HelpButton title="错题追踪" content="错题按原题和订正周期保留。生成原题订正卷会优先带出待订正题目，学生在后续批阅中做对后再更新纠正状态。" />
          </div>
        </template>
        <el-table :data="detail.wrong_questions" stripe>
          <el-table-column prop="question_no" label="原题号" width="80" />
          <el-table-column prop="question" label="题干" min-width="260" show-overflow-tooltip />
          <el-table-column prop="wrong_count" label="本周期错误" width="100" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="row.status === 'corrected' ? 'success' : 'danger'">
                {{ row.status === 'corrected' ? '已纠正' : (row.cycle_no > 1 ? '再次出错' : '待订正') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="纠正依据" min-width="150">
            <template #default="{ row }">{{ correctionMethodLabel(row.close_method) }}</template>
          </el-table-column>
          <template #empty><el-empty description="暂无错题周期" /></template>
        </el-table>
      </el-card>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowLeft, Document, Message, Refresh } from '@element-plus/icons-vue'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'
import { normalizeEducationStage, historyStageLabel } from '@/utils/educationStage'
import HelpButton from '@/components/HelpButton.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const educationStage = computed(() => normalizeEducationStage(auth.user?.education_stage))
const profileScope = computed(() => JSON.stringify([auth.user?.id, auth.token, educationStage.value]))
const mixedExamStages = computed(() => new Set(detail.exam_scores.map(row => `${row.education_stage || ''}:${row.catalog_version || ''}`)).size > 1)

const loading = ref(false)
const loadError = ref('')
const reporting = ref(false)
const weaknessFilter = ref('all')
const markingKey = ref('')
let detailGeneration = 0
let actionGeneration = 0
let markingGeneration = 0

const detail = reactive({
  student: {},
  weaknesses: [],
  wrong_questions: [],
  exam_scores: [],
  correction_rate: 0,
  wrong_rate: 0,
  latest_exam_delta: null,
  latest_exam_rank_delta: null,
  has_records: false,
  graded_count: 0,
})

const studentId = computed(() => Number(route.params.studentId))
const pendingWeaknesses = computed(() =>
  detail.weaknesses.filter((item) => item.status !== 'corrected')
)
const correctedWeaknesses = computed(() =>
  detail.weaknesses.filter((item) => item.status === 'corrected')
)
// A correction rate only means something once the student has made mistakes.
const hasMistakes = computed(() => detail.weaknesses.length > 0 || detail.wrong_questions.length > 0)
const pendingWrongQuestions = computed(() =>
  detail.wrong_questions.filter((item) => item.status === 'pending')
)
const filteredWeaknesses = computed(() => {
  if (weaknessFilter.value === 'all') return detail.weaknesses
  return detail.weaknesses.filter((item) => item.status === weaknessFilter.value)
})

const studentInitial = computed(() => {
  const name = String(detail.student.name || '学')
  return name.slice(-1)
})

const latestExam = computed(() => {
  const list = detail.exam_scores || []
  return list.length ? list[list.length - 1] : null
})

const studentMetaLine = computed(() => {
  const parts = [
    detail.student.class_name || '-',
    `学号 ${detail.student.student_no || '-'}`,
  ]
  if (detail.student.phone) parts.push(`家长手机 ${maskPhone(detail.student.phone)}`)
  return parts.join(' · ')
})

const examDeltaText = computed(() => {
  if (!latestExam.value) return '暂无月考记录'
  const scoreText = formatExamDelta(detail.latest_exam_delta)
  const rankText = formatRankDelta(detail.latest_exam_rank_delta)
  if (!scoreText && !rankText) return latestExam.value.exam_name || '仅有一次记录'
  return [scoreText, rankText].filter(Boolean).join('，')
})

const examDeltaClass = computed(() => {
  const scoreDelta = Number(detail.latest_exam_delta)
  const rankDelta = Number(detail.latest_exam_rank_delta)
  if (scoreDelta > 0 || rankDelta > 0) return 'is-good'
  if (scoreDelta < 0 || rankDelta < 0) return 'is-warn'
  return ''
})

const firstWeaknessSeen = computed(() => {
  const dates = detail.weaknesses.map((item) => item.first_seen).filter(Boolean).sort()
  return dates.length ? formatDate(dates[0]) : '-'
})

const latestWeaknessSeen = computed(() => {
  const dates = detail.weaknesses.map((item) => item.last_seen).filter(Boolean).sort()
  return dates.length ? formatDate(dates[dates.length - 1]) : '-'
})

const growthStatus = computed(() => {
  if (!detail.weaknesses.length && !detail.has_records) {
    return { label: '数据积累中', note: '完成批改后会形成成长轨迹', className: 'status-neutral' }
  }
  if (!detail.weaknesses.length) {
    return { label: '表现良好', note: `已批阅 ${detail.graded_count} 份，暂无错题记录`, className: 'status-good' }
  }
  if (!pendingWeaknesses.value.length) {
    return { label: '表现稳定', note: `${correctedWeaknesses.value.length} 个薄弱点已纠正`, className: 'status-good' }
  }
  if (Number(detail.wrong_rate || 0) >= 0.35 || pendingWeaknesses.value.length >= 3) {
    return { label: '重点关注', note: '建议优先安排讲评和同类练习', className: 'status-danger' }
  }
  return { label: '持续跟进', note: '薄弱点数量可控，适合同步巩固', className: 'status-warn' }
})

const wrongRateTone = computed(() => metricTone(Number(detail.wrong_rate || 0), true))
const correctionRateTone = computed(() => metricTone(Number(detail.correction_rate || 0), false))
const pendingTone = computed(() => {
  if (pendingWeaknesses.value.length >= 3) return 'tone-danger'
  if (pendingWeaknesses.value.length > 0) return 'tone-warn'
  return 'tone-good'
})

const wrongRateCopy = computed(() => {
  const value = Number(detail.wrong_rate || 0)
  if (value >= 0.35) return '近期错误偏多'
  if (value >= 0.18) return '需要继续观察'
  return '错误率较低'
})

const correctionRateCopy = computed(() => {
  const value = Number(detail.correction_rate || 0)
  if (value >= 0.7) return '纠错效果明显'
  if (value >= 0.4) return '已有改善迹象'
  return '纠错还需推进'
})

// Exams are often out of 120 or 150; scale the axis to the real scores
// instead of pinning everything above 100 to the top line.
const scoreScale = computed(() => {
  const top = Math.max(0, ...detail.exam_scores.map((item) => Number(item.score) || 0))
  const max = top <= 100 ? 100 : top <= 120 ? 120 : top <= 150 ? 150 : Math.ceil(top / 50) * 50
  return { max, step: max === 120 ? 20 : max / 5 }
})

const yTicks = computed(() => {
  const { max, step } = scoreScale.value
  const ticks = []
  for (let value = 0; value <= max; value += step) ticks.push({ value, y: scoreToY(value) })
  return ticks
})

const scorePoints = computed(() => {
  const list = detail.exam_scores
  if (!list.length) return []
  // Inset the first and last points from the axes so value labels never collide with tick labels.
  const width = 512
  const gap = list.length === 1 ? 0 : width / (list.length - 1)
  return list.map((item, index) => {
    const score = Number(item.score || 0)
    return {
      x: list.length === 1 ? 330 : 76 + gap * index,
      y: scoreToY(score),
      score,
      label: `${item.education_stage || ''}-${item.catalog_version || ''}-${item.exam_name || index}-${item.date || ''}`,
      shortName: `${mixedExamStages.value ? historyStageLabel(item.education_stage).replace('数学', '') + '·' : ''}${shortLabel(item.exam_name || item.date || `第${index + 1}次`)}`,
    }
  })
})

// Area under the score line (display only).
const scoreAreaPath = computed(() => {
  const points = scorePoints.value
  if (points.length < 2) return ''
  return `M${points[0].x},198 ${points.map((point) => `L${point.x},${point.y}`).join(' ')} L${points[points.length - 1].x},198 Z`
})

function heroTone(tone) {
  return { 'tone-good': 'is-good', 'tone-warn': 'is-warn', 'tone-danger': 'is-bad' }[tone] || ''
}

const scoreLinePoints = computed(() =>
  scorePoints.value.map((point) => `${point.x},${point.y}`).join(' ')
)

function scoreToY(score) {
  const max = scoreScale.value.max
  const clamped = Math.max(0, Math.min(max, Number(score || 0)))
  return 198 - (clamped / max) * 176
}

function maskPhone(phone) {
  const text = String(phone || '')
  return text.length >= 7 ? `${text.slice(0, 3)}****${text.slice(-4)}` : text
}

function shortLabel(text) {
  const value = String(text)
  return value.length > 6 ? `${value.slice(0, 6)}` : value
}

async function load() {
  const scope = profileScope.value
  const id = studentId.value
  const generation = ++detailGeneration
  const current = () => scope === profileScope.value && id === studentId.value && generation === detailGeneration
  if (!auth.token || !educationStage.value || !id) return
  loading.value = true
  loadError.value = ''
  try {
    const stage = educationStage.value
    const data = await api.student.classDetail(id, { education_stage: stage })
    if (!current()) return
    detail.student = data.student || {}
    detail.weaknesses = data.weaknesses || data.weakness_list || []
    detail.exam_scores = data.exam_scores || []
    detail.correction_rate = data.correction_rate || 0
    detail.wrong_rate = data.wrong_rate || 0
    detail.latest_exam_delta = data.latest_exam_delta ?? null
    detail.latest_exam_rank_delta = data.latest_exam_rank_delta ?? null
    detail.wrong_questions = data.wrong_questions || []
    detail.has_records = Boolean(data.has_records)
    detail.graded_count = Number(data.graded_count || 0)
  } catch {
    // Keep the last successful snapshot visible; never turn a network failure
    // into a misleading empty-state result.
    if (current()) loadError.value = '学生详情加载失败，当前数据可能不是最新，请重试。'
  } finally {
    if (current()) loading.value = false
  }
}

function goBack() {
  const classId = detail.student.class_id
  router.push(classId ? `/class/${classId}/students` : '/students')
}

function toPercent(value) {
  const n = Number(value || 0)
  return Math.max(0, Math.min(100, Math.round(n * 1000) / 10))
}

function formatRate(value) {
  return `${toPercent(value).toFixed(1)}%`
}

function formatScore(value) {
  const n = Number(value || 0)
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}

function formatExamDelta(value) {
  if (value == null) return ''
  const n = Number(value)
  if (Number.isNaN(n)) return ''
  if (n > 0) return `较上次 +${formatScore(n)} 分`
  if (n < 0) return `较上次 ${formatScore(n)} 分`
  return '较上次持平'
}

function formatRankDelta(value) {
  if (value == null) return ''
  const n = Number(value)
  if (Number.isNaN(n) || n === 0) return n === 0 ? '名次持平' : ''
  return n > 0 ? `名次进步 ${n} 名` : `名次退步 ${Math.abs(n)} 名`
}

function metricTone(value, badHigh) {
  if (badHigh) {
    if (value >= 0.35) return 'tone-danger'
    if (value >= 0.18) return 'tone-warn'
    return 'tone-good'
  }
  if (value >= 0.7) return 'tone-good'
  if (value >= 0.4) return 'tone-warn'
  return 'tone-danger'
}

function formatDate(value) {
  if (!value) return '-'
  try {
    const d = new Date(value)
    if (Number.isNaN(d.getTime())) return value
    return d.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
  } catch {
    return value
  }
}


function topicKey(row) {
  return row.topic_key || `${row.education_stage ? row.education_stage + '||' : ''}${row.chapter || ''}||${row.topic || ''}`
}

async function toggleWeakness(row) {
  if (markingKey.value || !auth.token || !educationStage.value) return
  const scope = profileScope.value
  const id = studentId.value
  const generation = ++markingGeneration
  const current = () => scope === profileScope.value && id === studentId.value && generation === markingGeneration
  const key = topicKey(row)
  const nextStatus = row.status === 'corrected' ? 'pending' : 'corrected'
  markingKey.value = key
  try {
    const result = await api.student.correctWeakness(id, key, nextStatus)
    if (!current()) return
    row.status = result?.status || nextStatus
    await load()
    if (current()) ElMessage.success(nextStatus === 'corrected' ? '已标记为已纠正' : '已标记为未纠正')
  } catch {
    // handled by interceptor
  } finally {
    if (current()) markingKey.value = ''
  }
}

function correctionMethodLabel(method) {
  return {
    question_bank_id: '同一题库题自动确认',
    template_question_id: '同一模板题自动确认',
    fingerprint: '完整原题指纹一致',
    teacher_authorized_same_original: '教师确认同一原题',
    teacher_authorized_knowledge: '教师确认同知识点题',
    teacher_authorized_similar: '教师授权相似题',
  }[method] || '尚未纠正'
}

async function generateCorrectionPaper() {
  if (reporting.value) return
  if (!educationStage.value || !auth.token) return
  const scope = profileScope.value
  const id = studentId.value
  const generation = ++actionGeneration
  const current = () => scope === profileScope.value && id === studentId.value && generation === actionGeneration
  const currentStage = educationStage.value
  const eligible = pendingWrongQuestions.value.filter(row => row.education_stage === currentStage)
  if (!eligible.length) {
    ElMessage.warning('当前资料学段没有待订正题；新任务按个人资料学段生成，历史未标学段资料不会自动混入')
    return
  }
  ElMessage.info(`新订正卷使用${historyStageLabel(currentStage)}的错题`)
  reporting.value = true
  try {
    const result = await api.exercise.generate({
      mode: 'correction',
      student_id: id,
      question_count: Math.min(22, eligible.length),
    })
    if (!current()) return
    sessionStorage.setItem('mathagent_generated_correction_paper', JSON.stringify(result))
    router.push({ path: '/exercise', query: { mode: 'correction', student_id: id } })
  } catch {
    // handled by interceptor
  } finally {
    if (current()) reporting.value = false
  }
}

function reportFileName(res, fallback) {
  return res?.display_name || fallback
}

async function downloadReport(res, fallbackName) {
  if (res?.download_url) {
    await api.file.downloadUrl(res.download_url, reportFileName(res, fallbackName))
  } else if (res?.file_id) {
    await api.file.downloadGenerated(res.file_id, reportFileName(res, fallbackName))
  }
}

async function generateReport(type) {
  if (reporting.value) return
  if (!educationStage.value || !auth.token) return
  const scope = profileScope.value
  const id = studentId.value
  const generation = ++actionGeneration
  const current = () => scope === profileScope.value && id === studentId.value && generation === actionGeneration
  const studentName = detail.student.name || '学生'
  reporting.value = true
  try {
    const res = await api.student.report(id, { type, education_stage: educationStage.value })
    if (!current()) return
    if (res?.download_url || res?.file_id) {
      const suffix = type === 'parent_letter' ? '致家长信.docx' : '学习报告.docx'
      await downloadReport(res, `${studentName}${suffix}`)
      if (current()) ElMessage.success('报告已生成')
    } else {
      ElMessage.success('报告生成任务已提交')
    }
  } catch {
    // handled by interceptor
  } finally {
    if (current()) reporting.value = false
  }
}

function reloadProfileScope() {
  detailGeneration += 1
  actionGeneration += 1
  markingGeneration += 1
  Object.assign(detail, { student: {}, weaknesses: [], wrong_questions: [], exam_scores: [],
    correction_rate: 0, wrong_rate: 0, latest_exam_delta: null, latest_exam_rank_delta: null,
    has_records: false, graded_count: 0 })
  loading.value = false
  reporting.value = false
  markingKey.value = ''
  loadError.value = ''
  weaknessFilter.value = 'all'
  return load()
}

watch([profileScope, studentId], reloadProfileScope, { flush: 'sync' })
onMounted(reloadProfileScope)
</script>

<style scoped>
.student-detail-page {
  gap: 24px;
}

.load-error {
  margin-bottom: 0;
}

.load-error-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}

.load-error-content span {
  min-width: 0;
  line-height: 1.45;
}

/* ── Ink profile hero: identity, status and the four figures that matter ── */
.profile-hero {
  display: flex;
  flex-direction: column;
  gap: 26px;
  padding: 22px 34px 30px;
  animation: ma-fade-in var(--dur-4) var(--ease-out) backwards;
}

.profile-hero::after {
  content: none;
}

.profile-hero__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.back-button {
  min-height: 32px;
}

.header-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.header-actions .el-button + .el-button,
.header-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.profile-stage-label {
  padding: 4px 12px;
  border: 1px solid var(--rule);
  border-radius: var(--radius-pill);
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
}

.profile-hero__identity {
  display: flex;
  align-items: center;
  gap: 20px;
  min-width: 0;
}

.student-avatar {
  width: 88px;
  height: 88px;
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--moss);
  color: var(--on-ink);
  font-family: var(--font-serif);
  font-size: 42px;
  font-weight: 500;
}

.student-identity-copy {
  min-width: 0;
}

.profile-kicker {
  color: var(--moss);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  letter-spacing: 0.16em;
  margin-bottom: 6px;
}

.profile-hero h2 {
  margin: 0 0 8px;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: clamp(36px, 3.4vw, 52px);
  font-weight: 500;
  letter-spacing: -0.01em;
  line-height: 1.1;
}

.profile-hero p {
  margin: 0;
  color: var(--ink-muted);
  font-size: var(--fs-md);
}

.profile-hero__identity :deep(.help-button) {
  align-self: flex-start;
  margin-top: 8px;
}

.status-pill {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 200px;
  margin-left: auto;
  padding: 4px 0 4px 20px;
  border-left: 3px solid var(--ink-faint);
}

.status-pill span {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.status-pill strong {
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 26px;
  font-weight: 500;
}

.status-pill small {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.status-pill.status-good { border-left-color: var(--success); }
.status-pill.status-warn { border-left-color: var(--warning); }
.status-pill.status-danger { border-left-color: var(--danger); }

.profile-hero__metrics {
  padding-top: 24px;
  border-top: 1px solid var(--rule);
}

.profile-hero__metrics .hero-metric {
  flex: 1 1 0;
}

.profile-hero__metrics .hero-metric__label em {
  font-style: normal;
}

.profile-hero__metrics .hero-metric__label em.is-good {
  color: var(--success);
}

.profile-hero__metrics .hero-metric__label em.is-warn {
  color: var(--warning);
}

/* ── Body: trend chart with an insight rail, then the tracking tables ── */
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(260px, 320px);
  gap: 24px;
  align-items: stretch;
}

.weakness-card {
  grid-column: 1 / -1;
}

.chart-card,
.weakness-card {
  min-width: 0;
}

.insight-rail {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

.insight-item {
  flex: 1 1 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  min-width: 0;
  padding: 16px 18px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--paper-raised);
}

.insight-item span {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
}

.insight-item strong {
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 26px;
  font-weight: 500;
  line-height: 1.2;
}

.insight-item small {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  line-height: 1.4;
}

.insight-item .is-good,
.status-good {
  color: var(--success);
}

.insight-item .is-warn,
.status-warn {
  color: var(--warning);
}

.status-danger {
  color: var(--danger);
}

.status-neutral {
  color: var(--ink);
}

.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-content: space-between;
  color: var(--ink);
  font-size: var(--fs-lg);
  font-weight: 700;
}

.score-chart-wrap {
  overflow-x: auto;
  color: var(--primary);
}

.score-chart {
  width: 100%;
  min-width: 600px;
  height: 300px;
}

.axis {
  stroke: var(--border);
  stroke-width: 1;
}

.grid-line {
  stroke: var(--line);
  stroke-width: 1;
  stroke-dasharray: 3 4;
}

.score-area {
  animation: ma-fade-in 700ms var(--ease-out) 300ms backwards;
}

.score-line {
  fill: none;
  stroke: var(--primary);
  stroke-width: 2.75;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-dasharray: 1;
  stroke-dashoffset: 0;
  animation: score-line-draw 800ms var(--ease-out) 120ms backwards;
}

@keyframes score-line-draw {
  from { stroke-dashoffset: 1; }
  to { stroke-dashoffset: 0; }
}

.score-hit {
  fill: transparent;
  cursor: default;
}

.score-dot {
  fill: var(--card);
  stroke: var(--primary);
  stroke-width: 2.75;
  transition: r var(--dur-2) var(--ease-out), stroke-width var(--dur-2) var(--ease-out);
}

.score-point:hover .score-dot {
  r: 7;
  stroke-width: 3;
}

.tick-label,
.x-label,
.score-label {
  fill: var(--ink-muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.score-label {
  fill: var(--ink);
  font-family: var(--font-serif);
  font-size: 15px;
  font-weight: 500;
}

.topic-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.topic-cell strong {
  color: var(--ink);
}

.topic-cell small {
  color: var(--ink-muted);
}

.topic-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.error-tag {
  flex-shrink: 0;
}

.weakness-head {
  align-items: flex-start;
}

.weakness-head small {
  display: block;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  font-weight: 400;
  line-height: 1.4;
  margin-top: 3px;
}

.auto-source {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.action-cell {
  color: var(--ink);
  font-size: var(--fs-sm);
  line-height: 1.45;
}

@media (max-width: 1100px) {
  .detail-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .insight-rail {
    flex-direction: row;
    flex-wrap: wrap;
  }

  .insight-item {
    flex: 1 1 200px;
  }

  .status-pill {
    margin-left: 0;
  }

  .profile-hero__identity {
    flex-wrap: wrap;
  }
}

@media (max-width: 640px) {
  .profile-hero {
    padding: 18px 20px 22px;
  }

  .student-avatar {
    width: 64px;
    height: 64px;
    font-size: 30px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .score-line,
  .score-area {
    animation: none;
  }
}
</style>
