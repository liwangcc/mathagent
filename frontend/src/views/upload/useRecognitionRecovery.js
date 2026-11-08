/**
 * Keeping student recognition going when the connection drops or a page fails transiently.
 *
 * - updateRecognitionRecoveryState decides what the teacher sees after an interruption;
 *   recoverRecognitionConnection / pollRecognitionRecovery reconnect to the running batch.
 * - scheduleRecognitionAutoResume and scheduleRetryableOcrAutoResume spend the batch's automatic resume
 *   after a short cool-down instead of leaving the teacher at an incomplete batch; clearRecognitionAutoResume
 *   cancels it.
 * - applyRecognitionDone, resetRecognitionCompletionState and resetRecognitionRecoveryState.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { normalizeBatchReadiness } from '@/utils/batchReadiness'
import { recognitionIncompleteMessage } from '@/utils/recognitionStatus'

export function useRecognitionRecovery(ctx) {
  const {
    fileList, activeStep, recognizing, grading, serviceState, ACCELERATOR_AUTO_RESUME_DELAY_MS,
    ACCELERATOR_AUTO_RESUME_LIMIT, OCR_RETRY_AUTO_RESUME_DELAY_MS, serviceFaultHoldUntil, batchId,
    papers, recognized, recognitionDone, recognitionCanGrade, recognitionError,
    recognitionRecoveryState, recognitionStats, currentMessage,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const refreshRecognitionStateAfterFailure = (...args) => ctx.refreshRecognitionStateAfterFailure(...args)
  const continueRefreshedRecognition = (...args) => ctx.continueRefreshedRecognition(...args)
  const resumeRecognitionBatch = (...args) => ctx.resumeRecognitionBatch(...args)

  let recognitionAutoResumeTimer = null
  let recognitionAutoResumeAttempts = 0
  let recognitionAutoResumeGeneration = 0
  let recognitionRetryAutoResumedBatchId = ''

  function resetRecognitionCompletionState() {
    recognitionDone.value = false
    recognitionCanGrade.value = false
    recognitionError.value = ''
    recognitionStats.value = { uploaded: 0, succeeded: 0, manualReview: 0, failed: 0, excluded: 0, unresolved: 0 }
  }

  function resetRecognitionRecoveryState() {
    recognitionRecoveryState.value = { status: 'none', message: '' }
  }

  function recoverRecognitionConnection(targetBatchId, payload) {
    if (!payload || String(payload.batch_id || '') !== String(targetBatchId || '')
      || String(batchId.value || '') !== String(targetBatchId || '')
      || !['recognizing_remaining', 'recognition_interrupted'].includes(payload.status)
      || recognitionRecoveryState.value.status !== 'available') return false
    // This owned snapshot proves reachability, not spare model/hardware capacity.
    serviceFaultHoldUntil.value = 0
    serviceState.value = { status: 'queued', label: '批次可恢复', message: '已连接服务器，正在恢复已保存的识别进度。' }
    recognitionError.value = ''
    currentMessage.value = ''
    activeStep.value = 1
    scheduleRecognitionAutoResume(targetBatchId)
    return true
  }

  async function pollRecognitionRecovery(target, generation) {
    const isCurrent = () => generation === recognitionAutoResumeGeneration
      && target === String(batchId.value || '') && !recognizing.value && !grading.value
    if (!isCurrent()) return
    const payload = await refreshRecognitionStateAfterFailure(target, { isCurrent, suppressAutoResume: true })
    if (!isCurrent() || !payload) return
    if (await continueRefreshedRecognition(payload)) return
    if (recognitionRecoveryState.value.status !== 'available') return
    // Active batch states still own server work. Only read checkpoints until it
    // ends; a race acquiring the lock is handled by BATCH_RECOGNITION_BUSY below.
    if (payload.status === 'recognizing_remaining') {
      recoverRecognitionConnection(target, payload)
      return
    }
    if (payload.status === 'recognition_interrupted') {
      await resumeRecognitionBatch(target, { automatic: true })
    }
  }

  function clearRecognitionAutoResume({ resetAttempts = false } = {}) {
    recognitionAutoResumeGeneration += 1
    if (recognitionAutoResumeTimer) {
      window.clearTimeout(recognitionAutoResumeTimer)
      recognitionAutoResumeTimer = null
    }
    if (resetAttempts) recognitionAutoResumeAttempts = 0
  }

  function scheduleRecognitionAutoResume(targetBatchId) {
    const target = String(targetBatchId || '')
    if (!target || target !== String(batchId.value || '')) return false
    clearRecognitionAutoResume()
    if (recognitionAutoResumeAttempts >= ACCELERATOR_AUTO_RESUME_LIMIT) {
      recognitionRecoveryState.value = {
        status: 'available',
        message: '自动恢复检查已达上限。已完成结果仍保留，可稍后点击“继续处理未完成试卷”。',
      }
      return false
    }
    recognitionAutoResumeAttempts += 1
    recognitionRecoveryState.value = {
      status: 'waiting',
      message: `识别进度已保存，系统将在 ${ACCELERATOR_AUTO_RESUME_DELAY_MS / 1000} 秒后检查并继续；服务器仍在处理时只等待，不重复提交（${recognitionAutoResumeAttempts}/${ACCELERATOR_AUTO_RESUME_LIMIT}）。`,
    }
    const generation = recognitionAutoResumeGeneration
    recognitionAutoResumeTimer = window.setTimeout(() => {
      recognitionAutoResumeTimer = null
      void pollRecognitionRecovery(target, generation)
    }, ACCELERATOR_AUTO_RESUME_DELAY_MS)
    return true
  }

  function scheduleRetryableOcrAutoResume(targetBatchId, payload = {}) {
    const target = String(targetBatchId || '')
    if (!target || target !== String(batchId.value || '')) return false
    if (Number(payload?.retryable_failed_papers || 0) <= 0) return false
    // The server allows each page one more attempt; spend it once per batch.
    if (recognitionRetryAutoResumedBatchId === target) return false
    recognitionRetryAutoResumedBatchId = target
    clearRecognitionAutoResume()
    recognitionRecoveryState.value = {
      status: 'waiting',
      message: `部分试卷识别暂时失败，系统将在 ${OCR_RETRY_AUTO_RESUME_DELAY_MS / 1000} 秒后自动重试这些试卷；也可立即点击“继续处理未完成试卷”。`,
    }
    recognitionAutoResumeTimer = window.setTimeout(() => {
      recognitionAutoResumeTimer = null
      if (target === String(batchId.value || '') && !recognizing.value && !grading.value) {
        void resumeRecognitionBatch(target, { automatic: true })
      }
    }, OCR_RETRY_AUTO_RESUME_DELAY_MS)
    return true
  }

  function updateRecognitionRecoveryState(payload = {}, { failureCode = '', serviceFault = false } = {}) {
    // A fully recovered snapshot wins over historical status/error codes. Keep
    // the same complete-paper gate as grading, not just the server's boolean.
    if (payload?.can_grade === true && normalizeBatchReadiness(payload, {
      expectedTotal: fileList.value.length,
      papers: papers.value,
    }).canGrade) {
      resetRecognitionRecoveryState()
      return
    }
    const code = String(failureCode || '').toUpperCase()
    if (code === 'OCR_RETRY_EXHAUSTED') {
      recognitionRecoveryState.value = {
        status: 'exhausted',
        message: '识别重试已耗尽，不能再自动恢复。请检查原图；若图片正常，请联系管理员检查模型服务。',
      }
      return
    }
    if (code === 'OCR_NO_RETRYABLE_FAILURES') {
      recognitionRecoveryState.value = {
        status: 'unrecoverable',
        message: '当前失败试卷不可自动恢复，请检查原图或联系管理员。',
      }
      return
    }
    if (serviceFault && !payload?.status) {
      recognitionRecoveryState.value = {
        status: 'service_fault',
        message: '批改链路当前故障，服务恢复后可继续处理未完成试卷。',
      }
      return
    }

    const status = String(payload?.status || '')
    const retryableFailures = Number(payload?.retryable_failed_papers || 0)
    const unresolved = Number(payload?.unresolved_papers || 0)
    const failed = Number(payload?.failed_papers || 0)
    const resumable = (
      ['recognizing_remaining', 'recognition_interrupted'].includes(status)
      || retryableFailures > 0
      || (status === 'failed' && unresolved > 0)
    )
    if (resumable) {
      recognitionRecoveryState.value = serviceFault
        ? {
            status: 'service_fault',
            message: '批改链路当前故障，服务恢复后可继续处理未完成试卷。',
          }
        : {
            status: 'available',
            message: '本批次仍有未完成或可重试的试卷，可点击“继续处理未完成试卷”。',
          }
      return
    }
    if (failed > 0 || ['failed', 'ocr_partial_failed'].includes(status)) {
      recognitionRecoveryState.value = {
        status: 'unrecoverable',
        message: '当前失败试卷不可自动恢复，自动重试可能已耗尽。请检查原图或联系管理员。',
      }
      return
    }
    resetRecognitionRecoveryState()
  }

  function applyRecognitionDone(data) {
    const stats = normalizeBatchReadiness(data, {
      expectedTotal: fileList.value.length,
      papers: papers.value,
    })
    recognitionStats.value = stats
    recognitionDone.value = true
    recognitionCanGrade.value = stats.canGrade
    recognitionError.value = stats.canGrade ? '' : recognitionIncompleteMessage(stats)
    // An incomplete `done` must still say what can be done next (resume a
    // retryable failure, or that a page is unrecoverable), not only that the
    // batch is incomplete.
    if (stats.canGrade) resetRecognitionRecoveryState()
    else updateRecognitionRecoveryState(data)
    recognized.current = stats.succeeded + stats.manualReview + stats.failed + stats.excluded
    recognized.total = stats.uploaded || recognized.total
  }

  return {
    recognitionAutoResumeTimer, recognitionAutoResumeAttempts, recognitionAutoResumeGeneration,
    recognitionRetryAutoResumedBatchId, resetRecognitionCompletionState,
    resetRecognitionRecoveryState, recoverRecognitionConnection, pollRecognitionRecovery,
    clearRecognitionAutoResume, scheduleRecognitionAutoResume, scheduleRetryableOcrAutoResume,
    updateRecognitionRecoveryState, applyRecognitionDone,
  }
}
