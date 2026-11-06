// The recognition status the grading page (views/Upload.vue) shows per paper and
// per batch: OCR outcome, orientation checks, review errors, whether a broken
// recognition connection can be recovered, and the hardware the next step needs.
import { isSseRequestCancelled, SseTransportError } from './sse.js'

export const ACCELERATOR_UNAVAILABLE_MESSAGE = '加速节点不可用，请使用客户端'

export function requiredHardwareState(hardware) {
  const status = String(hardware?.status || 'fault')
  // Only the server's explicit contract can make this extra hardware check
  // informational. Model/core readiness remains a separate mandatory gate.
  const advisory = hardware?.advisory === true
  if (status === 'ready') {
    return {
      status: 'available',
      blocksNewWork: false,
      advisory,
    }
  }
  if (['waiting_accelerator', 'pending', 'unavailable'].includes(status)) {
    return {
      status: 'unavailable',
      message: advisory ? '状态已记录' : ACCELERATOR_UNAVAILABLE_MESSAGE,
      blocksNewWork: !advisory,
      advisory,
    }
  }
  return {
    status: 'fault',
    message: advisory ? '状态已记录' : ACCELERATOR_UNAVAILABLE_MESSAGE,
    blocksNewWork: !advisory,
    advisory,
  }
}

export function paperOcrPending(paper) {
  return ['queued', 'running'].includes(String(paper?.ocr_status || '').toLowerCase())
}

export function paperOcrPendingLabel(paper) {
  const message = String(paper?.error || '').trim()
  if (message) return message
  return String(paper?.ocr_status || '').toLowerCase() === 'running' ? '正在识别处理' : '等待识别处理'
}

export function paperOcrFailed(paper) {
  const status = String(paper?.ocr_status || '').toLowerCase()
  if (status) return status === 'failed'
  return Boolean(paper?.error && !paper.requires_manual_review)
}

export function paperOcrNeedsReview(paper) {
  const status = String(paper?.ocr_status || '').toLowerCase()
  if (status) return status === 'needs_review'
  return Boolean(paper?.error && paper.requires_manual_review)
}

export function paperOcrSucceeded(paper) {
  const status = String(paper?.ocr_status || '').toLowerCase()
  if (status) return status === 'succeeded'
  return Boolean(paper && !paper.error)
}

export function canRecoverRecognitionConnection(error, signal) {
  if (isSseRequestCancelled(error, signal)) return false
  const code = String(error?.code || error?.processingCode || '').toUpperCase()
  if (['ACCELERATOR_WAITING', 'BATCH_RECOGNITION_BUSY'].includes(code)) return true
  // A business/authorization failure is not made healthy by a successful GET.
  if (code || error?.sseFailureKind === 'application') return false
  const status = Number(error?.status || error?.response?.status || 0)
  if (status) return [502, 503, 504].includes(status)
  return error instanceof SseTransportError || (!error?.sseFailureKind
    && /failed to fetch|fetch failed|network\s*error|networkerror|load failed|connection (?:was )?(?:lost|closed|reset)/i.test(String(error?.message || '')))
}

export function recognitionIncompleteMessage(stats) {
  return `本批次 OCR 未完整返回：上传 ${stats.uploaded} 张，成功 ${stats.succeeded} 张，需人工复核 ${stats.manualReview} 张，失败 ${stats.failed} 张，未完成 ${stats.unresolved} 张。为避免漏批，系统没有开始批阅。`
}

export function orientationReady(paper) {
  return ['auto_passed', 'teacher_confirmed'].includes(String(paper?.orientation_status || ''))
}

export function orientationReasonText(reason) {
  const labels = {
    page_tilt_exceeds_limit: '页面倾斜过大，请重新拍摄',
    exif_rotation_requires_confirmation: '图片带有旋转方向，请旋转后重新上传',
    orientation_confidence_too_low: '无法确认方向，请重新拍摄',
    orientation_detection_failed: '无法检测方向，请重新拍摄',
    teacher_confirmed: '教师已确认方向',
  }
  return labels[reason] || '请确认文字正立后重新上传'
}

export function orientationErrorMessage(err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') return detail.message || detail.error || fallback
  return err?.message || fallback
}

export function reviewErrorMessage(err, fallback) {
  return err?.response?.data?.detail || err?.message || fallback
}

export function reviewPaperId(row) {
  return String(row?.paper_id || row?.review_id || '')
}
