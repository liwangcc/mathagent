<template>
  <div class="reports-page page-shell">
    <section class="hero-band report-hero is-kraft">
      <svg class="hero-plot report-plot" viewBox="0 0 520 200" preserveAspectRatio="xMaxYMid slice" aria-hidden="true">
        <line class="axis" x1="0" y1="176" x2="520" y2="176" />
        <g class="report-plot__bars">
          <rect x="250" y="120" width="26" height="56" rx="4" />
          <rect x="296" y="92" width="26" height="84" rx="4" />
          <rect x="342" y="104" width="26" height="72" rx="4" />
          <rect x="388" y="60" width="26" height="116" rx="4" />
          <rect x="434" y="72" width="26" height="104" rx="4" />
          <rect x="480" y="36" width="26" height="140" rx="4" />
        </g>
        <path class="curve" pathLength="1" d="M200 150 C 290 120, 330 110, 400 70 S 480 34, 520 26" />
      </svg>
      <div class="report-hero__top">
        <div class="report-hero__copy">
          <p class="hero-band__eyebrow profile-stage-label">成绩报告 · {{ historyStageLabel(educationStage) }} · 跟随个人资料</p>
          <h1 class="hero-band__title">{{ auth.currentClassName }} 学情报告</h1>
          <p class="hero-band__lead">{{ periodLabel }}的批阅记录、作答量与正确率。班级报告、学生档案与家长信在右侧生成。</p>
        </div>
        <div class="report-hero__period">
          <el-radio-group v-model="period" class="ink-segmented" @change="load">
            <el-radio-button value="week">近7天</el-radio-button>
            <el-radio-button value="month">近30天</el-radio-button>
            <el-radio-button value="all">全部</el-radio-button>
          </el-radio-group>
          <HelpButton title="成绩报告" content="切换近7天、近30天或全部，查看当前班级的批阅记录、作答量、正确率、优秀学生和需要关注的学生（两组不会重复出现同一名学生）。" />
        </div>
      </div>
      <div class="hero-metrics report-hero__metrics">
        <div class="hero-metric">
          <span class="hero-metric__value">{{ metrics.total }}<small>份</small></span>
          <span class="hero-metric__label">批阅记录 · {{ periodLabel }}</span>
        </div>
        <div class="hero-metric">
          <span class="hero-metric__value">{{ metrics.questions }}<small>题</small></span>
          <span class="hero-metric__label">作答题数</span>
        </div>
        <div class="hero-metric" :class="heroAccuracyTone">
          <span class="hero-metric__value">{{ Number(metrics.accuracy || 0).toFixed(1) }}<small>%</small></span>
          <span class="hero-metric__label">正确率</span>
        </div>
      </div>
    </section>

    <div class="report-layout">
      <div class="report-main">
        <el-card shadow="never" class="report-body-card">
          <div v-loading="loading" class="report-body">
            <el-empty v-if="!loading && isEmpty" description="暂无报告数据" />

            <template v-else-if="!loading">
              <blockquote v-if="report.summary" class="summary-quote">
                <span class="summary-quote__label">报告摘要</span>
                <p class="summary-text">{{ report.summary }}</p>
              </blockquote>

              <div v-if="hasList(report.top_students) || hasList(report.weak_students)" class="rank-grid">
                <section v-for="group in rankGroups" :key="group.key" class="rank-card" :class="group.tone">
                  <span class="report-section__title"><i class="section-dot" :class="group.tone" aria-hidden="true" />{{ group.title }}</span>
                  <ol class="rank-list">
                    <li v-for="(s, index) in group.rows" :key="`${group.key}-${s.student_no}-${s.name}`" class="rank-row">
                      <span class="rank-row__no">{{ index + 1 }}</span>
                      <span class="rank-row__avatar" aria-hidden="true">{{ String(s.name || '?').slice(0, 1) }}</span>
                      <span class="rank-row__who">
                        <strong>{{ s.name }}</strong>
                        <small>{{ s.student_no || '—' }} · {{ s.profile_count }} 份 · {{ s.total_questions }} 题</small>
                      </span>
                      <span class="rank-row__bar" aria-hidden="true"><i :style="{ width: `${Math.min(100, Math.max(0, Number(s.accuracy) || 0))}%` }" /></span>
                      <span class="rank-row__value">{{ s.accuracy }}<small>%</small></span>
                    </li>
                  </ol>
                </section>
              </div>

              <section v-if="hasList(report.knowledge_distribution)" class="report-section">
                <span class="report-section__title">知识点错题分布</span>
                <div class="meter-list">
                  <div
                    v-for="item in report.knowledge_distribution"
                    :key="`${item.education_stage}-${item.catalog_version}-${item.chapter}-${item.topic}`"
                    class="meter-row knowledge-row"
                    :title="`${item.topic || '未分类'}：${item.count ?? 0} 题，占 ${toPercent(item.pct ?? item.percent ?? item.ratio ?? item.value)}%`"
                  >
                    <div class="meter-row__label knowledge-label">
                      <strong>{{ item.topic || '未分类' }}</strong>
                      <small>{{ historyStageLabel(item.education_stage) }} · {{ item.chapter || '未分章' }} · {{ item.student_count || 0 }} 名学生</small>
                    </div>
                    <div class="meter-row__track">
                      <div class="meter-row__fill" :style="{ width: `${toPercent(item.pct ?? item.percent ?? item.ratio ?? item.value)}%` }" />
                    </div>
                    <div class="meter-row__value knowledge-count">
                      <strong>{{ item.count ?? 0 }} 题</strong>
                      <small>{{ toPercent(item.pct ?? item.percent ?? item.ratio ?? item.value) }}%</small>
                    </div>
                  </div>
                </div>
              </section>

              <el-card v-if="report.raw && !hasMainSections" shadow="never" class="section-card">
                <template #header><span class="section-title">原始数据</span></template>
                <pre class="raw-json">{{ JSON.stringify(report.raw, null, 2) }}</pre>
              </el-card>
            </template>
          </div>
        </el-card>

        <el-card shadow="never" class="share-card">
          <section class="letter-share-panel" aria-label="家长专属链接管理">
          <div class="letter-share-head">
            <div>
              <strong>已生成链接</strong>
              <span>有效 {{ activeLetterShareCount }} 个；可单独复制或立即撤销</span>
            </div>
            <div class="inline-actions">
              <el-button size="small" :disabled="sharesLoading || !activeLetterShareCount" @click="copyActiveShares">复制全部有效链接</el-button>
              <el-button size="small" :loading="sharesLoading" :disabled="sharesLoading || !auth.currentClassId" @click="loadLetterShares">刷新</el-button>
            </div>
          </div>
          <el-table v-if="letterShares.length" :data="letterShares" class="letter-share-table" size="small" stripe v-loading="sharesLoading">
            <el-table-column label="学生" min-width="104">
              <template #default="{ row }">{{ row.student_name }} <span class="student-no">{{ row.student_no || '' }}</span></template>
            </el-table-column>
            <el-table-column label="状态" width="68">
              <template #default="{ row }"><el-tag size="small" :type="shareStatusType(row.status)">{{ row.status }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="view_count" label="查看次数" width="72" />
            <el-table-column label="学段" width="72">
              <template #default="{ row }">{{ historyStageLabel(row.education_stage) }}</template>
            </el-table-column>
            <el-table-column label="有效期" min-width="132">
              <template #default="{ row }">
                <time class="share-expiry">{{ formatDateTime(row.expires_at) }}</time>
                <small v-if="row.status === '有效' && remainingDays(row.expires_at) != null" class="expiry-hint" :class="{ 'is-soon': remainingDays(row.expires_at) <= 3 }">
                  剩 {{ remainingDays(row.expires_at) }} 天
                </small>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="108" align="right" fixed="right">
              <template #default="{ row }">
                <div class="share-actions">
                  <el-button link type="primary" :disabled="row.status !== '有效'" @click="copyShare(row)">复制</el-button>
                  <el-button link type="danger" :loading="revokingShareId === row.id" :disabled="row.status !== '有效' || !!revokingShareId" @click="revokeShare(row)">撤销</el-button>
                </div>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else-if="!sharesLoading" :image-size="52" description="尚未生成家长专属链接" />
        </section>
        </el-card>
      </div>

      <aside class="report-rail">
        <div class="rail-head">
          <div>
            <span class="rail-kicker">报告生成</span>
            <strong>{{ auth.currentClassName }}</strong>
          </div>
          <HelpButton
            title="报告生成"
            content="报告按当前班级和权限生成。班级报告汇总整体学情，学生报告聚焦个人，家长信用于对外沟通，请先检查内容再分享。"
          />
        </div>
        <div class="report-action-grid ma-stagger">
        <div class="report-action">
          <div>
            <strong>班级学情报告</strong>
            <span>生成极简素雅 Word 报告，汇总错题、薄弱知识点和订正进展</span>
          </div>
          <el-button type="primary" :loading="generating === 'class'" :disabled="generating || !auth.currentClassId" @click="generateClassReport">
            {{ generating === 'class' ? '生成中...' : '生成并下载' }}
          </el-button>
        </div>

        <div class="report-action">
          <div>
            <strong>学生成长档案</strong>
            <span>选择学生后生成个人报告，或生成适合家长阅读的家长信</span>
          </div>
          <div class="inline-actions">
            <el-select
              v-model="selectedStudentId"
              filterable
              placeholder="选择学生"
              :disabled="generating || !auth.currentClassId"
              @focus="loadClassStudents"
            >
              <el-option
                v-for="s in classStudents"
                :key="s.id"
                :label="`${s.name} (${s.student_no || '-'})`"
                :value="s.id"
              />
            </el-select>
            <el-dropdown :disabled="generating || !selectedStudentId" trigger="click" @command="generateStudentReport">
              <el-button :loading="generating === 'student'" :disabled="generating">
                {{ generating === 'student' ? '生成中...' : '生成报告' }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="student">学生报告</el-dropdown-item>
                  <el-dropdown-item command="parent_letter">致家长信</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </div>

        <div class="report-action">
          <div>
            <strong>批量家长信</strong>
            <span>按个人资料学段生成并冻结内容</span>
            <span>为每名学生准备独立链接；有手机号使用后四位校验，无手机号也会生成但不设密码。再次准备时复用有效链接，并把有效期顺延为 30 天</span>
            <HelpButton title="批量家长信" content="批量生成后会为学生建立独立链接。手机号后四位只用于有手机号的学生校验，没有手机号的链接不设置密码，请谨慎分享。" />
          </div>
          <div class="inline-actions">
            <el-button :loading="generating === 'batch'" :disabled="generating || !auth.currentClassId" @click="generateBatchReport(false)">
              {{ generating === 'batch' ? '生成中...' : '准备并复制链接' }}
            </el-button>
            <el-button plain :disabled="generating || !auth.currentClassId" @click="regenerateBatchReport">
              重新生成
            </el-button>
          </div>
        </div>
      </div>
      </aside>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Document, Files } from '@element-plus/icons-vue'
