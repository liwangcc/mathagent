<template>
  <div class="stats-page page-shell">
    <section class="hero-band stats-hero">
      <svg class="hero-plot stats-plot" viewBox="0 0 420 260" preserveAspectRatio="xMaxYMid meet" aria-hidden="true">
        <line class="axis" x1="40" y1="214" x2="410" y2="214" />
        <line class="axis" x1="72" y1="20" x2="72" y2="240" />
        <path class="curve is-faint" pathLength="1" d="M40 120 C 120 120, 170 196, 240 182 S 360 84, 410 96" />
        <path class="curve" pathLength="1" d="M52 222 C 130 214, 176 164, 236 140 S 352 58, 404 42" />
        <circle class="dot" cx="236" cy="140" r="5.5" />
        <circle class="dot is-hollow" cx="330" cy="76" r="5.5" />
        <circle class="dot" cx="404" cy="42" r="4" />
      </svg>
      <div class="stats-hero__top">
        <div class="stats-hero__copy">
          <p class="hero-band__eyebrow profile-stage-label">统计分析 · {{ historyStageLabel(educationStage) }} · 跟随个人资料</p>
          <h1 class="hero-band__title">{{ currentStudent?.name || '学生学情统计' }}</h1>
          <p class="hero-band__lead">
            {{ studentId ? '已完成且纳入统计的批阅：份数、正确率与知识点错题分布。' : '选择一名学生，查看批阅份数、正确率与知识点错题分布。' }}
          </p>
        </div>
        <div class="stats-hero__picker">
          <el-select
            v-model="studentId"
            placeholder="请选择学生"
            aria-label="选择学生"
            filterable
            clearable
            @change="onStudentChange"
          >
            <el-option
              v-for="s in students"
              :key="s.id"
              :label="formatStudentLabel(s)"
              :value="s.id"
            />
          </el-select>
          <HelpButton
            title="学生统计"
            content="选择学生后查看其批阅份数、累计题数、正确率和知识点错题分布。统计只使用已完成且纳入统计的批阅数据。"
          />
        </div>
      </div>
      <div v-if="studentId && !loading && stats" class="hero-metrics stats-hero__metrics">
        <div class="hero-metric">
          <span class="hero-metric__value"><AnimatedNumber :value="metric.total" /><small>份</small></span>
          <span class="hero-metric__label">批改总数</span>
        </div>
        <div class="hero-metric">
          <span class="hero-metric__value"><AnimatedNumber :value="metric.questions" /><small>题</small></span>
          <span class="hero-metric__label">统计题数</span>
        </div>
        <div class="hero-metric" :class="accuracyTone">
          <span class="hero-metric__value"><AnimatedNumber :value="metric.accuracy" :decimals="1" /><small>%</small></span>
          <span class="hero-metric__label">正确率</span>
        </div>
      </div>
    </section>

    <div v-if="!studentId" class="stats-empty">
      <strong>先选择学生</strong>
      <span>请先选择学生查看统计数据</span>
    </div>

    <div v-else-if="loading" class="stats-loading" role="status">
      <SheetLoader :size="72" />
      <span>正在载入统计…</span>
    </div>

    <section v-else-if="stats" class="stats-dashboard">
      <div class="stats-overview">
        <article class="stats-outcome-card">
          <div class="insight-card__head">
            <div>
              <span class="insight-card__kicker">判定结果</span>
              <h2 class="insight-card__title">正确率</h2>
            </div>
            <HelpButton title="正确率" content="正确率按已记录的作答题数计算；待复核或无有效作答的题目不会冒充正确结果。" />
          </div>
          <OutcomeDonut
            :segments="outcomeSegments"
            :total="metric.questions"
            :center-value="`${metric.accuracy}%`"
            center-label="正确率"
          />
        </article>

        <article class="insight-card stats-profile">
          <div class="insight-card__head">
            <div>
              <span class="insight-card__kicker">统计对象</span>
              <h2 class="insight-card__title">学生信息</h2>
            </div>
            <HelpButton title="学生信息" content="这里显示当前统计对象的身份信息，便于确认没有选错同名学生。" />
          </div>
          <dl class="stats-ledger">
            <div><dt>姓名</dt><dd>{{ currentStudent?.name || '-' }}</dd></div>
            <div><dt>学号</dt><dd>{{ currentStudent?.student_no || '-' }}</dd></div>
            <div><dt>班级</dt><dd>{{ currentStudent?.class_name || '-' }}</dd></div>
            <div><dt>批阅份数</dt><dd>{{ metric.total }}</dd></div>
            <div><dt>统计题数</dt><dd>{{ metric.questions }}</dd></div>
            <div><dt>正确题数</dt><dd>{{ stats.correct_count ?? stats.right_count ?? '-' }}</dd></div>
            <div><dt>部分正确</dt><dd>{{ stats.partial_count ?? '-' }}</dd></div>
            <div><dt>错误题数</dt><dd>{{ stats.wrong_count ?? stats.error_count ?? '-' }}</dd></div>
          </dl>
        </article>
      </div>

      <section class="insight-card stats-knowledge">
        <div class="insight-card__head">
          <div>
            <span class="insight-card__kicker">学习画像</span>
            <h2 class="insight-card__title">知识点错题分布</h2>
            <p class="insight-card__lead">按章节和知识点统计错题，先讲评占比最高的知识点。</p>
          </div>
          <div class="stats-knowledge__aside">
            <span v-if="stats.unclassified_knowledge_count" class="status-chip is-warn">待分类 {{ stats.unclassified_knowledge_count }} 题</span>
            <HelpButton title="知识点错题分布" content="先按章节和知识点统计错题。后台使用 Qwen Flash 根据文字题干从当前学段目录中分类，分类完成后自动回填。" />
          </div>
        </div>
        <ul v-if="knowledgeList.length" class="stats-bars">
          <li
            v-for="(item, index) in knowledgeList"
            :key="`${item.education_stage}-${item.catalog_version}-${item.chapter}-${item.topic}`"
            :title="`${item.topic || '未分类'}：${item.count} 题，占 ${item.percent}%`"
          >
            <div class="stats-bars__label">
              <strong>{{ item.topic || '未分类' }}</strong>
              <span>{{ item.count }} 题 · {{ item.percent }}%</span>
            </div>
            <span class="ink-bar" aria-hidden="true"><span :style="{ width: `${item.percent}%`, '--i': index }" /></span>
            <small>{{ historyStageLabel(item.education_stage) }} · {{ item.chapter || '未分章' }} · {{ item.student_count || 0 }} 名学生</small>
          </li>
        </ul>
        <p v-else class="stats-knowledge__empty">暂无错题分布数据</p>
      </section>
    </section>

    <div v-else class="stats-empty">
      <strong>暂无统计</strong>
      <span>暂无该学生的统计数据</span>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'
