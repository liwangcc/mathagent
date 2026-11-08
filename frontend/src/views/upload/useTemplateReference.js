/**
 * The template editor's per-question panel: drawing references, answer specs and scores, and jumping to
 * the questions the review flags.
 *
 * - Drawing references: which questions have one, their editors and canvases, saving and clearing changes,
 *   and why a question cannot be edited while one is open.
 * - Answer spec fields and scores (answerSpecFields, questionScoringValid, syncTemplateBatchScores,
 *   applyTemplateBatchScores).
 * - Problem lists and navigation: missing answers, Max flags, pending removals (openTemplateProblem,
 *   openTemplateMaxFlags, openTemplateMissingAnswers, locateTemplateReviewSlot).
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { computed, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import {
  applyUniformTemplateScore,
  templateIsPureDrawing,
  normalizeChoiceAnswer,
  templateFillAnswerSlots,
  templateMissingAnswerSlots,
  templateMaxFlagEntries,
  templateRegionSlotsValid,
  templateReviewEntries,
  templateQuestionMetadata,
  templateDrawingReference,
  isTemplateDirectoryLeaf,
} from '@/utils/templateReview'
import { templateSlotBoxes } from '@/utils/templateSlots'
import { defaultGradingMethod, toStringList, isCustomFill } from '@/utils/answerSpecEditor'
import { templateDrawingReferenceSnapshot } from '@/utils/templateDrawing'

export function useTemplateReference(ctx) {
  const {
    templatePages, templateCutPreviewPages, templateCropGesture, templateActivePreviewPage,
    templateChoiceScore, templateFillScore, form, questions, templateChoiceReadyEntries,
    templateFillEntries,
  } = ctx
  // Defined by later parts of the page; looked up when called.
  const templatePageEditingLocked = (...args) => ctx.templatePageEditingLocked(...args)
  const selectTemplatePreviewPage = (...args) => ctx.selectTemplatePreviewPage(...args)
  const selectTemplateGeometryTarget = (...args) => ctx.selectTemplateGeometryTarget(...args)
  const clearTemplateSelection = (...args) => ctx.clearTemplateSelection(...args)
  const cancelTemplateGeometry = (...args) => ctx.cancelTemplateGeometry(...args)
  const templateEditorProblems = (...args) => ctx.templateEditorProblems(...args)

  function templateQuestionSignature(pages = templatePages.value) {
    return JSON.stringify((pages || []).map((page) => ({
      page_index: Number(page.page_index),
      questions: (page.questions || []).map((question) => ({
        question_no: String(question.question_no ?? question.question_number ?? '').trim(),
        question_text: String(question.question_text ?? question.content ?? '').trim(),
        question_type: String(question.question_type || '').toLowerCase(),
        choice_mode: String(question.choice_mode || '').toLowerCase(),
      })),
    })))
  }

  function templateReferenceQuestionEditBlockReason() {
    if (templatePages.value.some((page) => (page.questions || []).some((question) => question._drawingReferenceDirty))) {
      return '有尚未最终保存的作图参考修改，请先核对并最终确认模板保存，再重新打开增删题号；如不保留，请先取消本次模板草稿。'
    }
    return ''
  }

  function templateDrawingReferenceSaveSnapshot() {
    return new Map(templatePages.value.flatMap((page) => (page.questions || []).map((question) => [question, templateDrawingReferenceSnapshot(question)])))
  }

  function clearSavedTemplateDrawingReferenceChanges(snapshot) {
    for (const page of templatePages.value) {
      for (const question of page.questions || []) {
        if (snapshot.has(question) && snapshot.get(question) === templateDrawingReferenceSnapshot(question)) {
          question._drawingReferenceDirty = false
          if (question.drawing_reference) question.drawing_reference.teacher_confirmed = true
        }
      }
    }
  }

  function templateDrawingReferenceQuestions(page) {
    return (templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions || [])
      .filter((question) => ['drawing', 'mixed'].includes(question.answer_kind))
  }

  function templateReferenceEditors(page) {
    // 教师标注：每页至少保留一个编辑器宿主——没有作图题的页面也要能看到原图、
    // 调整题号/作答框/分栏；作图参考控件只在该页存在作图题（q 非空）时出现。
    const questions = templateDrawingReferenceQuestions(page)
    return questions.length ? questions : [null]
  }


  function templateReferenceCanvasVisible(page, question) {
    // 教师标注：整页只显示一张模板画布——几何模式跟随第一道作图题，
    // 参考编辑模式跟随当前选中的题目，避免逐题编辑器各带一张一模一样的模板。
    const questions = templateDrawingReferenceQuestions(page)
    if (!questions.length) return question === null
    const activeNumber = String(page.referenceQuestionNumber || '')
    const editingReference = page.previewEditMode === 'reference'
      && questions.some((item) => String(item.question_no) === activeNumber)
    if (!editingReference) return questions[0] === question
    return String(question.question_no) === activeNumber
  }

  function templateReferenceImagesReady(page) {
    return templateDrawingReferenceQuestions(page).every((question) => page.referenceImageReadyNumbers?.[String(question.question_no)])
  }

  function setTemplatePreviewEditMode(page, mode) {
    if (templatePageEditingLocked(page)) return
    if (templateCropGesture.value) cancelTemplateGeometry({ pointerId: templateCropGesture.value.pointerId })
    clearTemplateSelection(page)
    page.geometryTool = ''
    page.previewEditMode = mode
  }

  function selectTemplateDrawingReference(page, number) {
    if (templatePageEditingLocked(page)) return
    page.referenceQuestionNumber = String(number)
    // Set the active question before switching modes so the visible editor and
    // its interactive SVG are selected in the same render pass.
    setTemplatePreviewEditMode(page, 'reference')
  }

  function openTemplateDrawingReference(pageIndex, number) {
    const preview = templateCutPreviewPages.value.find((page) => Number(page.pageIndex) === Number(pageIndex))
    if (!preview) { ElMessage.warning('模板预览尚未加载，请稍后重试'); return }
    templateActivePreviewPage.value = Number(preview.pageIndex)
    selectTemplateDrawingReference(preview, number)
    document.getElementById(`template-reference-page-${Number(pageIndex)}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }


  function answerSpecFields(question) {
    const questionType = question.question_type || 'solve'
    const directoryLeaf = isTemplateDirectoryLeaf(question)
    const typedSlots = Object.hasOwn(question, 'region_kinds') || Object.hasOwn(question, 'slot_ids')
    const blankCount = typedSlots ? question.blank_count
      : questionType === 'fill' || directoryLeaf ? Math.min(20, Math.max(1, Number(question.blank_count) || 1)) : 1
    const subquestionScores = questionType === 'solve'
      ? (Array.isArray(question.subquestion_scores) ? question.subquestion_scores.map((value) => Math.max(0, Number(value) || 0)) : [])
      : []
    const typedGraphic = Array.isArray(question.region_kinds) && question.region_kinds.includes('drawing')
    const fillScoringMode = !typedGraphic && questionType === 'fill' && blankCount > 1 && form.category === 'exam'
      ? (['equal', 'all_or_nothing', 'custom'].includes(question.fill_scoring_mode) ? question.fill_scoring_mode : 'equal')
      : 'equal'
    const blankScores = fillScoringMode === 'custom'
      ? (Array.isArray(question.blank_scores) ? question.blank_scores.map((value) => Number(value)) : [])
      : []
    // A standard figure needs a drawing position. Region marks decide that, as
    // on the server; a stale figure on a non-drawing item would block saving.
    const graphic = Array.isArray(question.region_kinds) && question.region_kinds.length
      ? typedGraphic : ['drawing', 'mixed'].includes(question.answer_kind)
    const drawingReference = graphic ? templateDrawingReference(question) : {}
    return {
      ...templateQuestionMetadata(question),
      ...drawingReference,
      question_type: questionType,
      choice_mode: questionType === 'choice' && ['single', 'multiple'].includes(question.choice_mode)
        ? question.choice_mode : 'unknown',
      answer_variants: toStringList(question.answer_variants_text || question.answer_variants),
      solution_steps: toStringList(question.solution_steps_text || question.solution_steps),
      grading_method: question.grading_method || defaultGradingMethod(questionType),
      max_score: form.category === 'exam' && ['choice', 'fill'].includes(questionType)
        ? Number(question.max_score || 0)
        : null,
      blank_count: blankCount,
      answer_slots: typedSlots || questionType === 'fill' || directoryLeaf ? templateFillAnswerSlots(question) : [],
      fill_scoring_mode: fillScoringMode,
      blank_scores: blankScores,
      subquestion_count: questionType === 'solve' && !directoryLeaf
        ? Math.min(20, Math.max(1, Number(question.subquestion_count) || 1))
        : 1,
      subquestion_scores: form.category === 'exam' && questionType === 'solve'
        ? (directoryLeaf ? subquestionScores.slice(0, 1) : subquestionScores) : [],
    }
  }

  function questionScoringValid(question) {
    const questionType = question.question_type || 'solve'
    if (!templateRegionSlotsValid(question)) return false
    if (question.region_kinds?.includes('drawing')
      && ((question.fill_scoring_mode && question.fill_scoring_mode !== 'equal') || question.blank_scores?.length)) return false
    if (questionType === 'choice' && !templateIsPureDrawing(question)) {
      const answer = normalizeChoiceAnswer(question.standard_answer ?? question.answer)
      if (question.choice_mode === 'single' && answer.length !== 1) return false
      if (question.choice_mode === 'multiple' && answer.length < 2) return false
      if (!['single', 'multiple'].includes(question.choice_mode)) return false
    }
    if (questionType === 'fill') {
      const blankCount = Number(question.blank_count) || 1
      if (blankCount < 1 || blankCount > 20) return false
      if (!templateIsPureDrawing(question) && !Object.hasOwn(question, 'region_kinds') && blankCount > 1 && toStringList(question.answer_slots_text || question.answer_slots).length !== blankCount) return false
      if (form.category === 'exam' && question.fill_scoring_mode === 'custom') {
        const scores = Array.isArray(question.blank_scores) ? question.blank_scores.map((value) => Number(value)) : []
        if (scores.length !== blankCount || scores.some((value) => !Number.isFinite(value) || value <= 0)) return false
        if (Math.abs(scores.reduce((sum, value) => sum + value, 0) - Number(question.max_score || 0)) > 0.0001) return false
      }
    }
    if (questionType === 'solve' && form.category !== 'exam') {
      const count = Number(question.subquestion_count) || 0
      if (count < 1 || count > 20) return false
    }
    if (form.category !== 'exam') return true
    if (['choice', 'fill'].includes(questionType)) return Number(question.max_score) > 0
    return Array.isArray(question.subquestion_scores) && question.subquestion_scores.some((value) => Number(value) > 0)
  }

  function templateQuestionDomId(pageIndex, questionIndex) {
    return `template-question-${pageIndex}-${questionIndex}`
  }

  function syncTemplateBatchScores() {
    const firstChoice = templateChoiceReadyEntries.value.find(({ question }) => Number(question.max_score) > 0)
    const firstFill = templateFillEntries.value.find(({ question }) => !isCustomFill(question) && Number(question.max_score) > 0)
    templateChoiceScore.value = Number(firstChoice?.question.max_score) || 5
    templateFillScore.value = Number(firstFill?.question.max_score) || 5
  }

  function applyTemplateBatchScores() {
    if (form.category !== 'exam') return
    applyUniformTemplateScore(templatePages.value, 'choice', templateChoiceScore.value)
    applyUniformTemplateScore(templatePages.value, 'fill', templateFillScore.value)
  }

  function setTemplateDetailOpen(type, open) {
    templateReviewEntries(templatePages.value, type).forEach(({ question }) => { question._open = open })
  }

  function openTemplateQuestion(entry) {
    entry.question._open = true
    nextTick(() => document.getElementById(templateQuestionDomId(entry.pageIndex, entry.questionIndex))?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
  }

  const templateMissingAnswerEntries = computed(() => templatePages.value.flatMap((page, pageIndex) =>
    (page.questions || []).map((question, questionIndex) => ({ pageIndex, questionIndex, question }))
      .filter(({ question }) => !question._pendingRemoval && templateMissingAnswerSlots(question).length)))

  const templateMaxFlagReviewEntries = computed(() => templatePages.value.flatMap((page, pageIndex) =>
    (page.questions || []).map((question, questionIndex) => ({ pageIndex, questionIndex, question }))
      .filter(({ question }) => !question._pendingRemoval && templateMaxFlagEntries(question).length)))

  const templatePendingRemovalEntries = computed(() => templatePages.value.flatMap((page) =>
    (page.questions || []).filter((question) => question._pendingRemoval)
      .map((question) => ({ pageIndex: Number(page.page_index), question }))))

  const templateEditorProblemEntries = computed(() => templateEditorProblems())
  // Boxes named in a problem row are highlighted on the page like flagged slots.
  const templateProblemBoxes = computed(() => new Set(templateEditorProblemEntries.value.map((problem) => problem.box).filter(Boolean)))

  async function openTemplateProblem(problem) {
    if (problem.entry) { openTemplateQuestion(problem.entry); return }
    if (problem.referenceNumber) { openTemplateDrawingReference(problem.pageIndex, problem.referenceNumber); return }
    const page = templateCutPreviewPages.value.find(item => Number(item.pageIndex) === Number(problem.pageIndex))
    if (!page) return
    selectTemplatePreviewPage(page)
    if (problem.locateSlot && problem.box) locateTemplateReviewSlot(page, problem.box)
    else if (problem.box && !templatePageEditingLocked(page)) {
      page.previewEditMode = 'geometry'
      page.geometryOverlayHidden = false
      selectTemplateGeometryTarget(page, { kind: problem.kind, target: problem.box })
    }
    await nextTick()
    document.getElementById(`template-reference-page-${page.pageIndex}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  async function openTemplateMaxFlags(entry) {
    const pageIndex = Number(templatePages.value[entry.pageIndex]?.page_index ?? entry.pageIndex)
    const page = templateCutPreviewPages.value.find(item => Number(item.pageIndex) === pageIndex)
    const ids = templateMaxFlagEntries(entry.question).map(flag => flag.slot_id).filter(Boolean)
    const box = page && templateSlotBoxes(page).find(item => ids.includes(item.slot_id)
      && String(item.question_number) === String(entry.question.question_no))
    if (!page || !box) { openTemplateQuestion(entry); return }
    entry.question._open = true
    selectTemplatePreviewPage(page)
    locateTemplateReviewSlot(page, box)
    await nextTick()
    const root = document.getElementById(`template-reference-page-${pageIndex}`)
    const target = [...(root?.querySelectorAll('[data-template-slot-id]') || [])]
      .find(element => element.dataset.templateSlotId === box.slot_id)
      || [...(root?.querySelectorAll('[data-template-slot-row]') || [])]
        .find(element => element.dataset.templateSlotRow === box.slot_id)
    ;(target || root)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  function templateSlotNeedsAnswer(page, box) {
    const question = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions
      ?.find(item => String(item.question_no) === String(box.question_number))
    return !!question && templateMissingAnswerSlots(question).some(slot => slot.slotId === box.slot_id)
  }

  function templateSlotMaxFlagged(page, box) {
    const question = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions
      ?.find(item => String(item.question_no) === String(box.question_number))
    return !!question && templateMaxFlagEntries(question).some(flag => flag.slot_id === box.slot_id)
  }

  function locateTemplateReviewSlot(page, box) {
    if (templatePageEditingLocked(page)) return
    page.previewEditMode = 'geometry'
    page.geometryOverlayHidden = false
    selectTemplateGeometryTarget(page, { kind: box.region_kind === 'drawing' ? 'drawing' : 'answer', target: box })
    page.geometryTool = box.bbox.length === 4 ? '' : (box.region_kind || 'answer')
    page.pendingSlotId = box.bbox.length === 4 ? '' : box.slot_id
  }

  async function openTemplateMissingAnswers(entry) {
    const pageIndex = Number(templatePages.value[entry.pageIndex]?.page_index ?? entry.pageIndex)
    const page = templateCutPreviewPages.value.find(item => Number(item.pageIndex) === pageIndex)
    entry.question._open = true
    if (!page) { openTemplateQuestion(entry); return }
    const ids = templateMissingAnswerSlots(entry.question).map(slot => slot.slotId)
    const boxes = templateSlotBoxes(page).filter(box => ids.includes(box.slot_id)
      && String(box.question_number) === String(entry.question.question_no))
    selectTemplatePreviewPage(page)
    if (boxes[0]) locateTemplateReviewSlot(page, boxes[0])
    await nextTick()
    const root = document.getElementById(`template-reference-page-${pageIndex}`)
    const target = [...(root?.querySelectorAll('[data-template-slot-id]') || [])]
      .find(element => ids.includes(element.dataset.templateSlotId))
      || [...(root?.querySelectorAll('[data-template-slot-row]') || [])]
        .find(element => ids.includes(element.dataset.templateSlotRow))
    ;(target || root)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  return {
    templateQuestionSignature, templateReferenceQuestionEditBlockReason,
    templateDrawingReferenceSaveSnapshot, clearSavedTemplateDrawingReferenceChanges,
    templateDrawingReferenceQuestions, templateReferenceEditors, templateReferenceCanvasVisible,
    templateReferenceImagesReady, setTemplatePreviewEditMode, selectTemplateDrawingReference,
    openTemplateDrawingReference, answerSpecFields, questionScoringValid, templateQuestionDomId,
    syncTemplateBatchScores, applyTemplateBatchScores, setTemplateDetailOpen, openTemplateQuestion,
    templateMissingAnswerEntries, templateMaxFlagReviewEntries, templatePendingRemovalEntries,
    templateEditorProblemEntries, templateProblemBoxes, openTemplateProblem, openTemplateMaxFlags,
    templateSlotNeedsAnswer, templateSlotMaxFlagged, locateTemplateReviewSlot,
    openTemplateMissingAnswers,
  }
}