import api from '@/api'
import { normalizeEducationStage, historyStageLabel } from '@/utils/educationStage'
import { useAuthStore } from '@/stores/auth'
import HelpButton from '@/components/HelpButton.vue'

const auth = useAuthStore()
const educationStage = computed(() => normalizeEducationStage(auth.user?.education_stage))
const profileScope = computed(() => JSON.stringify([auth.user?.id, auth.token, educationStage.value]))
const period = ref('week')
// Display-only helpers for the stat tiles.
const periodLabel = computed(() => ({ week: '近7天', month: '近30天', all: '全部时间' }[period.value] || '当前周期'))
const heroAccuracyTone = computed(() => {
  const value = Number(metrics.accuracy || 0)
  return value >= 85 ? 'is-good' : value >= 60 ? 'is-warn' : 'is-bad'
})
// Ranked student lists render the same rows the tables showed.
const rankGroups = computed(() => [
  { key: 'top', title: '优秀学生', tone: 'is-good', rows: hasList(report.top_students) ? report.top_students : [] },
  { key: 'weak', title: '需要关注的学生', tone: 'is-bad', rows: hasList(report.weak_students) ? report.weak_students : [] },
].filter((group) => group.rows.length))
const accuracyTone = computed(() => {
  const value = Number(metrics.accuracy || 0)
  return value >= 85 ? 'is-good' : value >= 60 ? 'is-warn' : 'is-bad'
})
const loading = ref(false)
const selectedStudentId = ref(null)
const classStudents = ref([])
const generating = ref('')
const letterShares = ref([])
const sharesLoading = ref(false)
const revokingShareId = ref(null)
let reportLoadGeneration = 0
let studentsGeneration = 0
let sharesGeneration = 0
let actionGeneration = 0

