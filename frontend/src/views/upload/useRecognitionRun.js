/**
 * Recognizing a student batch: starting it, the orientation review in between, and starting over.
 *
 * - onStartRecognize / onBatchConfirm fill in the batch details the server needs, maybeAutoStartRecognition
 *   starts recognition by itself once everything is ready, and runRecognize streams it (phase one: student
 *   papers against the confirmed template).
 * - The orientation review of a paper the server could not turn upright: opening it, rotating the preview,
 *   confirming and retrying (applyOrientationState, confirmOrientationAndRetry).
 * - Excluding a paper from the batch, continuing after recognition, and reset / startNewBatch.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { ElMessage } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import { isExcludedPaper, mergeRecognitionPaper } from '@/utils/batchReadiness'
import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  isSseServiceFault,
  reportSseServiceFault,
  SseTransportError,
} from '@/utils/sse'
import { createRotatedOcrReadyFile } from '@/utils/uploadValidation'
import { appendStudentUploadFiles } from '@/utils/uploadSource'
import {
  paperOcrFailed,
  canRecoverRecognitionConnection,
  orientationErrorMessage,
  reviewErrorMessage,
} from '@/utils/recognitionStatus'

export function useRecognitionRun(ctx) {
  const {
    anonymous, auth, inheritIdentity, fileList, imagePreprocessing, imageExtracting,
    batchDialogVisible, activeStep, recognizing, grading, recognitionBlocksNewWork,
    markServiceFault, templateMode, templateColumnMode, confirmedTemplateId,
    templateTeachingContext, templateDialogVisible, pendingTemplateId, identityResolutions,
    identityOptionsLoading, identityOptionsError, form, batchId, papers, recognized,
    recognitionDone, recognitionAutoArmed, recognitionCanGrade, recognitionError,
    recognitionRecoveryState, excludingPaperId, currentMessage, questions, orientationDialogVisible,
    orientationPaper, orientationImageUrl, orientationImageLoading, orientationImageError,
    orientationQuarterTurns, orientationSaving, orientationActionId, progressList, gradeTotal,
    gradeError, recognizeController, resetRecognitionCompletionState, resetRecognitionRecoveryState,
    recoverRecognitionConnection, clearRecognitionAutoResume, scheduleRetryableOcrAutoResume,
    updateRecognitionRecoveryState, applyRecognitionDone, batchConfirmValid,
    requireRecognitionService, normalizeTemplateMode, normalizeTemplateColumnMode, resetFiles,
    releasePhoneCaptureSession, loadDailyUsage,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const onConfirmGrade = (...args) => ctx.onConfirmGrade(...args)
  const rememberOrientationBatch = (...args) => ctx.rememberOrientationBatch(...args)
  const forgetOrientationBatch = (...args) => ctx.forgetOrientationBatch(...args)
  const refreshRecognitionStateAfterFailure = (...args) => ctx.refreshRecognitionStateAfterFailure(...args)
  const continueRefreshedRecognition = (...args) => ctx.continueRefreshedRecognition(...args)
  const forgetReviewBatch = (...args) => ctx.forgetReviewBatch(...args)
  const clearReviewDisplay = (...args) => ctx.clearReviewDisplay(...args)

  let orientationImageRequestId = 0

  function resetOrientationReview() {
    orientationImageRequestId += 1
    if (orientationImageUrl.value) URL.revokeObjectURL(orientationImageUrl.value)
    orientationImageUrl.value = ''
    orientationImageLoading.value = false
    orientationImageError.value = ''
    orientationPaper.value = null
    orientationQuarterTurns.value = 0
    orientationActionId.value = ''
  }

  async function openOrientationReview(paper) {
    resetOrientationReview()
    orientationPaper.value = paper
    orientationQuarterTurns.value = Number(paper?.manual_quarter_turns || 0) % 4
    orientationDialogVisible.value = true
    orientationImageLoading.value = true
    const requestId = orientationImageRequestId
    try {
      const imageUrl = await api.grade.paperImage(batchId.value, paper.paper_id, 'original')
      if (requestId !== orientationImageRequestId) {
        URL.revokeObjectURL(imageUrl)
        return
      }
      orientationImageUrl.value = imageUrl
    } catch (err) {
      if (requestId === orientationImageRequestId) {
        orientationImageError.value = orientationErrorMessage(err, '原图加载失败，请刷新后重试')
      }
    } finally {
      if (requestId === orientationImageRequestId) orientationImageLoading.value = false
    }
  }

  function rotateOrientationPreview(delta) {
    orientationQuarterTurns.value = (orientationQuarterTurns.value + delta + 4) % 4
    orientationActionId.value = ''
  }

  function applyOrientationState(payload, recoveryOptions = {}) {
    if (!payload?.batch_id) return
    batchId.value = String(payload.batch_id)
    papers.value = Array.isArray(payload.papers) ? payload.papers : []
    form.category = payload.category || form.category || 'daily'
    form.exam_name = payload.exam_name || ''
    form.mode = payload.mode || form.mode || 'precise'
    if (payload.source_mode === 'template' && payload.template_id) {
      confirmedTemplateId.value = String(payload.template_id)
      templateTeachingContext.value = payload.teaching_context || {}
      templateMode.value = normalizeTemplateMode(payload.template_mode, 'marked')
      templateColumnMode.value = normalizeTemplateColumnMode(payload.column_mode, templateColumnMode.value)
    }
    questions.value = payload.questions || []
    applyRecognitionDone(payload)
    if (
      ['recognizing_remaining', 'recognition_interrupted'].includes(payload.status)
      || (
        ['failed', 'ocr_partial_failed'].includes(payload.status)
        && Number(payload.retryable_failed_papers || 0) > 0
      )
    ) {
      recognitionDone.value = false
      recognitionCanGrade.value = false
      recognitionError.value = ''
      currentMessage.value = ''
    }
    activeStep.value = 1
    updateRecognitionRecoveryState(payload, recoveryOptions)
    if (!recoveryOptions.suppressAutoResume && recognitionRecoveryState.value.status === 'available' && !recognizing.value) {
      scheduleRetryableOcrAutoResume(batchId.value, payload)
    }
    rememberOrientationBatch(batchId.value)
  }

  function rememberRecognitionBatch(payload) {
    if (!payload?.batch_id) return
    batchId.value = String(payload.batch_id)
    rememberOrientationBatch(batchId.value)
  }

  function upsertRecognitionPaper(payload) {
    const paperId = String(payload?.paper_id || '')
    const filename = String(payload?.filename || '')
    const index = papers.value.findIndex((item) => (
      (paperId && String(item.paper_id || '') === paperId)
      || (!paperId && filename && String(item.filename || '') === filename)
    ))
    if (index >= 0) papers.value[index] = mergeRecognitionPaper(papers.value[index], payload)
    else papers.value.push(payload)
  }

  async function confirmOrientationAndRetry() {
    if (!requireRecognitionService()) return
    const paper = orientationPaper.value
    if (!batchId.value || !paper?.paper_id || orientationSaving.value) return
    orientationSaving.value = true
    orientationImageError.value = ''
    if (!orientationActionId.value) {
      orientationActionId.value = globalThis.crypto?.randomUUID?.()
        || `orientation-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
    }
    try {
      const sourceResponse = await fetch(orientationImageUrl.value)
      if (!sourceResponse.ok) throw new Error('原图读取失败，请刷新后重试')
      const sourceBlob = await sourceResponse.blob()
      const sourceFile = new File(
        [sourceBlob],
        String(paper.filename || 'paper').replace(/\.[^.]+$/, '') || 'paper',
        { type: sourceBlob.type || 'image/jpeg' },
      )
      const rotatedFile = await createRotatedOcrReadyFile(
        sourceFile,
        orientationQuarterTurns.value,
      )
      const formData = new FormData()
      formData.append('action_id', orientationActionId.value)
      formData.append('quarter_turns_clockwise', String(orientationQuarterTurns.value))
      formData.append('file', rotatedFile)
      const payload = await api.grade.orientationConfirm(
        batchId.value,
        paper.paper_id,
        formData,
      )
      applyOrientationState(payload)
      orientationActionId.value = ''
      orientationDialogVisible.value = false
      if (payload.can_grade) {
        await onConfirmGrade()
        return
      }
      ElMessage.success('该卷方向已确认并完成单卷重识别，请继续处理其余异常试卷')
    } catch (err) {
      orientationImageError.value = orientationErrorMessage(err, '方向确认失败，请稍后重试')
      notifyProcessingError(err, orientationImageError.value)
    } finally {
      orientationSaving.value = false
    }
  }

  function canExcludeRecognitionPaper(paper) {
    if (!paper?.paper_id || !batchId.value || recognizing.value || grading.value) return false
    if (isExcludedPaper(paper)) return false
    // Resumable or waiting work goes first; exclusion is only for pages whose
    // automatic recovery is exhausted.
    if (['waiting', 'available', 'service_fault'].includes(recognitionRecoveryState.value.status)) return false
    return paperOcrFailed(paper)
  }

  async function excludeRecognitionPaper(paper) {
    if (!canExcludeRecognitionPaper(paper) || excludingPaperId.value) return false
    const label = paper.filename || paper.student_name || '该试卷'
    if (!window.confirm(`确定将“${label}”排除出本批次吗？该卷不会被批阅，也不会写入学生档案；其余试卷可以继续批阅。如需批阅该卷，请重新拍摄后单独上传。`)) return false
    excludingPaperId.value = String(paper.paper_id)
    try {
      const payload = await api.grade.excludePaper(batchId.value, paper.paper_id)
      applyOrientationState(payload)
      if (payload?.can_grade) {
        ElMessage.success('已排除该卷，开始批阅其余试卷')
        await onConfirmGrade()
        return true
      }
      ElMessage.success('已排除该卷，请继续处理其余未完成试卷')
      return true
    } catch (err) {
      notifyProcessingError(err, reviewErrorMessage(err, '排除失败，请刷新后重试'))
      return false
    } finally {
      excludingPaperId.value = ''
    }
  }

  function reset() {
    recognitionAutoArmed.value = true
    clearRecognitionAutoResume({ resetAttempts: true })
    fileList.value = []
    forgetOrientationBatch()
    resetRecognitionCompletionState()
    gradeError.value = ''
    // A pending/confirmed template freezes the grading contract; only clear student papers.
    if (confirmedTemplateId.value || pendingTemplateId.value || templateDialogVisible.value) return
    form.category = ''
    form.exam_name = ''
    form.mode = 'precise'
    templateMode.value = 'marked'
    templateColumnMode.value = 'double'
  }

  async function continueAfterRecognition() {
    if (recognizing.value || grading.value) return false
    if (!recognitionDone.value || !recognitionCanGrade.value) {
      recognitionError.value ||= '本批次 OCR 尚未完整完成，不能开始批阅。'
      ElMessage.warning(recognitionError.value)
      return false
    }
    if (!questions.value.length) {
      recognitionError.value = '识别结果缺少可批阅题目，请重新识别或联系管理员。'
      ElMessage.warning(recognitionError.value)
      return false
    }
    return onConfirmGrade()
  }

  function startNewBatch() {
    if (recognizing.value || grading.value) return
    // Only release this page's recovery pointers; durable server batches and
    // the teacher-confirmed template remain untouched.
    clearRecognitionAutoResume({ resetAttempts: true })
    forgetOrientationBatch()
    activeStep.value = 0
    questions.value = []
    papers.value = []
    batchId.value = ''
    resetRecognitionCompletionState()
    gradeError.value = ''
    resetFiles()
    forgetReviewBatch()
    clearReviewDisplay()
    resetRecognitionRecoveryState()
    identityResolutions.value = []
    identityOptionsLoading.value = false
    identityOptionsError.value = ''
    recognized.current = 0
    recognized.total = 0
    currentMessage.value = ''
  }

  // ── 开始识别：先补齐批次信息（后端必需），再进入识别 ──
  function onStartRecognize() {
    if (!requireRecognitionService()) return
    if (!fileList.value.length) { ElMessage.warning('请先选择文件'); return }
    if (!confirmedTemplateId.value) {
      ElMessage.warning('请先识别并保存本次模板')
      return
    }
    if (templateDialogVisible.value) {
      ElMessage.warning('请先保存或关闭模板编辑窗口')
      return
    }
    if (!anonymous.value && !auth.currentClassId) {
      ElMessage.warning('请先在顶部选择或创建班级')
      return
    }
    batchDialogVisible.value = true
  }

  function maybeAutoStartRecognition() {
    // Only the saved answer/geometry contract permits comparison and grading;
    // finishing template correction or student image preparation does not.
    if (!recognitionAutoArmed.value || !confirmedTemplateId.value || !fileList.value.length) return
    if (recognizing.value || grading.value || imagePreprocessing.value || imageExtracting.value) return
    if (recognitionBlocksNewWork.value || templateDialogVisible.value) return
    if (!batchConfirmValid.value || (!anonymous.value && !auth.currentClassId)) return
    recognitionAutoArmed.value = false
    batchDialogVisible.value = false
    void runRecognize()
  }

  function onBatchConfirm() {
    if (!requireRecognitionService()) return
    if (!batchConfirmValid.value) return
    recognitionAutoArmed.value = false
    batchDialogVisible.value = false
    runRecognize()
  }

  // ── 阶段一：按已确认模板识别学生试卷 ──
  async function runRecognize() {
    if (!requireRecognitionService()) return
    if (recognizing.value) return
    if (!fileList.value.length) { ElMessage.warning('请先选择文件'); return }
    if (!form.category) { ElMessage.warning('请选择批次类型'); return }

    const fd = new FormData()
    appendStudentUploadFiles(fd, fileList.value)
    fd.append('category', form.category)
    fd.append('mode', form.mode)
    if (anonymous.value) fd.append('anonymous', 'true')
    else if (inheritIdentity.value) fd.append('inherit_identity', 'true')
    if (form.category === 'exam' && form.exam_name) fd.append('exam_name', form.exam_name)
    // 学生试卷始终复用已确认模板；服务端拒绝缺少 template_id 的请求。
    fd.append('template_id', confirmedTemplateId.value)

    recognizing.value = true
    activeStep.value = 1
    identityResolutions.value = []
    identityOptionsLoading.value = false
    identityOptionsError.value = ''
    papers.value = []
    questions.value = []
    clearRecognitionAutoResume({ resetAttempts: true })
    // Keep the old durable storage pointer until this request creates a batch,
    // but never use that older batch to recover a failed new upload.
    batchId.value = ''
    resetRecognitionCompletionState()
    resetRecognitionRecoveryState()
    gradeError.value = ''
    recognized.current = 0
    recognized.total = fileList.value.length
    progressList.value = []
    gradeTotal.value = fileList.value.length
    currentMessage.value = ''
    recognizeController.value = new AbortController()

    let requestBatchId = ''
    let doneData = null
    try {
      const response = await api.grade.recognize(fd, recognizeController.value.signal)
      void loadDailyUsage()
      if (!response.ok) {
        throw await createSseResponseError(response, '识别请求失败')
      }
      await consumeSse(response, (evt, data) => {
        if (data?.batch_id) {
          requestBatchId = String(data.batch_id)
          releasePhoneCaptureSession()
        }
        if (evt === 'progress') {
          rememberRecognitionBatch(data)
          currentMessage.value = data.message || ''
          if (typeof data.current === 'number') recognized.current = data.current
          if (typeof data.total === 'number') recognized.total = data.total
        } else if (evt === 'paper') {
          rememberRecognitionBatch(data)
          upsertRecognitionPaper(data)
        } else if (evt === 'result') {
          progressList.value.push(data)
        } else if (evt === 'done') {
          rememberRecognitionBatch(data)
          doneData = data
          questions.value = data.questions || []
          clearRecognitionAutoResume({ resetAttempts: true })
          applyRecognitionDone(data)
          currentMessage.value = ''
        } else if (evt === 'error') {
          rememberRecognitionBatch(data)
          throw createSseEventError(data, '识别出错')
        }
      })
      if (!recognitionCanGrade.value) {
        activeStep.value = 1
        scheduleRetryableOcrAutoResume(requestBatchId, doneData)
        return
      }
      if (!questions.value.length) {
        throw new Error('未识别到可用于批改的题目，请检查视觉模型服务、网络或试卷图片')
      }
      // 模板题干和答案已经由老师确认，识别完成后直接进入批改。
      activeStep.value = 1
      await onConfirmGrade()
    } catch (err) {
      const signal = recognizeController.value?.signal
      if (!isSseRequestCancelled(err, signal)) {
        const failureCode = String(err?.code || '').toUpperCase()
        const targetBatchId = requestBatchId
        const connectionRecovery = canRecoverRecognitionConnection(err, signal)
        const serviceFault = isSseServiceFault(err, { signal })
        const refreshed = targetBatchId
          ? await refreshRecognitionStateAfterFailure(targetBatchId, {
              failureCode, serviceFault: serviceFault && !connectionRecovery,
              isCurrent: () => !signal?.aborted && targetBatchId === String(batchId.value || ''),
            })
          : false
        if (signal?.aborted) return
        if (connectionRecovery && await continueRefreshedRecognition(refreshed)) return
        if (connectionRecovery && recoverRecognitionConnection(targetBatchId, refreshed)) return
        reportSseServiceFault(
          err, markServiceFault,
          '学生试卷识别链路中断，请稍后重试；若持续发生请联系管理员。',
          { signal },
        )
        if (!targetBatchId) updateRecognitionRecoveryState({}, { failureCode, serviceFault })
        if (!refreshed) recognitionDone.value = false
        recognitionCanGrade.value = false
        const rawMessage = String(err?.message || '')
        recognitionError.value = (
          err instanceof SseTransportError
          || rawMessage === 'Failed to fetch'
          || rawMessage === 'Network Error'
          || /networkerror/i.test(rawMessage)
        )
          ? '识别连接中断：服务器可能正在重启或预处理超时，请勿连续重复提交；稍后重试，若仍失败请联系管理员查看该时段服务日志。'
          : (rawMessage || '识别失败')
        currentMessage.value = ''
        notifyProcessingError(err, recognitionRecoveryState.value.message || recognitionError.value)
        activeStep.value = 1
      } else {
        activeStep.value = 0
      }
    } finally {
      recognizing.value = false
      recognizeController.value = null
    }
  }

  return {
    orientationImageRequestId, resetOrientationReview, openOrientationReview,
    rotateOrientationPreview, applyOrientationState, rememberRecognitionBatch,
    upsertRecognitionPaper, confirmOrientationAndRetry, canExcludeRecognitionPaper,
    excludeRecognitionPaper, reset, continueAfterRecognition, startNewBatch, onStartRecognize,
    maybeAutoStartRecognition, onBatchConfirm, runRecognize,
  }
}
