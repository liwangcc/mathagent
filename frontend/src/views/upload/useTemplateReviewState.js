/**
 * What still needs the teacher's review on a recognized template before it can be confirmed.
 *
 * - templateGeometryReviewState / templateGeometryReview: geometry diagnostics and pending pages
 *   (templateReviewPendingPageIndexes, templateDiagnosticPendingPageIndexes,
 *   clearTemplateResolvedGeometryDiagnostics), the visual evidence pages, and templateCanConfirm.
 * - The choice / fill / solve entries and their counts shown in the review summary.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { computed } from 'vue'
import {
  choiceContractValid,
  templateIsPureDrawing,
  templateChoiceConflictEntries,
  templateChoiceReviewEntries,
  templateReviewEntries,
} from '@/utils/templateReview'
import {
  templatePageIndex,
  templateQuestionNumber,
  templateConfidenceRisk,
  templateDiagnosticHasPendingGeometryReview,
  templateDiagnosticReasonText,
  TEMPLATE_REVIEW_PENDING_PAGE_KEYS,
  templatePageReviewPending,
  templateResolveDiagnosticField,
  templateBBoxIsValid,
  templateRegionEntries,
  mergeTemplateAnswerRegionSources,
  templateRegionCandidateUsable,
} from '@/utils/templateGeometry'
import { isCustomFill } from '@/utils/answerSpecEditor'

export function useTemplateReviewState(ctx) {
  const {
    templatePages, templateCutResult, templateCutPreviewPages, form,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const templateDrawingReferenceQuestions = (...args) => ctx.templateDrawingReferenceQuestions(...args)
  const templateReferenceImagesReady = (...args) => ctx.templateReferenceImagesReady(...args)
  const questionScoringValid = (...args) => ctx.questionScoringValid(...args)

  function templateReviewPendingPageIndexes(data = templateCutResult.value) {
    const pending = new Set()
    TEMPLATE_REVIEW_PENDING_PAGE_KEYS.forEach((key) => {
      const pages = data?.[key]
      if (!Array.isArray(pages)) return
      pages.map(Number).forEach((value) => {
        if (Number.isInteger(value) && value >= 0) pending.add(value)
      })
    })
    return pending
  }

  function templateDiagnosticPendingPageIndexes() {
    // A page whose model diagnostics still carry a pending geometry review is an
    // unsaved teacher decision: the save click must reach the server (which
    // clears the marker) instead of skipping the page and re-reading stale state.
    const pending = new Set()
    templateVisualEvidencePages().forEach((page) => {
      if (page.diagnostics.some(templateDiagnosticHasPendingGeometryReview)) {
        pending.add(Number(page.pageIndex))
      }
    })
    return pending
  }

  function clearTemplateResolvedGeometryDiagnostics(pageIndex, anchors, handwriting) {
    const targets = []
    const addTarget = (target) => {
      if (target && typeof target === 'object' && !targets.includes(target)) targets.push(target)
    }
    ;(templatePages.value || []).forEach((page) => {
      if (Number(page.page_index ?? page.pageIndex) === Number(pageIndex)) addTarget(page)
    })
    ;(templateCutPreviewPages.value || []).forEach((page) => {
      if (Number(page.pageIndex ?? page.page_index) === Number(pageIndex)) addTarget(page)
    })
    ;(templateCutResult.value?.pages || []).forEach((page) => {
      if (Number(page.page_index ?? page.pageIndex) === Number(pageIndex)) addTarget(page)
    })
    targets.forEach((target) => {
      delete target.question_anchor_candidates
      delete target.template_handwriting_region_candidates
      delete target.layout_review_pending
      delete target.anchor_review_pending
      delete target.geometry_review_pending
      target.layoutReviewPending = false
      target.anchorReviewPending = false
      target.geometryReviewPending = false
      templateResolveDiagnosticField(target, 'anchor_diagnostics', anchors, handwriting)
      templateResolveDiagnosticField(target, 'template_ocr_diagnostics', anchors, handwriting)
      templateResolveDiagnosticField(target, 'anchorDiagnostics', anchors, handwriting)
      templateResolveDiagnosticField(target, 'templateOcrDiagnostics', anchors, handwriting)
      const questionMode = target.modes?.question
      if (questionMode && typeof questionMode === 'object') {
        delete questionMode.layout_review_pending
        delete questionMode.anchor_review_pending
        delete questionMode.geometry_review_pending
        templateResolveDiagnosticField(questionMode, 'anchor_diagnostics', anchors, handwriting)
        templateResolveDiagnosticField(questionMode, 'template_ocr_diagnostics', anchors, handwriting)
        templateResolveDiagnosticField(questionMode, 'anchorDiagnostics', anchors, handwriting)
        templateResolveDiagnosticField(questionMode, 'templateOcrDiagnostics', anchors, handwriting)
        templateResolveDiagnosticField(questionMode, 'diagnostics', anchors, handwriting)
      }
    })
  }

  function templateVisualEvidencePages() {
    const byIndex = new Map()
    const merge = (raw, patch = {}) => {
      const index = templatePageIndex(raw, patch.pageIndex ?? byIndex.size)
      const current = byIndex.get(index) || {
        pageIndex: index,
        questions: [],
        anchors: undefined,
        handwriting: undefined,
        columns: undefined,
        layoutReviewPending: false,
        columnsSource: '',
        columnsConfirmed: undefined,
        diagnostics: [],
        hasVisualEvidence: false,
      }
      Object.assign(current, patch)
      byIndex.set(index, current)
      return current
    }

    ;(templatePages.value || []).forEach((page, index) => {
      const item = merge(page, { pageIndex: templatePageIndex(page, index) })
      item.questions = Array.isArray(page.questions) ? page.questions : []
      const reviewPending = templatePageReviewPending(page)
      if (reviewPending && Array.isArray(page.question_anchor_candidates) && page.question_anchor_candidates.length) item.anchors = page.question_anchor_candidates
      else if (Array.isArray(page.question_anchors)) item.anchors = page.question_anchors
      const answerRegions = reviewPending && templateRegionCandidateUsable(page.template_answer_region_candidates)
        ? page.template_answer_region_candidates : page.template_answer_regions
      const handwritingRegions = reviewPending && templateRegionCandidateUsable(page.template_handwriting_region_candidates)
        ? page.template_handwriting_region_candidates : page.template_handwriting_regions
      const mergedRegions = mergeTemplateAnswerRegionSources(answerRegions, handwritingRegions)
      if (mergedRegions.length) item.handwriting = mergedRegions
      item.layoutReviewPending = item.layoutReviewPending || templatePageReviewPending(page)
      if (page.anchor_diagnostics) item.diagnostics.push(page.anchor_diagnostics)
      if (page.template_ocr_diagnostics) item.diagnostics.push(page.template_ocr_diagnostics)
    })

    ;(templateCutResult.value?.pages || []).forEach((page, index) => {
      const artifact = page?.artifact || page?.page_artifact || {}
      const questionMode = page?.modes?.question || {}
      const columns = page?.anchor_crop_plan?.parameters?.columns
        ?? questionMode?.column_layout?.columns
      const item = merge(page, { pageIndex: templatePageIndex(page, index) })
      const reviewPending = templatePageReviewPending(page, questionMode)
      if (reviewPending && Array.isArray(page?.question_anchor_candidates) && page.question_anchor_candidates.length) item.anchors = page.question_anchor_candidates
      else if (Array.isArray(page?.question_anchors)) item.anchors = page.question_anchors
      else if (Array.isArray(artifact?.question_anchors)) item.anchors = artifact.question_anchors
      const answerRegions = reviewPending && templateRegionCandidateUsable(page?.template_answer_region_candidates)
        ? page.template_answer_region_candidates
        : (Array.isArray(page?.template_answer_regions) ? page.template_answer_regions : artifact?.template_answer_regions)
      const handwritingRegions = reviewPending && templateRegionCandidateUsable(page?.template_handwriting_region_candidates)
        ? page.template_handwriting_region_candidates
        : (Array.isArray(page?.template_handwriting_regions) ? page.template_handwriting_regions : artifact?.template_handwriting_regions)
      const mergedRegions = mergeTemplateAnswerRegionSources(answerRegions, handwritingRegions)
      if (mergedRegions.length) item.handwriting = mergedRegions
      if (Array.isArray(columns)) {
        item.columns = columns
        item.columnCount = Number(page?.anchor_crop_plan?.parameters?.column_count
          ?? questionMode?.column_layout?.column_count ?? columns.length)
        item.hasVisualEvidence = true
      }
      item.layoutReviewPending = item.layoutReviewPending || templatePageReviewPending(page, questionMode)
      item.diagnostics.push(
        page?.anchor_diagnostics,
        page?.template_ocr_diagnostics,
        questionMode?.anchor_diagnostics,
        questionMode?.diagnostics,
      )
    })

    ;(templateCutPreviewPages.value || []).forEach((page, index) => {
      const item = merge(page, { pageIndex: templatePageIndex(page, index) })
      if (Array.isArray(page.questionAnchors)) item.anchors = page.questionAnchors
      if (Array.isArray(page.answerBoxes)) item.handwriting = page.answerBoxes
      if (Array.isArray(page.columns)) {
        item.columns = page.columns
        item.columnCount = Number(page.columnCount ?? page.columns.length)
        item.hasVisualEvidence = page.columns.length > 0
      }
      item.layoutReviewPending = item.layoutReviewPending
        || page.layoutReviewPending === true
        || page.layout_review_pending === true
      item.columnsSource = page.columnsSource || item.columnsSource
      if (page.columnsConfirmed !== undefined) item.columnsConfirmed = page.columnsConfirmed
      item.diagnostics.push(page.anchorDiagnostics, page.templateOcrDiagnostics)
    })

    const pendingPages = templateReviewPendingPageIndexes()
    byIndex.forEach((page) => {
      if (pendingPages.has(page.pageIndex)) page.layoutReviewPending = true
      page.diagnostics = page.diagnostics.filter(Boolean)
    })
    return [...byIndex.values()].sort((left, right) => left.pageIndex - right.pageIndex)
  }

  function templateGeometryReviewState() {
    const pages = templateVisualEvidencePages()
    const expectedOccurrences = new Map()
    const actualAnchorOccurrences = new Map()
    const missingQuestionNumbers = []
    const duplicateQuestionNumbers = []
    const orphanQuestionNumbers = []
    const outOfBoundsQuestionNumbers = []
    const outOfBoundsHandwritingQuestions = []
    const lowConfidenceQuestionNumbers = []
    const lowConfidenceHandwritingQuestions = []
    const invalidColumnPages = []
    const diagnosticReasons = []
    const diagnosticReviewDetails = []
    const pendingPages = templateReviewPendingPageIndexes()
    const addUnique = (list, value) => {
      const normalized = String(value ?? '').trim()
      if (normalized && !list.includes(normalized)) list.push(normalized)
    }
    const addPage = (list, value) => {
      const normalized = Number(value)
      if (Number.isInteger(normalized) && normalized >= 0 && !list.includes(normalized)) list.push(normalized)
    }

    pages.forEach((page) => {
      const pageIndex = page.pageIndex
      const expected = Array.isArray(page.questions)
        ? page.questions.map((question) => templateQuestionNumber(question?.question_no ?? question?.question_number)).filter(Boolean)
        : []
      expected.forEach((number) => {
        const entries = expectedOccurrences.get(number) || []
        entries.push(pageIndex)
        expectedOccurrences.set(number, entries)
      })

      if (page.layoutReviewPending || page.columnsSource === 'teacher_draft' || page.columnsConfirmed === false) {
        pendingPages.add(pageIndex)
      }
      if (!Array.isArray(page.columns) || !page.columns.length) {
        // A missing plan is a review state, never an implicit equal-column result.
        pendingPages.add(pageIndex)
      } else {
        // The teacher may correct one page independently of the upload default,
        // including deleting a divider. Validate its own saved/draft count.
        const expectedColumnCount = Number(page.columnCount ?? page.columns.length)
        if (!Number.isInteger(expectedColumnCount) || expectedColumnCount < 1 || expectedColumnCount > 3
          || page.columns.length !== expectedColumnCount) addPage(invalidColumnPages, pageIndex)
        page.columns.forEach((column) => {
          if (!templateBBoxIsValid([
            Number(column?.x1_1000), 0, Number(column?.x2_1000), 1000,
          ])) addPage(invalidColumnPages, pageIndex)
        })
      }

      const anchors = Array.isArray(page.anchors) ? page.anchors : []
      anchors.forEach((anchor) => {
        const number = templateQuestionNumber(anchor?.question_number ?? anchor?.question_no)
        if (!number) return
        const occurrences = actualAnchorOccurrences.get(number) || []
        occurrences.push(pageIndex)
        actualAnchorOccurrences.set(number, occurrences)
        if (templateConfidenceRisk(anchor)) addUnique(lowConfidenceQuestionNumbers, number)
      })

      const expectedSet = new Set(expected)
      anchors.forEach((anchor) => {
        const number = templateQuestionNumber(anchor?.question_number ?? anchor?.question_no)
        if (number && !expectedSet.has(number)) addUnique(orphanQuestionNumbers, number)
      })
      const regions = templateRegionEntries(page.handwriting)
      regions.forEach(({ item, box, region }) => {
        const number = templateQuestionNumber(item?.question_number ?? region?.question_number)
        // Question numbers are ownership keys, not geometry.  Only compare them
        // when this page actually has a question list; otherwise a cut-preview
        // page without copied question metadata would falsely look out of bounds.
        if (number && expected.length && !expectedSet.has(number)) addUnique(orphanQuestionNumbers, number)
        if (!templateBBoxIsValid(box)) {
          if (number) addUnique(outOfBoundsHandwritingQuestions, number)
          return
        }
        const provenance = { ...item, ...(region || {}) }
        if (templateConfidenceRisk(provenance)) addUnique(lowConfidenceHandwritingQuestions, number || `P${pageIndex + 1}`)
      })
      page.diagnostics.forEach((diagnostic) => {
        // Directory-stage diagnostics also cover answer/reference failures.  An
        // explicit geometry result must not keep a successfully retried reference
        // behind the geometry gate; legacy diagnostics remain fail-closed.
        if (templateDiagnosticHasPendingGeometryReview(diagnostic)) {
          const reasonText = templateDiagnosticReasonText(diagnostic)
          addUnique(diagnosticReasons, reasonText)
          addUnique(diagnosticReviewDetails, `第${pageIndex + 1}页：${reasonText.slice(0, 48)}`)
        }
        const imageAssociation = diagnostic?.image_association
        if (imageAssociation?.needs_review === true || imageAssociation?.status === 'needs_review') {
          addUnique(diagnosticReasons, 'image_association')
          addUnique(diagnosticReviewDetails, `第${pageIndex + 1}页：image_association`)
        }
      })
    })

    expectedOccurrences.forEach((occurrences, number) => {
      if (occurrences.length > 1) addUnique(duplicateQuestionNumbers, number)
      if (!actualAnchorOccurrences.has(number)) addUnique(missingQuestionNumbers, number)
    })
    actualAnchorOccurrences.forEach((occurrences, number) => {
      if (occurrences.length > 1) addUnique(duplicateQuestionNumbers, number)
    })

    if (!templateCutResult.value && !templateCutPreviewPages.value.length) {
      pages.forEach((page) => pendingPages.add(page.pageIndex))
    }
    invalidColumnPages.forEach((pageIndex) => pendingPages.add(pageIndex))
    const summaryParts = []
    if (pendingPages.size) summaryParts.push(`栏边界 ${[...pendingPages].sort((a, b) => a - b).map((value) => `第${value + 1}页`).join('、')}`)
    if (missingQuestionNumbers.length) summaryParts.push(`缺失题号 ${missingQuestionNumbers.join('、')}`)
    if (duplicateQuestionNumbers.length) summaryParts.push(`重复题号 ${duplicateQuestionNumbers.join('、')}`)
    if (orphanQuestionNumbers.length) summaryParts.push(`未绑定题号 ${orphanQuestionNumbers.join('、')}`)
    if (outOfBoundsHandwritingQuestions.length) summaryParts.push('作答区域框越界')
    if (lowConfidenceQuestionNumbers.length) summaryParts.push('低置信题目归属')
    if (lowConfidenceHandwritingQuestions.length) summaryParts.push('低置信作答区域')
    if (diagnosticReasons.length) {
      const detail = diagnosticReviewDetails.slice(0, 3).join('；')
      summaryParts.push(detail ? `模型诊断待复核（${detail}）` : '模型诊断待复核')
    }
    return {
      needsReview: Boolean(
        pendingPages.size
        || missingQuestionNumbers.length
        || duplicateQuestionNumbers.length
        || orphanQuestionNumbers.length
        || outOfBoundsQuestionNumbers.length
        || outOfBoundsHandwritingQuestions.length
        || lowConfidenceQuestionNumbers.length
        || lowConfidenceHandwritingQuestions.length
        || diagnosticReasons.length,
      ),
      layoutReviewPendingPages: [...pendingPages].sort((a, b) => a - b),
      missingQuestionNumbers,
      duplicateQuestionNumbers,
      orphanQuestionNumbers,
      outOfBoundsQuestionNumbers,
      outOfBoundsHandwritingQuestions,
      lowConfidenceQuestionNumbers,
      lowConfidenceHandwritingQuestions,
      invalidColumnPages,
      diagnosticReasons: [...new Set(diagnosticReasons)],
      diagnosticReviewDetails: [...new Set(diagnosticReviewDetails)],
      summary: summaryParts.join('；'),
    }
  }

  const templateGeometryReview = computed(templateGeometryReviewState)
  const templateCanConfirm = computed(() => {
    // Keep the server's explicit page list in the gate as well as the merged
    // review helper.  This makes a newly polled result fail closed immediately,
    // before the preview-page merge has had a chance to run.
    const serverPendingPages = templateReviewPendingPageIndexes()
    return !!form.category && templatePages.value.length > 0
      && !templateCutPreviewPages.value.some((page) => templateDrawingReferenceQuestions(page).length > 0 && !templateReferenceImagesReady(page))
      && templateChoiceConflictEntries(templatePages.value).length === 0
       && serverPendingPages.size === 0
      && !templateGeometryReview.value.needsReview
      && templatePages.value.every((page) =>
        (page.questions || []).length > 0 && page.questions.every((q) =>
          String(q.question_no ?? '').trim() && ((q.question_type === 'choice' && !templateIsPureDrawing(q)) || (q.question_text || '').trim()) && (templateIsPureDrawing(q) || (q.standard_answer || '').trim()) && questionScoringValid(q)
        )
      )
  })
  const templateChoiceEntries = computed(() => templateReviewEntries(templatePages.value, 'choice'))
  const templateChoiceReadyEntries = computed(() => templateChoiceReviewEntries(templatePages.value))
  const templateChoiceConflicts = computed(() => templateChoiceConflictEntries(templatePages.value))
  const templateChoiceSingleCount = computed(() => templateChoiceEntries.value.filter(({ question }) => question.choice_mode === 'single').length)
  const templateChoiceMultipleCount = computed(() => templateChoiceEntries.value.filter(({ question }) => question.choice_mode === 'multiple').length)
  const templateChoicePendingCount = computed(() => templateChoiceEntries.value.filter(({ question }) => !choiceContractValid(question)).length)
  const templateFillEntries = computed(() => templateReviewEntries(templatePages.value, 'fill'))
  const templateFillBatchEntryCount = computed(() => templateFillEntries.value.filter(({ question }) => !isCustomFill(question)).length)
  const templateCustomFillCount = computed(() => templateFillEntries.value.length - templateFillBatchEntryCount.value)
  const templateSolveEntries = computed(() => templateReviewEntries(templatePages.value, 'solve'))

  return {
    templateReviewPendingPageIndexes, templateDiagnosticPendingPageIndexes,
    clearTemplateResolvedGeometryDiagnostics, templateVisualEvidencePages,
    templateGeometryReviewState, templateGeometryReview, templateCanConfirm, templateChoiceEntries,
    templateChoiceReadyEntries, templateChoiceConflicts, templateChoiceSingleCount,
    templateChoiceMultipleCount, templateChoicePendingCount, templateFillEntries,
    templateFillBatchEntryCount, templateCustomFillCount, templateSolveEntries,
  }
}