const report = reactive({
  summary: '',
  top_students: [],
  weak_students: [],
  knowledge_distribution: [],
  raw: null,
})

const metrics = reactive({
  total: 0,
  questions: 0,
  accuracy: 0,
})

const activeLetterShareCount = computed(
  () => letterShares.value.filter((share) => share.status === '有效').length
)

const hasMainSections = computed(
  () =>
    !!report.summary ||
    hasList(report.top_students) ||
    hasList(report.weak_students) ||
    hasList(report.knowledge_distribution)
)

const isEmpty = computed(
  () => !hasMainSections.value && !report.raw && !metrics.total
)

function hasList(arr) {
  return Array.isArray(arr) && arr.length > 0
}

function toPercent(v) {
  if (v == null) return 0
  const n = Number(v)
  if (Number.isNaN(n)) return 0
  return n <= 1 ? Math.round(n * 1000) / 10 : Math.round(n * 10) / 10
}

function resetReport() {
  report.summary = ''
  report.top_students = []
  report.weak_students = []
  report.knowledge_distribution = []
  report.raw = null
  metrics.total = 0
  metrics.questions = 0
  metrics.accuracy = 0
}

async function load() {
  const scope = profileScope.value
  const classId = auth.currentClassId
  const generation = ++reportLoadGeneration
  const current = () => scope === profileScope.value && generation === reportLoadGeneration && classId === auth.currentClassId
  if (!classId || !auth.token || !educationStage.value) {
    resetReport()
    loading.value = false
    return
  }
  loading.value = true
  resetReport()
  try {
    const data = await api.report.summary({
      education_stage: educationStage.value,
      period: period.value,
      class_id: classId,
      type: 'all',
    })
    if (!current()) return
    const d = data || {}
    report.summary = d.summary || d.report || ''
    report.top_students = Array.isArray(d.top_students) ? d.top_students : []
    report.weak_students = Array.isArray(d.weak_students) ? d.weak_students : []
    report.knowledge_distribution = Array.isArray(d.knowledge_distribution) ? d.knowledge_distribution : []

    metrics.total = Number(d.total ?? d.stats?.profile_count ?? 0)
    metrics.questions = Number(d.total_questions ?? d.stats?.total_questions ?? 0)
    metrics.accuracy = Number(d.accuracy ?? toPercent(d.stats?.correct_rate ?? 0))

    if (
      !report.summary &&
      !hasList(report.top_students) &&
      !hasList(report.weak_students) &&
      !hasList(report.knowledge_distribution)
    ) {
      report.raw = d
    }
  } catch {
    /* error handled by interceptor */
  } finally {
    if (current()) loading.value = false
  }
}

