/**
 * The 批改 page's batch report (本批洞察) and the courseware recommended for it.
 *
 * - The report's figures and charts, derived from the graded rows and the server's report: accuracy,
 *   outcome segments, top errors, knowledge points, the batch's average correct rate.
 * - Polling the server until the report is ready (startBatchReportPolling, pollBatchReport) and applying it.
 * - Courseware recommended for the batch's errors (loadCoursewareRecommendations, openCourseware).
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { computed } from 'vue'
import api from '@/api'
import { buildClientBatchReportFallback, reportPercent, nonNegativeReportCount } from '@/utils/batchReportView'

export function useBatchReport(ctx) {
  const {
    auth, router, form, batchId, progressList, summary, coursewareRecommendations,
    coursewareRecommendationState, coursewareRecommendationQuery, coursewareRecommendationKey,
    batchReport,
  } = ctx

  let batchReportPollTimer = null
  let batchReportPollGeneration = 0

  const dailyAverageCorrectRate = computed(() => {
    const rates = progressList.value
      .filter((row) => row.disposition === 'active' && Number(row.needs_review || 0) === 0)
      .map((row) => Number(row.correct_rate))
      .filter((value) => Number.isFinite(value))
    return rates.length ? rates.reduce((sum, value) => sum + value, 0) / rates.length : null
  })
  const batchReportStats = computed(() => {
    const stats = batchReport.value?.stats
    const normalized = stats && typeof stats === 'object' ? stats : {}
    if (Number(normalized.question_count) > 0) return normalized
    const fallback = buildClientBatchReportFallback(progressList.value)
    return fallback.question_count > 0 ? { ...normalized, ...fallback } : normalized
  })
  const batchReportTotalQuestions = computed(() => nonNegativeReportCount(batchReportStats.value.question_count))
  const batchReportCorrectCount = computed(() => nonNegativeReportCount(batchReportStats.value.correct_count))
  const batchReportPartialCount = computed(() => nonNegativeReportCount(batchReportStats.value.partial_count))
  const batchReportWrongCount = computed(() => nonNegativeReportCount(batchReportStats.value.wrong_count))
  const batchReportNeedsReviewCount = computed(() => nonNegativeReportCount(batchReportStats.value.needs_review_count))
  const batchReportJudgedCount = computed(() => (
    batchReportCorrectCount.value + batchReportPartialCount.value + batchReportWrongCount.value
  ))
  const batchReportErrorTotal = computed(() => batchReportPartialCount.value + batchReportWrongCount.value)
  const batchReportAccuracy = computed(() => {
    const judged = batchReportJudgedCount.value
    return judged > 0 ? batchReportCorrectCount.value / judged : null
  })
  const batchReportPrimaryValue = computed(() => {
    const averageScore = Number(summary.value?.avg_score)
    if (form.category === 'exam' && summary.value?.avg_score != null && Number.isFinite(averageScore)) {
      return averageScore.toFixed(averageScore % 1 ? 1 : 0)
    }
    return batchReportAccuracy.value == null ? '—' : `${Math.round(batchReportAccuracy.value * 100)}%`
  })
  const batchReportPrimaryLabel = computed(() => (
    form.category === 'exam' && summary.value?.avg_score != null ? '平均得分' : '已判正确率'
  ))
  const batchReportErrorRatePercent = computed(() => {
    const rate = batchReportStats.value.error_rate
    return rate == null || !Number.isFinite(Number(rate)) ? null : Number(rate) * 100
  })
  const batchReportDiagnosticErrorRateText = computed(() => reportPercent(batchReportStats.value.diagnostic_error_rate))
  // Judged outcomes only: pending answers are listed beside the donut, never drawn in it.
  const batchReportOutcomeSegments = computed(() => [
    { key: 'correct', label: '正确', value: batchReportCorrectCount.value },
    { key: 'partial', label: '部分正确', value: batchReportPartialCount.value },
    { key: 'wrong', label: '错误', value: batchReportWrongCount.value },
  ])
  const batchReportTopErrors = computed(() => (
    Array.isArray(batchReportStats.value.top_errors) ? batchReportStats.value.top_errors : []
  ))
  const batchReportKnowledgePoints = computed(() => (
    Array.isArray(batchReportStats.value.knowledge_points) ? batchReportStats.value.knowledge_points : []
  ))
  // Both lists are ranked by error count, so their bars are scaled to the
  // largest count rather than to the error rate shown alongside.
  const batchReportTopErrorMax = computed(() => Math.max(0, ...batchReportTopErrors.value.map((item) => Number(item.error_count) || 0)))
  const batchReportKnowledgeErrorMax = computed(() => Math.max(0, ...batchReportKnowledgePoints.value.map((item) => Number(item.error_count) || 0)))

  function coursewareStageLabel(stage) {
    return ({ primary: '小学', junior: '初中', senior: '高中' })[stage] || '当前学段'
  }

  async function loadCoursewareRecommendations() {
    // 推荐合同（内部设计文档 2026-09-13 §9）：精确知识点 > 同章知识点，最多 3 个，
    // 只含当前教师学段，须显示命中的学段、章节、知识点和原因；无匹配显示“暂无匹配”。
    const stats = batchReportStats.value
    // The unclassified bucket is not a knowledge point; never let it win the match.
    const points = (Array.isArray(stats.knowledge_points) ? stats.knowledge_points : [])
      .filter((item) => item && (item.topic || item.name) && (item.topic || item.name) !== '未分类')
      .sort((a, b) => Number(b.error_count || b.count || 0) - Number(a.error_count || a.count || 0))
    const top = points[0] || {}
    const query = {
      chapter: top.chapter === '未分类' ? '' : String(top.chapter || '').trim(),
      knowledge_point: String(top.topic || top.name || '').trim(),
    }
    const key = `${batchId.value || ''}|${query.chapter}|${query.knowledge_point}`
    coursewareRecommendationKey.value = key
    coursewareRecommendationQuery.value = query
    if (!query.chapter && !query.knowledge_point) {
      coursewareRecommendations.value = []
      coursewareRecommendationState.value = 'empty'
      return
    }
    coursewareRecommendationState.value = 'loading'
    try {
      const data = await api.courseware.recommendations({ ...query, limit: 3 })
      if (coursewareRecommendationKey.value !== key) return
      const items = Array.isArray(data?.items) ? data.items : []
      if (data?.matched === false || !items.length) {
        coursewareRecommendations.value = []
        coursewareRecommendationState.value = 'empty'
        return
      }
      coursewareRecommendations.value = items.slice(0, 3).map((item) => ({
        id: item.id,
        title: item.title || `${item.chapter || ''}：${item.knowledgePoint || item.knowledge_point || ''}`,
        chapter: item.chapter || query.chapter,
        knowledgePoint: item.knowledgePoint || item.knowledge_point || query.knowledge_point,
        stageLabel: coursewareStageLabel(item.stage || auth.user?.education_stage),
        reason: item.recommendation_reason || item.recommendationReason || '',
      }))
      coursewareRecommendationState.value = 'ready'
    } catch {
      if (coursewareRecommendationKey.value !== key) return
      coursewareRecommendations.value = []
      coursewareRecommendationState.value = 'error'
    }
  }

  function openCourseware(card) {
    router.push({
      path: '/courseware',
      query: {
        ...(card.chapter ? { chapter: card.chapter } : {}),
        ...(card.knowledgePoint ? { knowledge_point: card.knowledgePoint } : {}),
      },
    })
  }

  function resetBatchReport() {
    batchReportPollGeneration += 1
    if (batchReportPollTimer) {
      window.clearTimeout(batchReportPollTimer)
      batchReportPollTimer = null
    }
    batchReport.value = {
      status: 'not_started',
      text: '',
      stats: {},
      error: '',
      updated_at: '',
    }
  }

  function applyBatchReport(payload) {
    const report = payload?.report || payload || {}
    batchReport.value = {
      status: String(report.status || 'not_started'),
      text: String(report.text || ''),
      stats: report.stats && typeof report.stats === 'object' ? report.stats : {},
      error: String(report.error || ''),
      updated_at: String(report.updated_at || ''),
    }
    if (batchReport.value.status === 'ready') void loadCoursewareRecommendations()
  }

  function stopBatchReportPolling() {
    if (batchReportPollTimer) {
      window.clearTimeout(batchReportPollTimer)
      batchReportPollTimer = null
    }
  }

  async function pollBatchReport() {
    const currentBatchId = String(batchId.value || '')
    const currentGeneration = batchReportPollGeneration
    if (!currentBatchId) return
    try {
      const payload = await api.grade.reportGet(currentBatchId)
      if (
        currentGeneration !== batchReportPollGeneration
        || currentBatchId !== String(batchId.value || '')
      ) return
      applyBatchReport(payload)
      if (['ready', 'failed'].includes(batchReport.value.status)) {
        stopBatchReportPolling()
        return
      }
    } catch {
      if (
        currentGeneration !== batchReportPollGeneration
        || currentBatchId !== String(batchId.value || '')
      ) return
      // Keep the result table usable; the next poll can recover from a brief outage.
    }
    if (
      currentGeneration !== batchReportPollGeneration
      || currentBatchId !== String(batchId.value || '')
    ) return
    stopBatchReportPolling()
    batchReportPollTimer = window.setTimeout(pollBatchReport, 2500)
  }

  function startBatchReportPolling() {
    batchReportPollGeneration += 1
    stopBatchReportPolling()
    void pollBatchReport()
  }

  return {
    batchReportPollTimer, batchReportPollGeneration, dailyAverageCorrectRate, batchReportStats,
    batchReportTotalQuestions, batchReportCorrectCount, batchReportPartialCount,
    batchReportWrongCount, batchReportNeedsReviewCount, batchReportJudgedCount,
    batchReportErrorTotal, batchReportAccuracy, batchReportPrimaryValue, batchReportPrimaryLabel,
    batchReportErrorRatePercent, batchReportDiagnosticErrorRateText, batchReportOutcomeSegments,
    batchReportTopErrors, batchReportKnowledgePoints, batchReportTopErrorMax,
    batchReportKnowledgeErrorMax, coursewareStageLabel, loadCoursewareRecommendations,
    openCourseware, resetBatchReport, applyBatchReport, stopBatchReportPolling, pollBatchReport,
    startBatchReportPolling,
  }
}
