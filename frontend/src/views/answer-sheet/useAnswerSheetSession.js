/**
 * The answer-sheet session and the service behind it.
 *
 * - applySession takes a session from the server (normalizeQuestion, summarizeLayout); canOpenStage and
 *   openStage move between stages.
 * - run and runAnswerSheetStream send a request or a stream and turn failures into the page's messages
 *   (normalizeServiceError, applyAcceleratorStreamError).
 * - refreshAvailability polls the service; markServiceFault holds a recent fault visible and
 *   requireAnswerSheetService refuses new work while the service blocks it.
 *
 * Moved from views/AnswerSheet.vue; the page passes its earlier parts in ctx (see AnswerSheet.vue).
 */
import { shallowRef } from 'vue'
import { ElMessage } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api, {
  mergeHardwareAvailabilityStatus,
  normalizeHardwareAvailability,
} from '@/api'
import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  isSseServiceFault,
  SseTransportError,
} from '@/utils/sse'

export function useAnswerSheetSession(ctx) {
  const {
    STORAGE_KEY, busy, operationMessage, operationProgress, serviceState, hardwareState,
    SERVICE_FAULT_HOLD_MS, ACCELERATOR_STREAM_ERROR_STATES, session, activeStage, answerSource,
    templateCodeInput, gradingDrafts, questionDrafts, answerRegions, identityRegions, layoutSummary,
    currentPageId, activeDraftIndex, currentReviewPaperId, completedStage, serviceBlocksNewWork,
  } = ctx

  const availabilityPollTimer = shallowRef(null)
  let availabilityCheckPending = false
  let serviceFaultHoldUntil = 0

  const answerSheetStreamController = shallowRef(null)

  function normalizeQuestion(raw) {
    const steps = Array.isArray(raw.solution_steps) ? raw.solution_steps : []
    const type = ['choice', 'fill', 'solve'].includes(raw.question_type) ? raw.question_type : 'solve'
    const choiceMode = type === 'choice' && ['single', 'multiple'].includes(raw.choice_mode) ? raw.choice_mode : 'unknown'
    return { ...raw, question_no: String(raw.question_no || raw.question_number || ''), question_text: String(raw.question_text || raw.content || ''), question_type: type, choice_mode: choiceMode, standard_answer: String(raw.standard_answer || raw.answer || ''), max_score: raw.max_score ?? null, grading_method: raw.grading_method || ({ choice: 'exact', fill: 'equivalent', solve: '' }[type]), solution_steps_text: steps.join('\n') }
  }
  function applySession(data, { chooseStage = true } = {}) {
    session.value = data
    if (data.shared_template_code) templateCodeInput.value = data.shared_template_code
    answerSource.value = data.answer_source || 'embedded'
    questionDrafts.value = (data.answer_spec_snapshot?.length ? data.answer_spec_snapshot : data.source_question_drafts || []).map(normalizeQuestion)
    answerRegions.value = structuredClone(data.answer_regions || [])
    identityRegions.value = structuredClone(data.identity_regions || [])
    gradingDrafts.value = structuredClone(data.grouping_decisions || [])
    activeDraftIndex.value = Math.min(activeDraftIndex.value, Math.max(gradingDrafts.value.length - 1, 0))
    const draftPages = gradingDrafts.value[activeDraftIndex.value]?.paper_ids || []
    currentReviewPaperId.value = draftPages.includes(currentReviewPaperId.value) ? currentReviewPaperId.value : (draftPages[0] || '')
    layoutSummary.value = summarizeLayout()
    currentPageId.value = currentPageId.value && data.answer_sheet_pages?.some(page => page.page_id === currentPageId.value) ? currentPageId.value : (data.answer_sheet_pages?.[0]?.page_id || '')
    localStorage.setItem(STORAGE_KEY, data.session_id)
    if (chooseStage) activeStage.value = completedStage.value
  }
  function summarizeLayout() {
    const specs = session.value?.answer_spec_snapshot || []
    const required = new Set(specs.map(item => String(item.question_no)))
    const bound = new Set(answerRegions.value.filter(item => item.teacher_confirmed).map(item => String(item.question_no)))
    const conflicts = []
    for (let i = 0; i < answerRegions.value.length; i += 1) for (let j = i + 1; j < answerRegions.value.length; j += 1) {
      const a = answerRegions.value[i]; const b = answerRegions.value[j]
      if (a.page_id !== b.page_id || a.question_no === b.question_no) continue
      const overlap = Math.max(0, Math.min(a.bbox[2], b.bbox[2]) - Math.max(a.bbox[0], b.bbox[0])) * Math.max(0, Math.min(a.bbox[3], b.bbox[3]) - Math.max(a.bbox[1], b.bbox[1]))
      if (overlap) conflicts.push([a.region_id, b.region_id])
    }
    const missing = [...required].filter(no => !bound.has(no))
    return { question_count: required.size, bound_count: bound.size, missing_questions: missing, conflicts, identity_region_count: identityRegions.value.length, can_freeze: !missing.length && !conflicts.length && identityRegions.value.length > 0 && identityRegions.value.every(item => item.teacher_confirmed) }
  }
  function canOpenStage(index) { return !!session.value && index <= Math.max(completedStage.value, 0) }
  function openStage(index) { if (canOpenStage(index)) activeStage.value = index }
  function markServiceFault(message) {
    serviceFaultHoldUntil = Date.now() + SERVICE_FAULT_HOLD_MS
    serviceState.value = {
      status: 'fault',
      label: '链路故障',
      message: message || '答题卡批阅链路发生故障，已保存内容仍可查看；请稍后重试。',
    }
  }
  function requireAnswerSheetService() {
    if (!serviceBlocksNewWork.value) return true
    ElMessage.error(serviceState.value.message || '答题卡批阅链路当前不可用，请稍后再试。')
    return false
  }
  function normalizeServiceError(error) {
    const detail = error?.response?.data?.detail
    const detailObject = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : null
    const axiosCode = String(error?.code || '')
    const transportFailure = ['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK'].includes(axiosCode)
    return {
      message: String(error?.message || ''),
      status: Number(error?.response?.status || error?.status) || 0,
      code: String(error?.response?.data?.code || error?.response?.data?.error || detailObject?.code || detailObject?.error || axiosCode),
      sseFailureKind: transportFailure ? 'transport' : error?.sseFailureKind,
    }
  }
  function applyAcceleratorStreamError(error) {
    const acceleratorState = ACCELERATOR_STREAM_ERROR_STATES[String(error?.code || '').trim().toUpperCase()]
    if (!acceleratorState) return false
    const serverMessage = String(error?.message || '').trim()
    const message = serverMessage && !/保存|保留/.test(serverMessage)
      ? `${serverMessage} 已完成进度保留在当前会话，可在节点恢复后继续处理。`
      : (serverMessage || acceleratorState.message)
    hardwareState.value = normalizeHardwareAvailability({
      hardware: { status: acceleratorState.hardwareStatus, message },
    })
    if (acceleratorState.serviceStatus === 'fault') {
      serviceFaultHoldUntil = Date.now() + SERVICE_FAULT_HOLD_MS
    }
    serviceState.value = {
      status: acceleratorState.serviceStatus,
      label: acceleratorState.label,
      message,
    }
    if (acceleratorState.serviceStatus === 'queued') ElMessage.warning(message)
    else notifyProcessingError(error, message)
    return true
  }
  async function run(task, success) {
    busy.value = true
    try {
      const data = await task()
      if (success) ElMessage.success(success)
      return data
    } catch (error) {
      if (isSseServiceFault(normalizeServiceError(error))) {
        markServiceFault('答题卡批阅链路中断，已保存内容仍可查看；请稍后重试，若持续发生请联系管理员。')
      }
      throw error
    } finally {
      busy.value = false
    }
  }
  async function runAnswerSheetStream(request, fallbackMessage, successMessage, onProgress) {
    if (!requireAnswerSheetService()) return null
    if (busy.value) return null
    busy.value = true
    operationMessage.value = fallbackMessage
    const controller = new AbortController()
    answerSheetStreamController.value = controller
    try {
      const response = await request(controller.signal)
      if (!response.ok) throw await createSseResponseError(response, fallbackMessage)
      let completed = null
      await consumeSse(response, (event, data) => {
        if (event === 'progress') {
          const current = Number(data?.current)
          const total = Number(data?.total)
          operationMessage.value = data?.message || fallbackMessage
          operationProgress.value = Number.isFinite(current) && Number.isFinite(total) && total > 0
            ? { current, total }
            : null
          if (onProgress) onProgress(data)
        } else if (event === 'done') {
          completed = data
        } else if (event === 'error') {
          throw createSseEventError(data, fallbackMessage)
        }
      })
      if (!completed) throw new SseTransportError(`${fallbackMessage}连接已结束，但未收到完成状态`)
      if (successMessage) ElMessage.success(successMessage)
      return completed
    } catch (error) {
      if (isSseRequestCancelled(error, controller.signal)) return null
      try {
        const latest = await api.answerSheet.getSession(session.value.session_id)
        applySession(latest, { chooseStage: false })
      } catch {
        // Keep the last local snapshot if the status endpoint is also unreachable.
      }
      const normalizedError = normalizeServiceError(error)
      const acceleratorHandled = applyAcceleratorStreamError(normalizedError)
      if (!acceleratorHandled && isSseServiceFault(normalizedError, { signal: controller.signal })) {
        markServiceFault('答题卡批阅链路中断，已完成进度已保存；请稍后重试，若持续发生请联系管理员。')
      }
      if (!acceleratorHandled) notifyProcessingError(error, error?.message || fallbackMessage)
      return null
    } finally {
      if (answerSheetStreamController.value === controller) answerSheetStreamController.value = null
      operationMessage.value = ''
      operationProgress.value = null
      busy.value = false
    }
  }
  async function refreshAvailability() {
    if (availabilityCheckPending) return
    availabilityCheckPending = true
    try {
      const state = await api.answerSheet.availability()
      const hardware = normalizeHardwareAvailability(state)
      hardwareState.value = hardware
      const labels = { available: '可用', queued: '需排队', unavailable: '暂不可用', fault: '链路故障' }
      const messages = {
        available: '答题卡批阅服务当前可用。',
        queued: '答题卡批阅当前需排队；可以准备三类材料，提交后系统会等待准入。',
        unavailable: '答题卡批阅链路当前不可用，请稍后重试或联系管理员。',
        fault: '答题卡批阅链路发生故障，请稍后重试或联系管理员。',
      }
      if (!labels[state?.status]) {
        markServiceFault('答题卡批阅链路状态响应异常，请稍后重试或联系管理员。')
        return
      }
      const effectiveStatus = mergeHardwareAvailabilityStatus(state.status, hardware.status)
      const hardwareServiceStatus = mergeHardwareAvailabilityStatus('available', hardware.status)
      const effectiveMessage = String(hardwareServiceStatus === effectiveStatus
        ? (hardware.message || state.message || messages[effectiveStatus])
        : (state.message || hardware.message || messages[effectiveStatus]))
      if (
        ['available', 'queued'].includes(effectiveStatus)
        && serviceState.value.status === 'fault'
        && Date.now() < serviceFaultHoldUntil
      ) return
      if (effectiveStatus === 'fault') {
        markServiceFault(effectiveMessage || messages.fault)
        return
      }
      serviceState.value = {
        status: effectiveStatus,
        label: String(effectiveStatus === state.status ? (state.label || labels[effectiveStatus]) : labels[effectiveStatus]),
        message: effectiveMessage,
      }
    } catch {
      hardwareState.value = normalizeHardwareAvailability({
        hardware: { status: 'fault', message: '无法获取本地加速节点状态。' },
      })
      markServiceFault('无法获取答题卡批阅链路状态，请检查网络或联系管理员。')
    } finally {
      availabilityCheckPending = false
    }
  }

  return {
    availabilityPollTimer, availabilityCheckPending, serviceFaultHoldUntil,
    answerSheetStreamController, normalizeQuestion, applySession, summarizeLayout, canOpenStage,
    openStage, markServiceFault, requireAnswerSheetService, normalizeServiceError,
    applyAcceleratorStreamError, run, runAnswerSheetStream, refreshAvailability,
  }
}