function currentClassName() {
  return auth.currentClassName || '班级'
}

function getErrorMessage(err, fallback = '操作失败，请稍后重试') {
  return (
    err?.response?.data?.detail ||
    err?.response?.data?.message ||
    err?.response?.data?.error ||
    err?.message ||
    fallback
  )
}

async function downloadGenerated(res, filename) {
  const finalName = res?.display_name || filename
  if (res?.download_url) {
    await api.file.downloadUrl(res.download_url, finalName)
  } else if (res?.file_id) {
    await api.file.downloadGenerated(res.file_id, finalName)
  } else {
    throw new Error(res?.error || res?.message || '后端未返回可下载文件信息')
  }
  return finalName
}

async function loadClassStudents() {
  const scope = profileScope.value
  const classId = auth.currentClassId
  const generation = ++studentsGeneration
  const current = () => scope === profileScope.value && classId === auth.currentClassId && generation === studentsGeneration
  if (!classId || !auth.token || !educationStage.value) return
  try {
    // A plain roster is enough for the picker; the class analytics table is
    // far heavier. /student/list is already scoped to the current class.
    const data = await api.student.listAll()
    if (!current()) return
    classStudents.value = (data.students || []).filter((student) => !student.class_id || student.class_id === classId)
  } catch {
    if (current()) classStudents.value = []
  }
}