import { normalizeEducationStage, historyStageLabel } from '@/utils/educationStage'
import AnimatedNumber from '@/components/AnimatedNumber.vue'
import HelpButton from '@/components/HelpButton.vue'
import OutcomeDonut from '@/components/OutcomeDonut.vue'
import SheetLoader from '@/components/SheetLoader.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const educationStage = computed(() => normalizeEducationStage(auth.user?.education_stage))
const profileScope = computed(() => JSON.stringify([auth.user?.id, auth.token, educationStage.value]))

const students = ref([])
const studentId = ref(null)
const stats = ref(null)
const loading = ref(false)
let statsGeneration = 0
let studentsGeneration = 0

const metric = reactive({
  total: 0,
  questions: 0,
  accuracy: 0,
})

const currentStudent = computed(
  () => students.value.find((s) => s.id === studentId.value) || stats.value?.student || null
)

const knowledgeList = computed(() => {
  const raw = stats.value?.knowledge_distribution
  if (!Array.isArray(raw)) return []
  const total = raw.reduce((sum, item) => sum + Number(item.count || 0), 0)
  return raw
    .map((item) => ({
      ...item,
      percent: item.pct != null
        ? toPercent(item.pct)
        : total > 0
        ? Math.round((Number(item.count || 0) / total) * 1000) / 10
        : 0,
    }))
    .filter((item) => Number(item.count || 0) > 0)
})

const accuracyTone = computed(() => {
  const p = metric.accuracy
  return p >= 85 ? 'is-good' : p >= 60 ? 'is-warn' : 'is-bad'
})

// Judged answers for the outcome ring; the ring is scaled to every counted
// question, so its correct arc matches the accuracy in the centre.
const outcomeSegments = computed(() => {
  const s = stats.value || {}
  return [
    { key: 'correct', label: '正确', value: s.correct_count ?? s.right_count },
    { key: 'partial', label: '部分正确', value: s.partial_count },
    { key: 'wrong', label: '错误', value: s.wrong_count ?? s.error_count },
  ].map((part) => {
    const value = Number(part.value ?? 0)
    return { ...part, value: Number.isFinite(value) && value > 0 ? value : 0 }
  })
})

function formatStudentLabel(s) {
  const parts = [s.name, s.student_no, s.class_name].filter(Boolean)
  return parts.join(' · ')
}

function toPercent(v) {
  if (v == null) return 0
  const n = Number(v)
  if (Number.isNaN(n)) return 0
  return n <= 1 ? Math.round(n * 1000) / 10 : Math.round(n * 10) / 10
}

async function loadStudents() {
  const scope = profileScope.value
  const generation = ++studentsGeneration
  if (!auth.token || !educationStage.value) return
  try {
    const data = await api.student.listAll()
    if (scope !== profileScope.value || generation !== studentsGeneration) return
    students.value = data.items || data.students || data || []
  } catch {
    if (scope === profileScope.value && generation === studentsGeneration) students.value = []
  }
}

