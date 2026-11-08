/**
 * Picking an interrupted student batch back up after a reload or a failed request.
 *
 * - The orientation batch pointer kept in localStorage (orientationStorageKey, rememberOrientationBatch,
 *   forgetOrientationBatch) and resuming its orientation review (resumeOrientationBatch).
 * - Resuming recognition of a durable batch (resumeRecognitionBatch), and refreshing its state after a failed
 *   request before continuing (refreshRecognitionStateAfterFailure, continueRefreshedRecognition).
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  isSseServiceFault,
  reportSseServiceFault,
} from '@/utils/sse'
import { canRecoverRecognitionConnection } from '@/utils/recognitionStatus'

export function useBatchResume(ctx) {
  const {
    anonymous, auth, ORIENTATION_BATCH_STORAGE_PREFIX, activeStep, recognizing, markServiceFault,
    batchId, recognized, recognitionDone, recognitionCanGrade, recognitionError,
    recognitionRecoveryState, currentMessage, questions, recognizeController,
    resetRecognitionCompletionState, resetRecognitionRecoveryState, recoverRecognitionConnection,
    clearRecognitionAutoResume, scheduleRetryableOcrAutoResume, updateRecognitionRecoveryState,
    applyRecognitionDone, requireBatchService, applyOrientationState, rememberRecognitionBatch,
    upsertRecognitionPaper, onConfirmGrade,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const reviewStorageKey = (...args) => ctx.reviewStorageKey(...args)

  function orientationStorageKey() {
    const teacherId = auth.user?.id
    const mode = anonymous.value ? ':anonymous' : ''
    return teacherId ? `${ORIENTATION_BATCH_STORAGE_PREFIX}${mode}:${teacherId}` : ''
  }

  function rememberOrientationBatch(id) {
    const key = orientationStorageKey()
    if (!key || !id) return
    localStorage.setItem(key, String(id))
    // Grading success clears this pointer, so it only ever names work newer than
    // the saved review. A review pointer for another batch would otherwise win
    // on the next mount and hide this unfinished batch.
    const reviewKey = reviewStorageKey()
    const savedReview = reviewKey ? localStorage.getItem(reviewKey) : ''
    if (savedReview && savedReview !== String(id)) localStorage.removeItem(reviewKey)
  }

  function forgetOrientationBatch() {
    const key = orientationStorageKey()
    if (key) localStorage.removeItem(key)
  }

  async function resumeOrientationBatch() {
    const key = orientationStorageKey()
    const savedBatchId = key ? localStorage.getItem(key) : ''
    if (!savedBatchId) return false
    try {
      const payload = await api.grade.orientationState(savedBatchId)
      if (String(payload?.batch_id || '') !== String(savedBatchId) || !Array.isArray(payload.papers) || !payload.papers.length) {
        throw new Error('方向复核批次没有可恢复内容')
      }
      applyOrientationState(payload)
      if (recognitionCanGrade.value) {
        await onConfirmGrade()
      } else if (['recognizing_remaining', 'recognition_interrupted'].includes(payload.status)) {
        recoverRecognitionConnection(savedBatchId, payload)
      }
      return true
    } catch (err) {
      const status = Number(err?.response?.status || err?.status || 0)
      if ([401, 403, 404].includes(status)) forgetOrientationBatch()
      return false
    }
  }

  async function refreshRecognitionStateAfterFailure(targetBatchId, recoveryOptions) {
    try {
      const payload = await api.grade.orientationState(targetBatchId)
      if (recoveryOptions?.isCurrent && !recoveryOptions.isCurrent()) return false
      if (String(payload?.batch_id || '') !== String(targetBatchId) || !Array.isArray(payload.papers) || !payload.papers.length) {
        throw new Error('识别批次状态响应不完整')
      }
      applyOrientationState(payload, recoveryOptions)
      return payload
    } catch (err) {
      if (recoveryOptions?.isCurrent && !recoveryOptions.isCurrent()) return false
      const status = Number(err?.response?.status || err?.status || 0)
      if ([401, 403, 404].includes(status)) forgetOrientationBatch()
      const serviceFault = Boolean(recoveryOptions?.serviceFault) || !status || status >= 500 || [408, 429].includes(status)
      if (serviceFault && !recoveryOptions?.serviceFault) {
        markServiceFault('无法刷新识别批次状态，请检查网络或联系管理员。')
      }
      updateRecognitionRecoveryState({}, { ...recoveryOptions, serviceFault })
      return false
    }
  }

  async function continueRefreshedRecognition(refreshed) {
    if (!refreshed || !recognitionDone.value || !recognitionCanGrade.value) return false
    if (!questions.value.length) {
      recognitionCanGrade.value = false
      recognitionError.value = '恢复识别后未得到可批阅题目'
      return false
    }
    clearRecognitionAutoResume({ resetAttempts: true })
    currentMessage.value = ''
    // The snapshot is authoritative; transport failure must not erase it or
    // raise a second recognition error. Keep the existing identity/grade gates.
    await onConfirmGrade()
    return true
  }

  async function resumeRecognitionBatch(resumeBatchId, { automatic = false } = {}) {
    // This endpoint resumes an already-created durable batch. It may consume an
    // artifact that the busy accelerator is about to finish, so the new-work
    // free-slot gate must not suppress the recovery request.
    if (!requireBatchService()) return false
    const targetBatchId = String(resumeBatchId || batchId.value || '')
    if (!targetBatchId || recognizing.value) return false
    clearRecognitionAutoResume({ resetAttempts: !automatic })

    recognizing.value = true
    activeStep.value = 1
    resetRecognitionCompletionState()
    resetRecognitionRecoveryState()
    recognitionError.value = ''
    currentMessage.value = '正在从已保存进度恢复识别'
    recognizeController.value = new AbortController()
    let doneData = null
    try {
      const response = await api.grade.recognitionResume(targetBatchId, recognizeController.value.signal)
      if (!response.ok) {
        throw await createSseResponseError(response, '恢复识别请求失败')
      }
      await consumeSse(response, (evt, data) => {
        rememberRecognitionBatch(data)
        if (evt === 'progress') {
          currentMessage.value = data.message || ''
          if (typeof data.current === 'number') recognized.current = data.current
          if (typeof data.total === 'number') recognized.total = data.total
        } else if (evt === 'paper') {
          upsertRecognitionPaper(data)
        } else if (evt === 'done') {
          doneData = data
          questions.value = data.questions || []
          clearRecognitionAutoResume({ resetAttempts: true })
          applyRecognitionDone(data)
          currentMessage.value = ''
        } else if (evt === 'error') {
          throw createSseEventError(data, '恢复识别出错')
        }
      })

      if (!recognitionCanGrade.value) {
        activeStep.value = 1
        scheduleRetryableOcrAutoResume(targetBatchId, doneData)
        return true
      }
      if (!questions.value.length) throw new Error('恢复识别后未得到可批阅题目')
      await onConfirmGrade()
      return true
    } catch (err) {
      const signal = recognizeController.value?.signal
      if (!isSseRequestCancelled(err, signal)) {
        const failureCode = String(err?.code || '').toUpperCase()
        const connectionRecovery = canRecoverRecognitionConnection(err, signal)
        const serviceFault = isSseServiceFault(err, { signal })
        const refreshed = await refreshRecognitionStateAfterFailure(targetBatchId, {
          failureCode, serviceFault: serviceFault && !connectionRecovery,
          isCurrent: () => !signal?.aborted && targetBatchId === String(batchId.value || ''),
        })
        if (signal?.aborted) return false
        if (connectionRecovery && await continueRefreshedRecognition(refreshed)) return true
        if (connectionRecovery && recoverRecognitionConnection(targetBatchId, refreshed)) return false
        reportSseServiceFault(
          err, markServiceFault,
          '学生试卷识别恢复链路中断，已完成结果仍会保留；请稍后重试。',
          { signal },
        )
        if (!refreshed) recognitionDone.value = false
        recognitionCanGrade.value = false
        recognitionError.value = err.message || '恢复识别失败'
        currentMessage.value = ''
        activeStep.value = 1
        notifyProcessingError(err, recognitionRecoveryState.value.message || recognitionError.value)
      }
      return false
    } finally {
      recognizing.value = false
      recognizeController.value = null
    }
  }

  return {
    orientationStorageKey, rememberOrientationBatch, forgetOrientationBatch, resumeOrientationBatch,
    refreshRecognitionStateAfterFailure, continueRefreshedRecognition, resumeRecognitionBatch,
  }
}