function shareUrl(share) {
  return `${window.location.origin}${share?.url || ''}`
}

function formatDateTime(value) {
  if (!value) return '-'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', { hour12: false })
}

function remainingDays(value) {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return null
  return Math.max(0, Math.ceil((parsed.getTime() - Date.now()) / 86400000))
}

function showLetterWarnings(res) {
  const lines = []
  const skipped = Array.isArray(res?.skipped) ? res.skipped : []
  const empty = Array.isArray(res?.empty_content) ? res.empty_content : []
  if (skipped.length) lines.push(`未生成 ${skipped.length} 人：${skipped.join('、')}。请补齐数据后重新生成。`)
  if (empty.length) lines.push(`${empty.length} 名学生在当前学段暂无批阅或考试数据，信件只有问候语，建议确认后再发送：${empty.join('、')}。`)
  if (!lines.length) return
  ElMessageBox.alert(lines.join(' '), '发送前请留意', { type: 'warning', confirmButtonText: '知道了' })
}

function shareStatusType(status) {
  return ({ 有效: 'success', 已过期: 'info', 已锁定: 'warning', 已失效: 'info' })[status] || 'info'
}

async function loadLetterShares() {
  const scope = profileScope.value
  const classId = auth.currentClassId
  const stage = educationStage.value
  const generation = ++sharesGeneration
  const current = () => scope === profileScope.value && classId === auth.currentClassId && generation === sharesGeneration
  if (!classId || !auth.token || !stage) return
  sharesLoading.value = true
  try {
    const data = await api.classroom.letterShares(classId, { education_stage: stage })
    if (current()) letterShares.value = data.shares || []
  } catch {
    if (current()) letterShares.value = []
  } finally {
    if (current()) sharesLoading.value = false
  }
}

async function writeClipboard(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(successMessage)
    return true
  } catch {
    ElMessage.warning('链接已生成，但浏览器未允许自动复制；请在下方逐条复制')
    return false
  }
}

async function copyShare(share) {
  if (share?.status !== '有效') return
  await writeClipboard(shareUrl(share), `${share.student_name || '该学生'}的链接已复制`)
}

async function copyActiveShares() {
  const rows = letterShares.value.filter((share) => share.status === '有效')
  if (!rows.length) return
  const text = rows.map((share) => `${share.student_name || '学生'}：${shareUrl(share)}`).join('\n')
  await writeClipboard(text, `已复制 ${rows.length} 个有效链接`)
}

