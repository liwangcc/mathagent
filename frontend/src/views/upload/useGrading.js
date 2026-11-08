/**
 * Grading a recognized batch (phase two) and the grading stopwatch.
 *
 * - onConfirmGrade streams grading against the frozen template's answers; onConfirmIdentitiesAndGrade first
 *   applies the teacher's identity decisions.
 * - startGradingClock / anchorGradingClock / stopGradingClock keep the live stopwatch, re-anchored to the
 *   server's elapsed seconds.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { ElMessage } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import { validateGradeCompletion } from '@/utils/batchReadiness'
import { formatGradingDuration } from '@/utils/gradingTimer'
import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  reportSseServiceFault,
  SseTransportError,
} from '@/utils/sse'

export function useGrading(ctx) {
  const {
    anonymous, fileList, activeStep, grading, gradingClockStart, gradingClockNow, serviceState,
    markServiceFault, identityDialogVisible, identityResolutions, form, batchId, recognitionDone,
    recognitionCanGrade, recognitionError, currentMessage, progressList, gradeTotal, gradeError,
    summary, reviewRestoredLabel, coursewareRecommendations, coursewareRecommendationState,
    coursewareRecommendationQuery, coursewareRecommendationKey, reviewBusy, reviewArchived,
    reviewRevision, gradeController, gradablePaperCount, needsIdentityResolution,
    identityCanConfirm, prepareIdentityResolutions, identityResolutionPayload, requireBatchService,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const forgetOrientationBatch = (...args) => ctx.forgetOrientationBatch(...args)
  const rememberReviewBatch = (...args) => ctx.rememberReviewBatch(...args)
  const resetBatchReport = (...args) => ctx.resetBatchReport(...args)
  const startBatchReportPolling = (...args) => ctx.startBatchReportPolling(...args)
  const hydrateReviewState = (...args) => ctx.hydrateReviewState(...args)

  let gradingClockTimer = null

  async function onConfirmIdentitiesAndGrade() {
    if (grading.value) return false
    if (!identityCanConfirm.value) {
      ElMessage.warning('请为每张未确认试卷选择名单中的学生；确需例外新建时，请确认姓名和学号')
      return
    }
    identityDialogVisible.value = false
    await onConfirmGrade(true)
  }

  // ── 阶段二：按冻结模板的答案批改 ──
  async function onConfirmGrade(skipIdentityGate = false) {
    if (grading.value) return false
    if (!requireBatchService()) {
      gradeError.value = `批阅未开始：${serviceState.value.message || '批改链路当前不可用，请稍后再试。'}`
      return false
    }
    if (!recognitionDone.value || !recognitionCanGrade.value) {
      recognitionError.value ||= '本批次 OCR 尚未完整完成，不能开始部分批阅。请重新识别全部试卷。'
      gradeError.value = `批阅未开始：${recognitionError.value}`
      activeStep.value = 1
      ElMessage.warning(recognitionError.value)
      return false
    }
    if (!skipIdentityGate && needsIdentityResolution.value) {
      identityDialogVisible.value = true
      if (!identityResolutions.value.length) await prepareIdentityResolutions()
      return false
    }

    const payload = {
      batch_id: batchId.value,
      mode: form.mode,
      category: form.category || 'daily',
      exam_name: form.category === 'exam' ? form.exam_name : '',
      // The server grades against the batch's frozen template answers.
      identity_resolutions: identityResolutionPayload(),
    }

    const expectedGradeTotal = gradablePaperCount.value
    if (!expectedGradeTotal) {
      gradeError.value = '当前没有可批阅的试卷，请重新识别全部试卷。'
      ElMessage.warning(gradeError.value)
      return false
    }

    grading.value = true
    reviewBusy.value = false
    reviewArchived.value = false
    reviewRevision.value = 0
    progressList.value = []
    gradeTotal.value = expectedGradeTotal
    gradeError.value = ''
    summary.value = null
    reviewRestoredLabel.value = ''
    coursewareRecommendations.value = []
    coursewareRecommendationState.value = 'idle'
    coursewareRecommendationQuery.value = {}
    coursewareRecommendationKey.value = ''
    resetBatchReport()
    currentMessage.value = ''
    gradeController.value = new AbortController()
    startGradingClock()
    let completedSummary = null
    let completedRevision = 0

    try {
      const response = await api.grade.confirm(payload, gradeController.value.signal)
      if (!response.ok) {
        throw await createSseResponseError(response, '批改请求失败')
      }
      await consumeSse(response, (evt, data) => {
        if (evt === 'progress') {
          currentMessage.value = data.message || ''
          if (typeof data.total === 'number') gradeTotal.value = data.total
          anchorGradingClock(data.grading_elapsed_seconds)
        } else if (evt === 'result') {
          progressList.value.push(data)
          currentMessage.value = ''
        } else if (evt === 'done') {
          // `done` is sent only after the server saved this batch's review, so
          // keep the current batch restorable even if this client rejects the
          // stream below; an older saved review must not take its place.
          if (Number(data.total) > 0) rememberReviewBatch(batchId.value)
          const completion = validateGradeCompletion({
            expectedTotal: expectedGradeTotal,
            reportedTotal: data.total,
            receivedTotal: progressList.value.length,
          })
          if (!completion.complete) throw new SseTransportError(completion.message)
          completedSummary = {
            total: data.total,
            avg_score: data.avg_score ?? null,
            grading_elapsed_seconds: data.grading_elapsed_seconds ?? null,
          }
          completedRevision = Number(data.review_revision || 0)
          currentMessage.value = ''
        } else if (evt === 'error') {
          throw createSseEventError(data, '批改出错')
        }
      })
      if (completedSummary) {
        summary.value = completedSummary
        reviewRevision.value = completedRevision
        rememberReviewBatch(batchId.value)
        forgetOrientationBatch()
        // SSE rows are progress summaries; reload the authoritative persisted review
        // so page groups and per-question page references are available immediately.
        const reviewPayload = await api.grade.reviewGet(batchId.value)
        if (!hydrateReviewState(reviewPayload)) throw new Error('批阅结果已生成，但审阅工作台恢复失败')
        startBatchReportPolling()
        const elapsed = summary.value.grading_elapsed_seconds
        ElMessage.success(
          `批改完成：共 ${summary.value.total} ${anonymous.value ? '页' : '张'}`
          + (elapsed != null ? `，批阅用时 ${formatGradingDuration(elapsed)}` : ''),
        )
        // 完成后回到上传步骤，结果保留在进度卡片
        activeStep.value = 0
        fileList.value = []
        return true
      }
      throw new SseTransportError('批改连接提前结束')
    } catch (err) {
      summary.value = null
      const signal = gradeController.value?.signal
      if (!isSseRequestCancelled(err, signal)) {
        reportSseServiceFault(
          err,
          markServiceFault,
          '批改链路连接中断，已完成结果会保留；请稍后重试。',
          { signal },
        )
        gradeError.value = err.message || '批改失败'
        notifyProcessingError(err, gradeError.value)
      }
    } finally {
      stopGradingClock()
      grading.value = false
      gradeController.value = null
    }
    return false
  }

  function startGradingClock() {
    stopGradingClock()
    gradingClockStart.value = Date.now()
    gradingClockNow.value = gradingClockStart.value
    gradingClockTimer = window.setInterval(() => { gradingClockNow.value = Date.now() }, 1000)
  }

  function anchorGradingClock(serverElapsedSeconds) {
    const seconds = Number(serverElapsedSeconds)
    if (!gradingClockStart.value || serverElapsedSeconds == null || !Number.isFinite(seconds) || seconds < 0) return
    gradingClockNow.value = Date.now()
    gradingClockStart.value = gradingClockNow.value - seconds * 1000
  }

  function stopGradingClock() {
    if (gradingClockTimer) window.clearInterval(gradingClockTimer)
    gradingClockTimer = null
    gradingClockStart.value = 0
  }

  return {
    gradingClockTimer, onConfirmIdentitiesAndGrade, onConfirmGrade, startGradingClock,
    anchorGradingClock, stopGradingClock,
  }
}