function resetStats() {
  stats.value = null
  Object.assign(metric, { total: 0, questions: 0, accuracy: 0 })
}

async function loadStats() {
  const scope = profileScope.value
  const generation = ++statsGeneration
  const id = studentId.value
  const current = () => scope === profileScope.value && generation === statsGeneration && id === studentId.value
  resetStats()
  if (!id || !auth.token || !educationStage.value) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const stage = educationStage.value
    const data = await api.stats.student(id, { education_stage: stage })
    if (!current()) return
    stats.value = data || null
    const d = data || {}
    metric.total = d.total_gradings ?? d.total_exams ?? d.total ?? d.count ?? 0
    metric.questions = d.total_questions ?? 0
    metric.accuracy = toPercent(
      d.accuracy != null
        ? d.accuracy
        : d.average_correct_rate != null
        ? d.average_correct_rate
        : d.accuracy_percent ?? 0
    )
  } catch {
    if (current()) resetStats()
  } finally {
    if (current()) loading.value = false
  }
}

function onStudentChange(id) {
  if (id) {
    router.replace(`/stats/${id}`)
  } else {
    router.replace('/stats')
  }
  loadStats()
}

watch(
  () => route.params.id,
  (id) => {
    const next = id ? Number(id) : null
    if (next !== studentId.value) {
      studentId.value = next
      loadStats()
    }
  }
)

async function reloadProfileScope() {
  studentsGeneration += 1
  statsGeneration += 1
  students.value = []
  resetStats()
  loading.value = false
  const id = route.params.id ? Number(route.params.id) : null
  studentId.value = id
  await Promise.all([loadStudents(), loadStats()])
}

watch(profileScope, reloadProfileScope, { flush: 'sync' })
onMounted(reloadProfileScope)
</script>

<style scoped>
/* ── Masthead: the student as the headline, a ruled ledger of figures ── */
.stats-hero {
  display: flex;
  flex-direction: column;
  gap: 28px;
  padding: 34px 38px 30px;
  animation: ma-fade-in var(--dur-4) var(--ease-out) backwards;
}

.stats-plot {
  top: 14px;
  right: 24px;
  width: 36%;
  height: 40%;
}

.stats-hero__top {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 20px 40px;
}

.stats-hero__copy {
  min-width: 0;
  max-width: 640px;
}

.stats-hero__picker {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 0 1 320px;
  min-width: 0;
}

.stats-hero__picker :deep(.el-select) {
  flex: 1;
  min-width: 0;
}

.stats-hero__metrics {
  padding-top: 22px;
  border-top: 1px solid var(--rule);
}

/* ── Dashboard: the showcase's report panel — outcome ring, identity ledger, knowledge bars ── */
.stats-dashboard {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: clamp(18px, 2.4vw, 28px);
  border: 1px solid var(--line);
  border-radius: var(--radius-xl);
  background: var(--paper-raised);
  animation: ma-rise-in var(--dur-4) var(--ease-emphasized) backwards;
}

.stats-overview {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
}

.stats-outcome-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 20px 22px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--paper);
}

.stats-outcome-card > .outcome-donut {
  flex: 1;
}

/* Identity ledger: hairline cells, serif figures, like the showcase's readouts. */
.stats-ledger {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  margin: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--line);
}

.stats-ledger > div {
  min-width: 0;
  padding: 10px 14px;
  background: var(--paper-raised);
}

.stats-ledger dt {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
}

.stats-ledger dd {
  margin: 2px 0 0;
  overflow-wrap: anywhere;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: var(--fs-lg);
  font-variant-numeric: lining-nums tabular-nums;
}

.stats-knowledge__aside {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 0 0 auto;
}

.stats-bars {
  display: grid;
  gap: 16px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.stats-bars li {
  display: grid;
  gap: 4px;
}

.stats-bars__label {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.stats-bars__label strong {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--ink);
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
}

.stats-bars__label span {
  flex: 0 0 auto;
  color: var(--ink-soft);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.stats-bars small {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.stats-knowledge__empty {
  margin: 0;
  padding: 20px 12px;
  border: 1px dashed var(--line);
  border-radius: var(--radius-md);
  color: var(--ink-muted);
  font-size: var(--fs-sm);
  text-align: center;
}

.stats-empty,
.stats-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  min-height: 168px;
  padding: 24px;
  border: 1px dashed var(--rule);
  border-radius: var(--radius-xl);
  background: var(--paper-raised);
  color: var(--ink-muted);
  font-size: var(--fs-sm);
}

.stats-empty {
  flex-direction: column;
  gap: 4px;
}

.stats-empty strong {
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: var(--fs-xl);
  font-weight: 500;
}

@media (max-width: 900px) {
  .stats-overview {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 640px) {
  .stats-hero {
    padding: 24px 20px;
  }

  .stats-hero__picker {
    flex-basis: 100%;
  }

  .stats-ledger {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