async function revokeShare(share) {
  if (share?.status !== '有效' || revokingShareId.value) return
  const scope = profileScope.value
  const classId = auth.currentClassId
  const current = () => scope === profileScope.value && classId === auth.currentClassId
  try {
    await ElMessageBox.confirm(
      `撤销后，${share.student_name || '该学生'}的这个链接将立即无法访问。`,
      '撤销家长链接',
      { type: 'warning', confirmButtonText: '确认撤销', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  if (!current()) return
  revokingShareId.value = share.id
  try {
    await api.classroom.revokeLetterShare(share.id)
    if (!current()) return
    ElMessage.success('链接已撤销')
    await loadLetterShares()
  } catch {
    // handled by interceptor
  } finally {
    if (current()) revokingShareId.value = null
  }
}

async function generateClassReport() {
  if (generating.value) return
  if (!auth.token || !educationStage.value || !auth.currentClassId) return
  const scope = profileScope.value
  const classId = auth.currentClassId
  const generation = ++actionGeneration
  const current = () => scope === profileScope.value && classId === auth.currentClassId && generation === actionGeneration
  const filename = `${currentClassName()}报告.docx`
  generating.value = 'class'
  try {
    const res = await api.classroom.report(classId, { education_stage: educationStage.value })
    if (!current()) return
    await downloadGenerated(res, filename)
    if (current()) ElMessage.success(res?.message || '班级报告已生成并开始下载')
  } catch (err) {
    if (current()) ElMessage.error(getErrorMessage(err, '班级报告生成失败'))
  } finally {
    if (current()) generating.value = ''
  }
}

async function generateStudentReport(type) {
  if (generating.value) return
  if (!auth.token || !educationStage.value || !selectedStudentId.value) return
  const scope = profileScope.value
  const classId = auth.currentClassId
  const id = selectedStudentId.value
  const generation = ++actionGeneration
  const current = () => scope === profileScope.value && classId === auth.currentClassId && id === selectedStudentId.value && generation === actionGeneration
  const student = classStudents.value.find((s) => s.id === id)
  generating.value = 'student'
  try {
    const res = await api.student.report(id, { type, education_stage: educationStage.value })
    if (!current()) return
    const suffix = type === 'parent_letter' ? '致家长信.docx' : '学习报告.docx'
    await downloadGenerated(res, `${student?.name || '学生'}${suffix}`)
    if (current()) ElMessage.success(res?.message || (type === 'parent_letter' ? '致家长信已生成并开始下载' : '学生报告已生成并开始下载'))
  } catch (err) {
    if (current()) ElMessage.error(getErrorMessage(err, '学生报告生成失败'))
  } finally {
    if (scope === profileScope.value && generation === actionGeneration) generating.value = ''
  }
}

async function generateBatchReport(refresh = false) {
  if (generating.value) return
  if (!auth.token || !educationStage.value || !auth.currentClassId) return
  const scope = profileScope.value
  const classId = auth.currentClassId
  const generation = ++actionGeneration
  const current = () => scope === profileScope.value && classId === auth.currentClassId && generation === actionGeneration
  generating.value = 'batch'
  try {
    const res = await api.classroom.reportBatch(classId, {
      education_stage: educationStage.value,
      type: 'parent_letter',
      lock: 'token',
      refresh,
    })
    if (!current()) return
    if (res?.mode === 'share') {
      const shares = res.shares || []
      await loadLetterShares()
      if (!current()) return
      if (shares.length) {
        const text = shares.map((share) => `${share.student_name || '学生'}：${shareUrl(share)}`).join('\n')
        await writeClipboard(text, res?.message || `已准备 ${shares.length} 个家长专属链接`)
      }
      if (current()) showLetterWarnings(res)
    } else {
      throw new Error(res?.error || res?.message || '后端未返回家长专属链接')
    }
  } catch (err) {
    if (current()) ElMessage.error(getErrorMessage(err, '批量报告生成失败'))
  } finally {
    if (current()) generating.value = ''
  }
}

async function regenerateBatchReport() {
  if (generating.value || !auth.currentClassId) return
  const scope = profileScope.value
  const classId = auth.currentClassId
  try {
    await ElMessageBox.confirm(
      '系统会按个人资料学段重新生成家长信，并使该学段现有有效链接失效；其他学段不变。确定继续吗？',
      '重新生成家长链接',
      { type: 'warning', confirmButtonText: '重新生成', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  if (scope === profileScope.value && classId === auth.currentClassId) await generateBatchReport(true)
}

async function reloadProfileScope() {
  reportLoadGeneration += 1
  studentsGeneration += 1
  sharesGeneration += 1
  actionGeneration += 1
  selectedStudentId.value = null
  classStudents.value = []
  letterShares.value = []
  loading.value = false
  sharesLoading.value = false
  generating.value = ''
  revokingShareId.value = null
  resetReport()
  if (auth.token && educationStage.value && auth.currentClassId) {
    await Promise.all([loadClassStudents(), loadLetterShares(), load()])
  }
}

watch([profileScope, () => auth.currentClassId], reloadProfileScope, { flush: 'sync' })
onMounted(reloadProfileScope)
</script>

<style scoped>
.reports-page {
  gap: 24px;
}

/* ── Ink masthead: class, period and the three headline numbers ── */
.report-hero {
  display: flex;
  flex-direction: column;
  gap: 30px;
  padding: 32px 36px 30px;
  animation: ma-fade-in var(--dur-4) var(--ease-out) backwards;
}

.report-plot {
  top: 70px;
  right: 0;
  width: 58%;
  height: calc(100% - 70px);
  opacity: 0.85;
  mask-image: linear-gradient(90deg, transparent, #000 45%);
}

.report-plot__bars rect {
  fill: var(--paper-raised);
  stroke: var(--rule);
  transform-origin: bottom;
  transform-box: fill-box;
  animation: report-bar-rise var(--dur-4) var(--ease-emphasized) backwards;
}

.report-plot__bars rect:nth-child(2) { animation-delay: 60ms; }
.report-plot__bars rect:nth-child(3) { animation-delay: 120ms; }
.report-plot__bars rect:nth-child(4) { animation-delay: 180ms; }
.report-plot__bars rect:nth-child(5) { animation-delay: 240ms; }
.report-plot__bars rect:nth-child(6) { animation-delay: 300ms; fill: var(--moss); stroke: none; }

@keyframes report-bar-rise {
  from { transform: scaleY(0); }
  to { transform: scaleY(1); }
}

.report-hero__top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
}

.report-hero__copy {
  min-width: 0;
  max-width: 680px;
}

.report-hero__period {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* Segmented control drawn for the ink surface. */
/* Period switch set as a quiet paper control on the kraft masthead. */
.ink-segmented {
  padding: 3px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--ink) 6%, transparent);
}

.ink-segmented :deep(.el-radio-button) {
  --el-radio-button-checked-bg-color: var(--paper-raised);
  --el-radio-button-checked-text-color: var(--ink);
}

.ink-segmented :deep(.el-radio-button__inner) {
  padding: 6px 16px;
  border-radius: 6px !important;
  color: var(--ink-muted);
  font-weight: 600;
}

.ink-segmented :deep(.el-radio-button__inner:hover) {
  color: var(--ink);
}

.ink-segmented :deep(.el-radio-button.is-active .el-radio-button__inner) {
  box-shadow: 0 0 0 1px var(--line), var(--shadow-xs) !important;
}

.report-hero__metrics .hero-metric__value {
  font-size: clamp(40px, 4vw, 60px);
}

/* ── Body: report reads first, generation lives in the rail ── */
.report-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 380px);
  gap: 24px;
  align-items: start;
}

.report-main {
  display: flex;
  flex-direction: column;
  gap: 24px;
  min-width: 0;
}

.report-body-card :deep(.el-card__body) {
  padding: 26px 28px 28px;
}

.report-body {
  display: flex;
  flex-direction: column;
  gap: 30px;
  min-height: 200px;
}

.summary-quote {
  position: relative;
  margin: 0;
  padding: 4px 0 4px 64px;
}

.summary-quote::before {
  content: '“';
  position: absolute;
  top: -18px;
  left: 0;
  color: var(--moss);
  font-family: Georgia, 'Times New Roman', serif;
  font-size: 96px;
  line-height: 1;
}

.summary-quote__label {
  display: block;
  margin-bottom: 8px;
  color: var(--moss);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  letter-spacing: 0.14em;
}

.summary-text {
  margin: 0;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 21px;
  font-weight: 400;
  line-height: 1.7;
  white-space: pre-wrap;
}

.report-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-width: 0;
}

.report-section__title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 20px;
  font-weight: 500;
}

.section-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--ink-faint);
}

