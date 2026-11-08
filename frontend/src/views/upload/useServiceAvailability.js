/**
 * Whether the grading service, the template service and the registration hardware can take new work.
 *
 * - refreshAvailability reads both services' availability and the registration hardware gate (the page
 *   polls it); markServiceFault holds a recent fault visible for a while.
 * - requireBatchService, requireRecognitionService and requireTemplateService refuse new work with a
 *   message while a service blocks it; loadDailyUsage reads today's usage.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { ElMessage } from 'element-plus'
import api from '@/api'
import { requiredHardwareState, ACCELERATOR_UNAVAILABLE_MESSAGE } from '@/utils/recognitionStatus'

export function useServiceAvailability(ctx) {
  const {
    dailyUsage, serviceState, SERVICE_FAULT_HOLD_MS, TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE,
    serviceFaultHoldUntil, serviceBlocksNewWork, templateServiceState, templateServiceBlocksNewWork,
    registrationHardwareState, registrationHardwareBlocksNewWork,
  } = ctx

  let availabilityCheckPending = false

  function markServiceFault(message) {
    serviceFaultHoldUntil.value = Date.now() + SERVICE_FAULT_HOLD_MS
    serviceState.value = {
      status: 'fault',
      label: '链路故障',
      message: message || '批改链路发生故障，请稍后重试。',
    }
  }

  function requireBatchService() {
    if (!serviceBlocksNewWork.value) return true
    ElMessage.error(serviceState.value.message || '批改链路当前不可用，请稍后再试。')
    return false
  }

  function requireRecognitionService() {
    if (!requireBatchService()) return false
    if (!registrationHardwareBlocksNewWork.value) return true
    ElMessage.error(ACCELERATOR_UNAVAILABLE_MESSAGE)
    return false
  }

  function requireTemplateService() {
    if (!templateServiceBlocksNewWork.value) return true
    ElMessage.error(templateServiceState.value.message || '模板识别链路当前不可用，请稍后再试。')
    return false
  }

  async function loadDailyUsage() {
    try {
      const data = await api.usage.stats()
      dailyUsage.value = data?.daily_images || data?.monthly_images || null
    } catch {
      dailyUsage.value = null
    }
  }

  async function refreshAvailability() {
    if (availabilityCheckPending) return
    availabilityCheckPending = true
    try {
      const [stateResult, templateStateResult] = await Promise.allSettled([
        api.grade.availability(),
        api.grade.templateAvailability(),
      ])
      if (stateResult.status !== 'fulfilled') {
        markServiceFault('无法获取批改链路状态，请检查网络或联系管理员。')
        templateServiceState.value = {
          status: 'fault',
          label: '模板链路故障',
          message: TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE,
          blocksNewWork: true,
        }
        registrationHardwareState.value = requiredHardwareState(null)
        return
      }
      const state = stateResult.value
      const templateState = templateStateResult.status === 'fulfilled'
        ? templateStateResult.value
        : null
      const labels = {
        available: '可用',
        queued: '需排队',
        unavailable: '暂不可用',
        fault: '链路故障',
      }
      if (!labels[state?.status]) {
        markServiceFault('批改链路状态响应异常，请稍后重试或联系管理员。')
        templateServiceState.value = {
          status: 'fault',
          label: '模板链路故障',
          message: '批改链路状态响应异常，请稍后重试或联系管理员。',
          blocksNewWork: true,
        }
        registrationHardwareState.value = requiredHardwareState(null)
        return
      }
      if (
        ['available', 'queued'].includes(state.status)
        && serviceState.value.status === 'fault'
        && Date.now() < serviceFaultHoldUntil.value
      ) {
        // Keep the recent transport fault visible while still refreshing hardware gates below.
      } else if (state.status === 'fault') {
        markServiceFault(state.message || '批改链路发生故障，请稍后重试。')
      } else {
        serviceState.value = {
          status: state.status,
          label: String(state.label || labels[state.status]),
          message: String(state.message || ''),
        }
      }
      if (!labels[templateState?.status]) {
        templateServiceState.value = {
          status: 'fault',
          label: '模板链路故障',
          message: TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE,
          blocksNewWork: true,
        }
        registrationHardwareState.value = requiredHardwareState(null)
      } else {
        const registrationHardware = requiredHardwareState(templateState.registration_hardware)
        registrationHardwareState.value = registrationHardware
        const templateModelStatus = labels[templateState.service_status]
          ? templateState.service_status
          : templateState.status
        const templateModelBlocks = ['checking', 'unavailable', 'fault'].includes(templateModelStatus)
        // Template correction and column montage run on the server CPU. Only the
        // actual model/core service state can block template import; the student
        // registration accelerator is handled separately below.
        templateServiceState.value = serviceBlocksNewWork.value
          ? {
              status: serviceState.value.status,
              label: serviceState.value.label,
              message: serviceState.value.message,
              blocksNewWork: true,
            }
          : {
              status: templateModelStatus,
              label: String(templateState.label || labels[templateModelStatus]),
              message: String(templateState.message || ''),
              blocksNewWork: templateModelBlocks,
            }
      }
    } catch {
      markServiceFault('无法获取批改链路状态，请检查网络或联系管理员。')
      templateServiceState.value = {
        status: 'fault',
        label: '模板链路故障',
        message: TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE,
        blocksNewWork: true,
      }
      registrationHardwareState.value = requiredHardwareState(null)
    } finally {
      availabilityCheckPending = false
    }
  }

  return {
    availabilityCheckPending, markServiceFault, requireBatchService, requireRecognitionService,
    requireTemplateService, loadDailyUsage, refreshAvailability,
  }
}
