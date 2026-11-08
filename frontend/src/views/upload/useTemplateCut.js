/**
 * Cutting the confirmed template into per-question images and showing their previews.
 *
 * - onRunTemplateCut starts a cut run; pollTemplateCutState / loadTemplateCutState follow it and
 *   applyTemplateCutState applies its state, guarded by the run token so a stale run never wins.
 * - loadTemplateCutPreviews loads the finished pages' previews; revokeTemplateCutPreviews releases them,
 *   and templateCutErrorMessage explains a failed run.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { ElMessage } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import { templateDrawingReview, mergeTemplateDrawingCandidates } from '@/utils/templateReview'
import {
  templatePageReviewPending,
  cloneTemplateColumns,
  cloneQuestionAnchors,
  mergeTemplateAnswerRegionSources,
  templateRegionCandidateUsable,
} from '@/utils/templateGeometry'

export function useTemplateCut(ctx) {
  const {
    templatePages, templateCutBusy, templateCutResult, templateCutProgress, templateCutActionId,
    templateCutImageType, templateCutPreviewPages, templateCutPreviewError, pendingTemplateId,
    templateCutController, templateCutCompletedRunToken, templateCutRunToken,
    templateReviewPendingPageIndexes, requireTemplateService,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const initializeTemplatePageSlots = (...args) => ctx.initializeTemplatePageSlots(...args)

  let templateCutPollTimer = null

  let templateCutPreviewLoadToken = 0

  function templateCutPageOriginalCount(pageIndex) {
    const page = (templateCutResult.value?.pages || []).find((item) => Number(item.page_index) === Number(pageIndex))
    const result = page?.modes?.question
    if (!result) return 0
    return (result.pages || []).reduce((total, providerPage) => total + (providerPage.candidates || [])
      .filter((candidate) => candidate.coordinate_trust === 'original').length, 0)
  }

  function stopTemplateCutPolling() {
    if (templateCutPollTimer) clearTimeout(templateCutPollTimer)
    templateCutPollTimer = null
  }

  function releaseFinishedTemplateCut(runToken) {
    if (runToken !== templateCutRunToken.value) return
    // The finished run is persisted server-side, so the pending POST no longer
    // owns the editor: holding the lock until it returns locks the teacher out of
    // a template that is already done.
    templateCutBusy.value = false
    stopTemplateCutPolling()
  }

  function applyTemplateCutState(data, expectedRunToken = templateCutRunToken.value) {
    if (!pendingTemplateId.value || expectedRunToken !== templateCutRunToken.value) return
    if (!data || (templateCutActionId.value && data.action_id && data.action_id !== templateCutActionId.value)) return
    const finished = ['complete', 'failed'].includes(data.status)
    if (templateCutCompletedRunToken.value === expectedRunToken) {
      releaseFinishedTemplateCut(expectedRunToken)
      return
    }
    if (data.progress) templateCutProgress.value = data.progress
    if (['scan', 'photo'].includes(data.image_type)) templateCutImageType.value = data.image_type
    if (data.status !== 'not_run') {
      templateCutResult.value = data
      const pendingPages = templateReviewPendingPageIndexes(data)
      templateCutPreviewPages.value.forEach((page) => {
        const pending = pendingPages.has(Number(page.pageIndex))
          || data.pages?.some((item) => Number(item.page_index) === Number(page.pageIndex) && templatePageReviewPending(item))
        if (pending) {
          page.layoutReviewPending = true
          page.layout_review_pending = true
          page.anchorReviewPending = true
          page.geometryReviewPending = true
          page.columnsConfirmed = false
        }
      })
      if (finished) {
        templateCutCompletedRunToken.value = expectedRunToken
        releaseFinishedTemplateCut(expectedRunToken)
      }
      if (data.status === 'failed') {
        templateCutPreviewError.value = templateCutErrorMessage(data, '模板自动裁剪失败，请重试。')
      }
      if (data.status === 'complete') void loadTemplateCutPreviews(data, expectedRunToken)
    }
  }

  function revokeTemplateCutPreviews() {
    templateCutPreviewLoadToken += 1
    templateCutPreviewPages.value.forEach((page) => {
      if (page.imageUrl) URL.revokeObjectURL(page.imageUrl)
    })
    templateCutPreviewPages.value = []
  }

  async function loadTemplateCutPreviews(data = templateCutResult.value, expectedRunToken = templateCutRunToken.value) {
    const templateId = pendingTemplateId.value
    if (!templateId || expectedRunToken !== templateCutRunToken.value || data?.status !== 'complete') return
    revokeTemplateCutPreviews()
    const loadToken = templateCutPreviewLoadToken
    const isCurrentLoad = () => templateId === pendingTemplateId.value
      && expectedRunToken === templateCutRunToken.value && loadToken === templateCutPreviewLoadToken
    const imageUrls = new Set()
    let failed = false
    templateCutPreviewError.value = ''
    try {
      const pendingPages = templateReviewPendingPageIndexes(data)
      const pages = await Promise.all((data.pages || []).map(async (page, index) => {
        // Normalize legacy responses in place so the established editor calls
        // keep reading the server-owned page fields.
        if (page.page_index === undefined || page.page_index === null) {
          page.page_index = index
        }
        const pageIndex = Number(page.page_index)
        const planColumns = cloneTemplateColumns(page.anchor_crop_plan?.parameters?.columns)
        const questionMode = page.modes?.question || {}
        // Real model runs also publish the detected layout under the question-mode
        // contract while the crop plan still carries no trusted columns. Show that
        // candidate so the teacher reviews the model's actual result instead of an
        // empty layout — but never promote it: only a saved teacher override
        // confirms a layout, so the candidate stays unconfirmed and under review.
        const candidateColumns = planColumns.length
          ? []
          : cloneTemplateColumns(questionMode?.column_layout?.columns)
        const columns = planColumns.length ? planColumns : candidateColumns
        const reviewPending = pendingPages.has(pageIndex) || templatePageReviewPending(page, questionMode)
        const layoutReviewPending = reviewPending
          || !columns.length
          || !planColumns.length
        const columnState = columns.length
          ? {
            columnsSource: planColumns.length ? 'model' : 'model_candidate',
            columnsConfirmed: !layoutReviewPending && planColumns.length > 0,
          }
          : { columnsSource: 'teacher_draft', columnsConfirmed: false }
        // No trusted columns means the teacher must explicitly choose a count.
        // Keeping this at zero avoids presenting the global two-column default
        // as though it were an already reviewed page layout.
        const columnCount = columns.length
          ? Number(page.anchor_crop_plan?.parameters?.column_count
            || questionMode?.column_layout?.column_count || columns.length)
          : 0
        const artifact = page.artifact || page.page_artifact || {}
        // Semantic/geometry review deliberately keeps the approved fields empty.
        // Prefer candidate fields only as the editor's initial draft; the single
        // 作答框 contract is promoted only after the teacher saves an override.
        const rawAnchors = reviewPending && Array.isArray(page.question_anchor_candidates) && page.question_anchor_candidates.length
          ? page.question_anchor_candidates
          : (Array.isArray(page.question_anchors) ? page.question_anchors : (artifact.question_anchors || []))
        const rawHandwritingRegions = reviewPending && templateRegionCandidateUsable(page.template_handwriting_region_candidates)
          ? page.template_handwriting_region_candidates
          : (Array.isArray(page.template_handwriting_regions) ? page.template_handwriting_regions : (artifact.template_handwriting_regions || []))
        const rawAnswerRegions = reviewPending && templateRegionCandidateUsable(page.template_answer_region_candidates)
          ? page.template_answer_region_candidates
          : (Array.isArray(page.template_answer_regions) ? page.template_answer_regions : (artifact.template_answer_regions || []))
        const mergedAnswerBoxes = mergeTemplateAnswerRegionSources(rawAnswerRegions, rawHandwritingRegions)
        const questionSchema = mergeTemplateDrawingCandidates(
          page.question_schema || artifact.question_schema || [],
          page.question_schema_candidates || [], reviewPending,
        )
        const imageUrl = await api.grade.templatePageImage(templateId, Number(page.page_index))
        if (failed || !isCurrentLoad()) {
          URL.revokeObjectURL(imageUrl)
          return null
        }
        imageUrls.add(imageUrl)
        return {
          pageLabel: page.page_label,
          pageIndex,
          // Do not manufacture equal partitions while reviewing a pending page.
          // The empty list is intentional and keeps the model result distinct
          // from the teacher's later draft in resetTemplateColumns().
          columns,
          // A candidate layout is displayed for review but is never counted as
          // this page's confirmed layout; zero keeps the upload default invisible.
          columnCount,
          ...columnState,
          layoutReviewPending,
          layout_review_pending: layoutReviewPending,
          anchorReviewPending: reviewPending,
          geometryReviewPending: reviewPending,
          questionAnchors: cloneQuestionAnchors(rawAnchors),
          questionSchema,
          ...templateDrawingReview(questionSchema, { includeConflictingGeometry: reviewPending }),
          hasAnswerRegionContract: mergedAnswerBoxes.length > 0,
          answerBoxes: mergedAnswerBoxes,
          // Compatibility alias for older code paths; both references represent
          // the same unified 作答框 collection and are never edited separately.
          handwritingBoxes: mergedAnswerBoxes,
          anchorDiagnostics: page.anchor_diagnostics || questionMode.anchor_diagnostics || null,
          templateOcrDiagnostics: page.template_ocr_diagnostics || questionMode.template_ocr_diagnostics || null,
          newHandwritingQuestion: String(rawAnchors?.[0]?.question_number || ''),
          editQuestionNumber: String(rawAnchors?.[0]?.question_number || ''),
          drawingQuestionNumber: String(questionSchema[0]?.question_number || ''),
          referenceQuestionNumber: '',
          previewEditMode: 'geometry',
          referenceImageReadyNumbers: {},
          insertedPlaceholder: Boolean(page.inserted_placeholder || artifact.inserted_placeholder
            || page.anchor_diagnostics?.review_reason === 'teacher_added_question_placeholder'),
          // Review is an unsaved teacher decision even when the teacher accepts
          // every visible candidate without dragging it first.
          dirty: (reviewPending || layoutReviewPending) && columns.length > 0,
          saving: false,
          imageUrl,
        }
      }))
      if (!isCurrentLoad()) {
        imageUrls.forEach((url) => URL.revokeObjectURL(url))
        return
      }
      for (const page of pages) initializeTemplatePageSlots(page)
      templateCutPreviewPages.value = pages
    } catch {
      failed = true
      imageUrls.forEach((url) => URL.revokeObjectURL(url))
      if (!isCurrentLoad()) return
      templateCutPreviewError.value = '裁剪模板读取失败，请重新打开模板或重试自动裁剪。'
    }
  }

  async function pollTemplateCutState(templateId, expectedRunToken = templateCutRunToken.value) {
    if (!templateCutBusy.value || templateId !== pendingTemplateId.value || expectedRunToken !== templateCutRunToken.value) return
    try {
      const state = await api.grade.templateCutCandidatesState(templateId)
      if (templateId !== pendingTemplateId.value || expectedRunToken !== templateCutRunToken.value) return
      applyTemplateCutState(state, expectedRunToken)
    } catch {
      // The POST request remains authoritative; transient polling errors stay quiet.
    } finally {
      if (templateCutBusy.value && templateId === pendingTemplateId.value && expectedRunToken === templateCutRunToken.value) {
        templateCutPollTimer = setTimeout(() => pollTemplateCutState(templateId, expectedRunToken), 800)
      }
    }
  }

  async function loadTemplateCutState(templateId, expectedRunToken = templateCutRunToken.value) {
    if (!templateId || templateId !== pendingTemplateId.value || expectedRunToken !== templateCutRunToken.value) return
    try {
      const state = await api.grade.templateCutCandidatesState(templateId)
      if (templateId !== pendingTemplateId.value || expectedRunToken !== templateCutRunToken.value) return
      if (state.status !== 'not_run') {
        templateCutResult.value = state
        templateCutProgress.value = state.progress || templateCutProgress.value
        if (['scan', 'photo'].includes(state.image_type)) templateCutImageType.value = state.image_type
        if (state.status === 'complete') void loadTemplateCutPreviews(state, expectedRunToken)
      }
    } catch {
      // Existing template editing remains available if optional candidate state cannot load.
    }
  }

  function templateCutErrorMessage(data, fallback) {
    const detail = data?.detail ?? data?.message ?? data?.error
    if (typeof detail === 'string') return detail
    if (detail && typeof detail.message === 'string') return detail.message
    return fallback
  }

  async function onRunTemplateCut({ force = false } = {}) {
    if (!requireTemplateService()) return
    const templateId = pendingTemplateId.value
    if (!templateId || templateCutBusy.value) return
    const runToken = ++templateCutRunToken.value
    templateCutCompletedRunToken.value = null
    templateCutController.value?.abort()
    templateCutPreviewLoadToken += 1
    templateCutPreviewError.value = ''
    if (force) templateCutActionId.value = ''
    if (!templateCutActionId.value) {
      templateCutActionId.value = globalThis.crypto?.randomUUID?.()
        || `template-cut-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
    }
    templateCutBusy.value = true
    templateCutProgress.value = {
      completed: 0,
      total: templatePages.value.length,
      remaining: templatePages.value.length,
    }
    templateCutController.value = new AbortController()
    stopTemplateCutPolling()
    templateCutPollTimer = setTimeout(() => pollTemplateCutState(templateId, runToken), 300)
    try {
      const response = await api.grade.templateCutCandidates(templateId, {
        action_id: templateCutActionId.value,
        cut_types: ['question'],
        image_type: templateCutImageType.value,
        force,
      }, templateCutController.value.signal)
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(templateCutErrorMessage(data, `裁题失败 (${response.status})`))
      if (runToken !== templateCutRunToken.value || templateId !== pendingTemplateId.value) return
      if (!['complete', 'failed'].includes(data.status)
        || (data.action_id && data.action_id !== templateCutActionId.value)) {
        throw new Error('模板裁剪结果不完整或不属于本次任务，请重试。')
      }
      applyTemplateCutState(data, runToken)
      templateCutActionId.value = ''
      if (data.status === 'complete' && templateCutResult.value?.status === 'complete') {
        ElMessage.success(`模板自动裁剪完成：已生成 ${data.pages?.reduce((total, page) => total + (page.anchor_crop_plan?.blocks?.length || 0), 0) || 0} 个连续裁剪块`)
      }
    } catch (err) {
      if (runToken !== templateCutRunToken.value || templateId !== pendingTemplateId.value) return
      if (templateCutCompletedRunToken.value === runToken) return
      if (err.name !== 'AbortError') notifyProcessingError(err, err.message || '教师模板裁题失败')
    } finally {
      if (runToken === templateCutRunToken.value) {
        templateCutBusy.value = false
        templateCutController.value = null
        stopTemplateCutPolling()
      }
    }
  }

  return {
    templateCutPollTimer, templateCutPreviewLoadToken, templateCutPageOriginalCount,
    stopTemplateCutPolling, releaseFinishedTemplateCut, applyTemplateCutState,
    revokeTemplateCutPreviews, loadTemplateCutPreviews, pollTemplateCutState, loadTemplateCutState,
    templateCutErrorMessage, onRunTemplateCut,
  }
}