.section-dot.is-good {
  background: var(--success);
}

.section-dot.is-bad {
  background: var(--danger);
}

/* Ranked lists replace the two plain tables. */
.rank-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}

.rank-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  padding: 18px 0 8px;
  border-top: 2px solid var(--ink);
}

.rank-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.rank-row {
  display: grid;
  grid-template-columns: 22px 34px minmax(0, 1fr) minmax(60px, 90px) 64px;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
  border-top: 1px solid var(--line);
  animation: ma-fade-in var(--dur-3) var(--ease-out) backwards;
}

.rank-row:first-child {
  border-top: 0;
}

.rank-row__no {
  color: var(--ink-faint);
  font-family: var(--font-serif);
  font-size: var(--fs-xl);
}

.rank-row__avatar {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background: var(--moss-soft);
  color: var(--moss-deep);
  font-family: var(--font-serif);
  font-weight: 600;
}

.rank-card.is-bad .rank-row__avatar {
  background: var(--danger-soft);
  color: var(--danger);
}

.rank-row__who {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.rank-row__who strong {
  overflow: hidden;
  color: var(--ink);
  font-size: var(--fs-md);
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rank-row__who small {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.rank-row__bar {
  height: 6px;
  overflow: hidden;
  border-radius: 3px;
  background: color-mix(in srgb, var(--success) 14%, var(--card));
}

.rank-card.is-bad .rank-row__bar {
  background: color-mix(in srgb, var(--danger) 12%, var(--card));
}

.rank-row__bar i {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--success);
  transform-origin: left center;
  animation: ma-meter-grow var(--dur-4) var(--ease-emphasized) backwards;
}

.rank-card.is-bad .rank-row__bar i {
  background: var(--danger);
}

.rank-row__value {
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 22px;
  font-weight: 400;
  text-align: right;
}

.rank-row__value small {
  margin-left: 1px;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

/* ── Right rail: report generation as a stacked, sticky toolbox ── */
.report-rail {
  position: sticky;
  top: 8px;
  display: flex;
  flex-direction: column;
  gap: 0;
  min-width: 0;
  padding: 22px 22px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--paper-raised);
}

.rail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding-bottom: 14px;
  border-bottom: 2px solid var(--ink);
}

.rail-kicker {
  display: block;
  color: var(--moss);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  letter-spacing: 0.16em;
}

.rail-head strong {
  display: block;
  margin-top: 6px;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 24px;
  font-weight: 500;
}

.class-select {
  width: 220px;
}

.report-action-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 0;
}

