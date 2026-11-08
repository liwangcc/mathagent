/**
 * Importing a teacher template and recognizing it: choosing the template pages, the recognition request,
 * and its checkpoint across reloads.
 *
 * - onTemplateFileChange takes the chosen pages; onRecognizeTemplate sends them and waits for the result
 *   (waitForTemplateRecognition, waitForTemplatePoll), and applyTemplateRecognitionResult turns it into
 *   the editable pages.
 * - The recognition checkpoint in localStorage (templateRecognitionStorageKey, rememberTemplateRecognition,
 *   forgetTemplateRecognition, loadTemplateRecognitionCheckpoint) lets resumeTemplateRecognitionCheckpoint
 *   pick a running recognition back up after a reload.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { ElMessage } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  reportSseServiceFault,
  SseTransportError,
} from '@/utils/sse'
import { validateUploadList, compressImageFiles } from '@/utils/uploadValidation'
import { extractImagesFromFileList } from '@/utils/fileExtraction'
import { appendDesktopCanonicalCandidates } from '@/utils/desktopTemplateCanonicalization'
import { normalizeAnswerSpecForEditor } from '@/utils/answerSpecEditor'

export function useTemplateRecognition(ctx) {
  const {
    anonymous, auth, TEMPLATE_RECOGNITION_STORAGE_PREFIX, markServiceFault, templateMode,
    templateColumnMode, templateFiles, templatePreprocessing, recognizingTemplate,
    templateRecognizeMessage, templateRecognizeError, templateTeachingContext,
    templateDialogVisible, templateBankImported, templatePages, templateCutBusy, templateCutResult,
    templateCutProgress, templateCutActionId, templateCutPreviewError, pendingTemplateId, form,
    templateRecognizeController, templateCutController, templateCutCompletedRunToken,
    templateCutRunToken, syncTemplateBatchScores, requireTemplateService,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const stopTemplateCutPolling = (...args) => ctx.stopTemplateCutPolling(...args)
  const applyTemplateCutState = (...args) => ctx.applyTemplateCutState(...args)
  const revokeTemplateCutPreviews = (...args) => ctx.revokeTemplateCutPreviews(...args)
  const onRunTemplateCut = (...args) => ctx.onRunTemplateCut(...args)
  const loadTemplateCorrectionCandidates = (...args) => ctx.loadTemplateCorrectionCandidates(...args)
  const clearReviewDisplay = (...args) => ctx.clearReviewDisplay(...args)

  // ── 多页一次性模板：导入 N 页 → 识别 → 确认（即本次批改绝对权威）──
  async function onTemplateFileChange(e) {
    const raw = Array.from(e.target.files || [])
    e.target.value = ''
    if (!raw.length || templatePreprocessing.value || !requireTemplateService()) return
    templatePreprocessing.value = true
    let failedCount = 0
    const failedFiles = []
    try {
      const extracted = await extractImagesFromFileList(raw, { maxFiles: 20 })
      const fakeList = extracted.map((f) => ({ raw: f, name: f.name, size: f.size }))
      const result = validateUploadList(fakeList, { maxFiles: 20 })
      if (result.errors.length) throw new Error(result.errors.join('；'))
      if (!result.files.length) return
      const preparedFiles = await compressImageFiles(result.files, 1920, 0.88, {
        onFileError: ({ file }) => { failedCount += 1; failedFiles.push(file.name) },
      })
      if (failedCount) {
        templateFiles.value = []
        ElMessage.error(`[IMPORT_IMAGE_INVALID] ${failedFiles.join('、')}：${failedCount} 张模板图片损坏或尺寸超限，本次选择已清空，请检查原图后重新选择`)
        return
      }
      templateFiles.value = preparedFiles
      clearReviewDisplay()
    } catch (err) {
      notifyProcessingError(err, err.message || '模板图片预处理未完成，请重新选择')
    } finally {
      templatePreprocessing.value = false
    }
  }

  function templateRecognitionStorageKey() {
    const teacherId = auth.user?.id
    const mode = anonymous.value ? ':anonymous' : ''
    return teacherId ? `${TEMPLATE_RECOGNITION_STORAGE_PREFIX}${mode}:${teacherId}` : ''
  }

  function normalizeTemplateMode(value, fallback = 'blank') {
    const mode = String(value || '').trim().toLowerCase()
    if (mode === 'blank' || mode === 'marked') return mode
    return fallback === 'marked' ? 'marked' : 'blank'
  }

  function rememberTemplateRecognition(templateId, mode = templateMode.value) {
    const key = templateRecognitionStorageKey()
    if (!key || !templateId) return
    localStorage.setItem(key, JSON.stringify({
      template_id: String(templateId),
      category: form.category,
      exam_name: form.exam_name,
      template_mode: normalizeTemplateMode(mode, templateMode.value),
      column_mode: normalizeTemplateColumnMode(templateColumnMode.value),
    }))
  }

  function normalizeTemplateColumnMode(value, fallback = 'double') {
    const mode = String(value || '').trim().toLowerCase()
    if (['single', 'double', 'triple'].includes(mode)) return mode
    return ['single', 'double', 'triple'].includes(fallback) ? fallback : 'double'
  }

  function forgetTemplateRecognition() {
    const key = templateRecognitionStorageKey()
    if (key) localStorage.removeItem(key)
  }

  function loadTemplateRecognitionCheckpoint() {
    const key = templateRecognitionStorageKey()
    if (!key) return null
    try {
      const saved = JSON.parse(localStorage.getItem(key) || 'null')
      if (!saved?.template_id) throw new Error('invalid template checkpoint')
      return saved
    } catch {
      localStorage.removeItem(key)
      return null
    }
  }

  function isTerminalTemplateFailure(error) {
    return Boolean(error?.code) && error?.sseFailureKind === 'application'
  }

  function waitForTemplatePoll(milliseconds, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        const error = new Error('模板识别状态查询已取消')
        error.name = 'AbortError'
        reject(error)
        return
      }
      const onAbort = () => {
        window.clearTimeout(timer)
        const error = new Error('模板识别状态查询已取消')
        error.name = 'AbortError'
        reject(error)
      }
      const timer = window.setTimeout(() => {
        signal?.removeEventListener('abort', onAbort)
        resolve()
      }, milliseconds)
      signal?.addEventListener('abort', onAbort, { once: true })
    })
  }

  async function waitForTemplateRecognition(templateId, signal) {
    let consecutiveReadFailures = 0
    while (true) {
      let state
      try {
        state = await api.grade.templateRecognitionState(templateId, signal)
        consecutiveReadFailures = 0
      } catch (error) {
        if (isSseRequestCancelled(error, signal)) throw error
        consecutiveReadFailures += 1
        if (consecutiveReadFailures >= 3) throw error
        templateRecognizeMessage.value = '模板仍在服务器处理，正在重新连接任务状态…'
        await waitForTemplatePoll(1000 * (2 ** (consecutiveReadFailures - 1)), signal)
        continue
      }

      const status = String(state?.status || '')
      if (['ocr_done', 'confirmed', 'frozen'].includes(status)) {
        const pages = Array.isArray(state.pages) ? state.pages : []
        if (!pages.length) throw new SseTransportError('模板任务已完成，但服务器未返回页面结果')
        return { templateId: String(state.template_id || templateId), pages, state }
      }
      if (status === 'failed' || state?.terminal === true) {
        throw createSseEventError({
          code: state?.error_code || 'TEMPLATE_IMPORT_FAILED',
          message: state?.error || state?.message || '模板识别失败',
          error_info: state?.error_info,
        }, '模板识别失败')
      }

      templateRecognizeMessage.value = state?.message || '模板仍在服务器处理中，请勿重复提交…'
      const retryAfter = Math.min(5000, Math.max(500, Number(state?.retry_after_ms) || 1000))
      await waitForTemplatePoll(retryAfter, signal)
    }
  }

  async function applyTemplateRecognitionResult(tid, pages, mode = '', columnMode = '', teachingContext = {}) {
    const runToken = ++templateCutRunToken.value
    templateCutCompletedRunToken.value = null
    templateCutController.value?.abort()
    templateCutController.value = null
    stopTemplateCutPolling()
    templateCutBusy.value = false
    revokeTemplateCutPreviews()
    templateCutPreviewError.value = ''
    // Keep the unfinished-draft checkpoint until the template is confirmed or
    // cancelled: an unconfirmed template must stay recoverable after a reload
    // instead of being silently orphaned with no editor entry.
    pendingTemplateId.value = tid
    templateTeachingContext.value = teachingContext || {}
    templateMode.value = normalizeTemplateMode(mode, templateMode.value)
    templateColumnMode.value = normalizeTemplateColumnMode(columnMode || (
      pages?.[0]?.column_mode || pages?.[0]?.layout_mode || templateColumnMode.value
    ))
    templateBankImported.value = false
    templateCutResult.value = null
    templateCutActionId.value = ''
    templateCutProgress.value = { completed: 0, total: pages.length, remaining: pages.length }
    templatePages.value = pages.map((page) => ({
      ...page,
      template_mode: templateMode.value,
      layout_review_pending: page.layout_review_pending === true,
      anchor_diagnostics: page.anchor_diagnostics || null,
      template_ocr_diagnostics: page.template_ocr_diagnostics || null,
      questions: (page.questions || []).map((question) => {
        const normalized = normalizeAnswerSpecForEditor(question)
        return {
          ...normalized,
          _open: normalized.question_type === 'solve',
          correction_excluded: normalized.correction_excluded === true,
          authorized_wrong_cycle_ids: [],
          correction_candidates: [],
          correction_knowledge_summary: null,
        }
      }),
    }))
    syncTemplateBatchScores()
    templateDialogVisible.value = true
    const existingCut = await api.grade.templateCutCandidatesState(tid).catch(() => null)
    if (tid !== pendingTemplateId.value || runToken !== templateCutRunToken.value) return
    if (existingCut?.status === 'complete') {
      applyTemplateCutState(existingCut, runToken)
    } else {
      void onRunTemplateCut()
    }
    void loadTemplateCorrectionCandidates()
  }

  async function onRecognizeTemplate() {
    if (recognizingTemplate.value) return
    if (!requireTemplateService()) return
    if (!templateFiles.value.length) { ElMessage.warning('请先导入模板图片'); return }
    if (!form.category) { ElMessage.warning('请先选择本次批阅类型'); return }
    if (form.category === 'exam' && !form.exam_name.trim()) { ElMessage.warning('请先填写考试名称'); return }
    const fd = new FormData()
    templateFiles.value.forEach((f) => fd.append('files', f.raw || f))
    fd.append('category', form.category)
    fd.append('exam_name', form.category === 'exam' ? form.exam_name.trim() : '')
    fd.append('template_mode', normalizeTemplateMode(templateMode.value))
    const columnMode = normalizeTemplateColumnMode(templateColumnMode.value)
    fd.append('column_mode', columnMode)
    fd.append('layout_mode', columnMode)
    recognizingTemplate.value = true
    templateRecognizeError.value = ''
    templateRecognizeMessage.value = '正在上传并准备模板…'
    templateRecognizeController.value = new AbortController()
    let tid = ''
    try {
      await appendDesktopCanonicalCandidates(fd, templateFiles.value)
      const response = await api.grade.templateRecognize(fd, templateRecognizeController.value.signal)
      if (!response.ok) {
        throw await createSseResponseError(response, '模板识别失败')
      }
      tid = String(response.headers.get('X-Template-ID') || '')
      if (tid) rememberTemplateRecognition(tid, templateMode.value)
      let pages = []
      let teachingContext = {}
      await consumeSse(response, (evt, data) => {
        if (data?.teaching_context) teachingContext = data.teaching_context
        if (data?.template_id) {
          tid = String(data.template_id)
          if (data.template_mode) templateMode.value = normalizeTemplateMode(data.template_mode, templateMode.value)
          if (data.column_mode || data.layout_mode) {
            templateColumnMode.value = normalizeTemplateColumnMode(data.column_mode || data.layout_mode)
          }
          rememberTemplateRecognition(tid, templateMode.value)
        }
        if (evt === 'done') {
          if (data?.template_mode) templateMode.value = normalizeTemplateMode(data.template_mode, templateMode.value)
          if (data?.column_mode || data?.layout_mode) {
            templateColumnMode.value = normalizeTemplateColumnMode(data.column_mode || data.layout_mode)
          }
          pages = data.pages || []
        }
        else if (evt === 'error') { throw createSseEventError(data, '模板识别出错') }
        else if (evt === 'progress') templateRecognizeMessage.value = data.message || '模板识别中…'
      })
      if (!tid || !pages.length) {
        throw new SseTransportError('识别连接已结束，但没有收到模板结果')
      }
      await applyTemplateRecognitionResult(tid, pages, templateMode.value, templateColumnMode.value, teachingContext)
    } catch (err) {
      const signal = templateRecognizeController.value?.signal
      let failure = err
      const rawFailure = String(failure?.message || '')
      const canRecover = Boolean(tid) && (
        failure instanceof SseTransportError
        || failure?.sseFailureKind === 'transport'
        || /network|fetch|连接提前结束|连接读取中断/i.test(rawFailure)
      )
      if (canRecover && !isSseRequestCancelled(failure, signal)) {
        try {
          templateRecognizeError.value = ''
          templateRecognizeMessage.value = '模板识别连接中断，正在恢复服务器任务…'
          const recovered = await waitForTemplateRecognition(tid, signal)
          await applyTemplateRecognitionResult(
            recovered.templateId,
            recovered.pages,
            recovered.state?.template_mode || templateMode.value,
            recovered.state?.column_mode || recovered.state?.layout_mode || templateColumnMode.value,
            recovered.state?.teaching_context || {},
          )
          return
        } catch (recoveryError) {
          failure = recoveryError
        }
      }
      if (
        isTerminalTemplateFailure(failure)
        || [401, 403, 404].includes(Number(failure?.status || 0))
      ) forgetTemplateRecognition()
      if (!isSseRequestCancelled(failure, signal)) {
        reportSseServiceFault(
          failure,
          markServiceFault,
          '模板识别链路中断，请稍后重试；若持续发生请联系管理员。',
          { signal },
        )
        const raw = String(failure.message || '')
        templateRecognizeError.value = failure instanceof SseTransportError || /network|fetch|连接提前结束/i.test(raw)
          ? '模板识别连接中断，且无法读取服务器任务状态。请勿连续重复提交；稍后重试，如仍失败请联系管理员。'
          : (raw || '模板识别失败')
        notifyProcessingError(failure, templateRecognizeError.value)
      }
    } finally {
      recognizingTemplate.value = false
      templateRecognizeMessage.value = ''
      templateRecognizeController.value = null
    }
  }

  async function resumeTemplateRecognitionCheckpoint() {
    const checkpoint = loadTemplateRecognitionCheckpoint()
    if (!checkpoint?.template_id || recognizingTemplate.value) return false
    const checkpointCategory = String(checkpoint.category || '').trim().toLowerCase()
    if (!['daily', 'exam'].includes(checkpointCategory)) {
      forgetTemplateRecognition()
      return false
    }
    form.category = checkpointCategory
    form.exam_name = form.category === 'exam' ? String(checkpoint.exam_name || '') : ''
    templateMode.value = normalizeTemplateMode(checkpoint.template_mode, 'marked')
    templateColumnMode.value = normalizeTemplateColumnMode(checkpoint.column_mode)
    recognizingTemplate.value = true
    templateRecognizeError.value = ''
    templateRecognizeMessage.value = '正在恢复上次未完成的模板识别…'
    templateRecognizeController.value = new AbortController()
    try {
      const recovered = await waitForTemplateRecognition(
        String(checkpoint.template_id),
        templateRecognizeController.value.signal,
      )
      const stateCategory = String(recovered.state?.category || '')
      if (['daily', 'exam'].includes(stateCategory)) {
        form.category = stateCategory
        form.exam_name = stateCategory === 'exam'
          ? String(recovered.state?.exam_name || checkpoint.exam_name || '')
          : ''
      }
      if (String(recovered.state?.status || '') === 'frozen') {
        // The template already drives student recognition and cannot be edited
        // any more; retire the checkpoint instead of reopening a frozen editor.
        forgetTemplateRecognition()
        return false
      }
      await applyTemplateRecognitionResult(
        recovered.templateId,
        recovered.pages,
        recovered.state?.template_mode || templateMode.value,
        recovered.state?.column_mode || recovered.state?.layout_mode || templateColumnMode.value,
        recovered.state?.teaching_context || {},
      )
      ElMessage.success('已恢复服务器完成的模板识别结果')
      return true
    } catch (failure) {
      if (
        isTerminalTemplateFailure(failure)
        || [401, 403, 404].includes(Number(failure?.status || 0))
      ) forgetTemplateRecognition()
      const signal = templateRecognizeController.value?.signal
      if (!isSseRequestCancelled(failure, signal)) {
        reportSseServiceFault(
          failure,
          markServiceFault,
          '无法恢复模板识别任务，请检查网络或联系管理员。',
          { signal },
        )
        templateRecognizeError.value = failure?.message || '模板识别任务恢复失败'
        notifyProcessingError(failure, templateRecognizeError.value)
      }
      return false
    } finally {
      recognizingTemplate.value = false
      templateRecognizeMessage.value = ''
      templateRecognizeController.value = null
    }
  }

  return {
    onTemplateFileChange, templateRecognitionStorageKey, normalizeTemplateMode,
    rememberTemplateRecognition, normalizeTemplateColumnMode, forgetTemplateRecognition,
    loadTemplateRecognitionCheckpoint, isTerminalTemplateFailure, waitForTemplatePoll,
    waitForTemplateRecognition, applyTemplateRecognitionResult, onRecognizeTemplate,
    resumeTemplateRecognitionCheckpoint,
  }
}