.report-action {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  padding: 18px 0;
  border-bottom: 1px solid var(--line);
}

.report-action:last-child {
  border-bottom: 0;
}

.report-action strong {
  display: block;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 18px;
  font-weight: 500;
  margin-bottom: 4px;
}

.report-action span {
  display: block;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.report-action :deep(.help-button) {
  margin-top: 6px;
}

.report-action > .el-button {
  align-self: flex-start;
}

.letter-share-panel {
  margin: 0;
  padding: 0;
  border: 0;
}

.letter-share-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.letter-share-head strong {
  display: block;
  margin-bottom: 3px;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 20px;
  font-weight: 500;
}

.letter-share-table :deep(.cell) {
  padding-right: 6px;
  padding-left: 6px;
}

.student-no,
.share-expiry {
  display: block;
}

.share-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  white-space: nowrap;
}

.share-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.expiry-hint {
  display: block;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.expiry-hint.is-soon {
  color: var(--danger);
}

.letter-share-head span,
.student-no {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.inline-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.inline-actions .el-select {
  width: 200px;
  flex: 1 1 160px;
}

.inline-actions .el-button + .el-button {
  margin-left: 0;
}

.raw-json {
  background: var(--surface-subtle);
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-size: 12px;
  max-height: 360px;
  overflow: auto;
}

@media (max-width: 1320px) {
  .report-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .report-rail {
    position: static;
  }

  .report-action-grid {
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  }
}

@media (max-width: 760px) {
  .rank-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .report-hero {
    padding: 24px;
  }

  .summary-quote {
    padding-left: 0;
  }

  .summary-quote::before {
    display: none;
  }

  .letter-share-head,
  .inline-actions {
    align-items: stretch;
    flex-direction: column;
  }
}

@media (max-width: 480px) {
  .report-hero,
  .report-rail {
    padding: 20px 16px;
  }

  .report-action-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .report-body-card :deep(.el-card__body) {
    padding: 20px 16px;
  }

  .rank-row {
    grid-template-columns: 20px 34px minmax(0, 1fr) 64px;
    gap: 8px;
  }

  .rank-row__bar {
    display: none;
  }

  .inline-actions .el-select {
    width: 100%;
    flex-basis: auto;
  }
}

@media (prefers-reduced-motion: reduce) {
  .report-plot__bars rect,
  .rank-row__bar i {
    animation: none;
  }
}
</style>
