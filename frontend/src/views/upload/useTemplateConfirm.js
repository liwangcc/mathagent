/**
 * Confirming the template a batch will be graded against, and leaving or resetting the template editor.
 *
 * - onConfirmTemplate / saveAndConfirmTemplateDraft save the edited pages and confirm the draft once
 *   nothing blocks it (templateEditorProblems, templatePreSaveBlockers, warnTemplateSaveBlockers,
 *   removePendingTemplateQuestions).
 * - Correction candidates from earlier grading (loadTemplateCorrectionCandidates, toggleQuestionCorrection),
 *   opening the editor, importing the template into the question bank.
 * - clearTemplateDraft, cancelTemplateDialog and resetTemplate.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { ElMessage } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import {
  templateIsPureDrawing,
  templateChoiceConflictReason,
  templateMissingAnswerNotice,
  templateMaxFlagNotice,
  templateBoxSizeProblems,
  templateReferenceLeavesRegions,
  templateChoiceConflictReasons,
} from '@/utils/templateReview'
import { templateSlotBoxes } from '@/utils/templateSlots'
import { templatePageSaveBoxes } from '@/utils/templateGeometry'
import { updateDrawingReference, templateDrawingReferenceRegionsFor } from '@/utils/templateDrawing'

export function useTemplateConfirm(ctx) {
  const {
    recognizing, grading, templateMode, templateColumnMode, templateFiles, templatePreprocessing,
    recognizingTemplate, templateRecognizeMessage, templateRecognizeError, confirmedTemplateId,
    templatePageCount, templateDialogVisible, confirmingTemplate, importingTemplateBank,
    templateBankImported, templatePages, templateCorrectionCandidatesLoading, templateCutBusy,
    templateCutResult, templateCutProgress, templateCutActionId, templateCutImageType,
    templateCutPreviewPages, templateCutPreviewError, templateCropGesture,
    templateReferenceEditorInstances, templateQuestionEditing, templateChoiceScore,
    templateFillScore, pendingTemplateId, form, recognitionAutoArmed, templateRecognizeController,
    templateConfirmController, templateCutController, templateCutCompletedRunToken,
    templateCutRunToken, templateGeometryReview, templateCanConfirm, templateChoiceConflicts,
    templateQuestionSignature, templateDrawingReferenceSaveSnapshot,
    clearSavedTemplateDrawingReferenceChanges, templateDrawingReferenceQuestions,
    templateReferenceImagesReady, answerSpecFields, questionScoringValid, syncTemplateBatchScores,
    applyTemplateBatchScores, normalizeTemplateMode, normalizeTemplateColumnMode,
    forgetTemplateRecognition, stopTemplateCutPolling, revokeTemplateCutPreviews,
    templateLayoutConflictNotice, templateAnswerQuestion, templatePageNeedsSave,
    saveAllTemplateLayoutPages, loadTemplateCutState,
  } = ctx

  let templateCorrectionCandidatesGeneration = 0

  async function onConfirmTemplate() {
    if (confirmingTemplate.value || importingTemplateBank.value || templateQuestionEditing.value || templateCutBusy.value || !pendingTemplateId.value) return false
    if (templateCropGesture.value) { ElMessage.info('请先完成当前框选，再保存模板'); return false }
    const editorErrors = []
    for (const page of templateCutPreviewPages.value) {
      for (const question of templateDrawingReferenceQuestions(page)) {
        const editor = templateReferenceEditorInstances.get(`${page.pageIndex}:${String(question.question_no)}`)
        const error = editor?.prepareForSave?.()
        if (error) { editorErrors.push(`第 ${page.pageIndex + 1} 页第 ${question.question_no} 题：${error}`); continue }
        // Keep the confirmation payload aligned with the visible SVG draft even
        // when the final pointer/edit event and the save click share one tick.
        // A null value deliberately remains missing so the server-side drawing
        // reference gate is still authoritative.
        const visibleReference = editor?.valueForSave?.()
        if (visibleReference) updateDrawingReference(question, visibleReference)
      }
    }
    if (editorErrors.length) { warnTemplateSaveBlockers(editorErrors); return false }
    const templateId = pendingTemplateId.value
    confirmingTemplate.value = true
    try {
      return await saveAndConfirmTemplateDraft(templateId)
    } catch (err) {
      if (templateId === pendingTemplateId.value && err.name !== 'CanceledError' && err.name !== 'AbortError') {
        notifyProcessingError(err, err.message || '模板未保存，当前修改已保留，请重试')
      }
      return false
    } finally {
      if (templateId === pendingTemplateId.value) confirmingTemplate.value = false
    }
  }

  /**
   * Everything a save would reject that the Max, removal and missing-answer rows
   * do not already list: oversized boxes, standard figures outside their drawing
   * areas, missing question content and unresolved choice questions. Shown live
   * from the moment the editor opens, so the teacher sees every problem at once.
   */
  function templateEditorProblems() {
    const problems = []
    for (const page of templateCutPreviewPages.value) {
      const pageNumber = Number(page.pageIndex) + 1
      const { textBoxes, drawingBoxes } = templatePageSaveBoxes(page)
      const boxes = [...new Set([...textBoxes, ...drawingBoxes])]
        .map((box) => ({ box, kind: page.drawingBoxes.includes(box) ? 'drawing' : 'answer' }))
      for (const problem of templateBoxSizeProblems(boxes, pageNumber)) {
        const index = boxes.findIndex((item) => item.box === problem.box)
        problems.push({ ...problem, key: `size-${page.pageIndex}-${index}`, pageIndex: page.pageIndex })
      }
      // Kept positions still waiting for a box (待框选); the page save names them
      // the same way, but only after the earlier checks have passed.
      const slotBoxes = templateSlotBoxes(page)
      for (const item of page.questionSchema || []) {
        if (item._slotsCleared || !Array.isArray(item.slot_ids)) continue
        const number = String(item.question_number ?? item.question_no)
        if (templateAnswerQuestion(page, number)?._pendingRemoval) continue
        const bound = (id) => slotBoxes.find((box) => box.slot_id === id && String(box.question_number) === number)
        const unboxed = item.slot_ids.filter((id) => bound(id)?.bbox?.length !== 4)
        if (!unboxed.length) continue
        problems.push({
          key: `unboxed-${page.pageIndex}-${number}`, pageIndex: page.pageIndex, box: bound(unboxed[0]), locateSlot: true,
          message: `${unboxed.map((id) => `Q${id}`).join('、')} 还没有框选位置：请在“作答槽顺序”中点该编号后框出位置，或删除该作答位`,
        })
      }
      for (const question of templateDrawingReferenceQuestions(page)) {
        if (question._pendingRemoval) continue
        if (!templateReferenceLeavesRegions(question.drawing_reference, templateDrawingReferenceRegionsFor(page, question))) continue
        problems.push({
          key: `figure-${page.pageIndex}-${question.question_no}`, pageIndex: page.pageIndex, referenceNumber: String(question.question_no),
          message: `第 ${pageNumber} 页第 ${question.question_no} 题：标准图有笔画画在画图区外：请把笔画移回画图区内（可撤销后重画），或把画图区拉大到包住整个标准图`,
        })
      }
    }
    templatePages.value.forEach((page, position) => {
      (page.questions || []).forEach((question, questionIndex) => {
        if (question._pendingRemoval) return
        const number = String(question.question_no ?? '').trim()
        const location = `第 ${Number(page.page_index) + 1} 页${number ? `第 ${number} 题` : `第 ${questionIndex + 1} 个题目`}`
        const messages = []
        if (!number) messages.push(`${location}缺少题号`)
        if ((question.question_type !== 'choice' || templateIsPureDrawing(question)) && !String(question.question_text || '').trim()) messages.push(`${location}缺少题干，请在题目内容中补齐`)
        // Missing slot answers already have their own row.
        if (!templateIsPureDrawing(question) && !String(question.standard_answer || '').trim() && !templateMissingAnswerNotice(question)) messages.push(`${location}缺少标准答案，请在答案编辑区填写`)
        messages.push(...templateChoiceConflictReasons(question))
        messages.forEach((message, index) => problems.push({
          key: `question-${page.page_index}-${questionIndex}-${index}`, pageIndex: Number(page.page_index),
          entry: { pageIndex: position, questionIndex, question }, message,
        }))
      })
    })
    return problems
  }

  /** Every reason a save would stop before writing anything, in one pass. */
  function templatePreSaveBlockers() {
    const blockers = []
    for (const page of templatePages.value) {
      if (!page.questions?.length) { blockers.push(`第 ${Number(page.page_index) + 1} 页没有题目，请在该页添加题目`); continue }
      if (page.questions.every((question) => question._pendingRemoval)) {
        blockers.push(`第 ${Number(page.page_index) + 1} 页的题目框已全部删除；每页至少保留一道题，请为其中一题重新框出作答位置`)
        continue
      }
      for (const question of page.questions) {
        if (question._pendingRemoval) continue
        const maxFlagNotice = templateMaxFlagNotice(question)
        if (maxFlagNotice) blockers.push(maxFlagNotice)
        const missingNotice = templateMissingAnswerNotice(question)
        if (missingNotice) blockers.push(missingNotice)
      }
    }
    for (const page of templateCutPreviewPages.value) {
      if (!templatePageNeedsSave(page)) continue
      if (!Array.isArray(page.columns) || !page.columns.length) blockers.push(`第 ${Number(page.pageIndex) + 1} 页尚未设置栏数，请先选择并核对栏边界`)
      const conflictNotice = templateLayoutConflictNotice(page)
      if (conflictNotice) blockers.push(`第 ${Number(page.pageIndex) + 1} 页：${conflictNotice}`)
    }
    blockers.push(...templateEditorProblems().map((problem) => problem.message))
    return [...new Set(blockers)]
  }

  /** One message naming every blocker; the review rows at the top keep them visible. */
  function warnTemplateSaveBlockers(blockers) {
    if (blockers.length === 1) { ElMessage.warning(blockers[0]); return }
    ElMessage.warning({
      message: `保存前还有 ${blockers.length} 处需要处理（编辑器顶部已逐条列出）：${blockers.map((text, index) => `${index + 1}. ${text}`).join('；')}`,
      duration: 10000, showClose: true,
    })
  }

  /**
   * Questions whose every box the teacher deleted leave the template at save
   * time (题号随框消失). The server delete keeps all other numbers; only the
   * removed question leaves the local draft, so other unsaved edits survive.
   */
  async function removePendingTemplateQuestions(templateId) {
    for (const page of templatePages.value) {
      for (const question of [...(page.questions || [])]) {
        if (!question._pendingRemoval) continue
        const number = String(question.question_no)
        await api.grade.templateQuestionEdit(templateId, { page_index: page.page_index, action: 'delete', question_number: number })
        if (templateId !== pendingTemplateId.value) return false
        page.questions = page.questions.filter((item) => item !== question)
        const preview = templateCutPreviewPages.value.find((item) => Number(item.pageIndex) === Number(page.page_index))
        if (!preview) continue
        const other = (item) => String(item.question_number ?? item.question_no) !== number
        preview.questionAnchors = preview.questionAnchors.filter(other)
        preview.questionSchema = (preview.questionSchema || []).filter(other)
        for (const key of ['answerBoxes', 'handwritingBoxes', 'drawingBoxes', 'drawingLabels']) {
          preview[key] = (preview[key] || []).filter(other)
        }
        preview.dirty = true
      }
    }
    return true
  }

  async function saveAndConfirmTemplateDraft(templateId) {
    if (templateCutPreviewPages.value.some((page) => templateDrawingReferenceQuestions(page).length > 0 && !templateReferenceImagesReady(page))) {
      ElMessage.warning('作图模板原图尚未加载成功，请先完成图像核对再确认模板')
      return false
    }
    // Saving approves the visible draft. Late correction previews must not
    // change it while geometry is being persisted, or require another approval.
    templateCorrectionCandidatesGeneration += 1
    const blockers = templatePreSaveBlockers()
    if (blockers.length) { warnTemplateSaveBlockers(blockers); return false }
    if (!await removePendingTemplateQuestions(templateId)) return false
    if (!await saveAllTemplateLayoutPages()) return false
    if (templateId !== pendingTemplateId.value) return false
    try {
      applyTemplateBatchScores()
    } catch (err) {
      ElMessage.warning(err.message || '请输入大于 0 的统一分值')
      return false
    }
    if (!templateCanConfirm.value) {
      // Name every remaining category at once instead of one per save attempt.
      const geometryReview = templateGeometryReview.value
      const blockers = []
      if (geometryReview.layoutReviewPendingPages.length) blockers.push('部分页面的栏边界仍待核对，请调整后重新保存模板')
      if (geometryReview.missingQuestionNumbers.length) blockers.push(`请补齐缺失题号定位：${geometryReview.missingQuestionNumbers.join('、')}`)
      if (geometryReview.duplicateQuestionNumbers.length) blockers.push(`请处理重复题号定位：${geometryReview.duplicateQuestionNumbers.join('、')}`)
      if (geometryReview.orphanQuestionNumbers.length) blockers.push(`请处理未绑定到模板题目的题号定位：${geometryReview.orphanQuestionNumbers.join('、')}`)
      if (geometryReview.outOfBoundsHandwritingQuestions.length) blockers.push(`作答区域框越出页面范围：${geometryReview.outOfBoundsHandwritingQuestions.join('、')}，已自动收回页面内，请重试保存`)
      if (geometryReview.lowConfidenceQuestionNumbers.length || geometryReview.lowConfidenceHandwritingQuestions.length) {
        blockers.push(`低置信题目归属或作答区域需要人工复核：${[...geometryReview.lowConfidenceQuestionNumbers, ...geometryReview.lowConfidenceHandwritingQuestions].join('、')}`)
      }
      if (geometryReview.diagnosticReasons.length) {
        const detail = (geometryReview.diagnosticReviewDetails || []).slice(0, 3).join('；')
        blockers.push(`模型定位或逐图关联仍需人工复核，请核对后重新保存模板${detail ? `（${detail}）` : ''}`)
      }
      for (const { question } of templateChoiceConflicts.value) blockers.push(templateChoiceConflictReason(question))
      for (const page of templatePages.value) {
        for (const question of page.questions || []) {
          if (!questionScoringValid(question)) blockers.push(`第 ${Number(page.page_index) + 1} 页第 ${question.question_no} 题的题型或分值设置不完整，请检查该题的选项、填空数量及分值`)
        }
      }
      warnTemplateSaveBlockers(blockers.length ? blockers : ['当前模板页面信息不完整，修改已保留，请检查页面加载状态后重试保存模板'])
      return false
    }
    if (templateId !== pendingTemplateId.value) return false
    const payload = {
      template_id: templateId,
      category: form.category,
      exam_name: form.category === 'exam' ? form.exam_name.trim() : '',
      pages: templatePages.value.map((pg) => ({
        page_index: pg.page_index,
        questions: (pg.questions || []).map((q) => ({
          question_no: q.question_no,
          question_text: q.question_text || '',
          standard_answer: q.standard_answer || '',
          chapter: q.chapter || '',
          topic: q.topic || '',
          correction_excluded: q.correction_excluded === true,
          ...answerSpecFields(q),
          authorized_wrong_cycle_ids: q.authorized_wrong_cycle_ids || [],
          max_flags_kept: Array.isArray(q.max_flags_kept) ? [...q.max_flags_kept] : [],
        })),
      })),
    }
    const drawingReferenceSnapshot = templateDrawingReferenceSaveSnapshot()
    const controller = new AbortController()
    templateConfirmController.value = controller
    try {
      const res = await api.grade.templateConfirm(payload, controller.signal)
      if (controller.signal.aborted || templateId !== pendingTemplateId.value) return false
      if (!res.success) throw new Error(res.detail || '确认失败')
      clearSavedTemplateDrawingReferenceChanges(drawingReferenceSnapshot)
      // The template is now this batch's saved authority; drop the unfinished
      // draft checkpoint so a later page load does not reopen the editor.
      forgetTemplateRecognition()
      templateMode.value = normalizeTemplateMode(res.template_mode, templateMode.value)
      templateColumnMode.value = normalizeTemplateColumnMode(res.column_mode, templateColumnMode.value)
      confirmedTemplateId.value = templateId
      templatePageCount.value = res.page_count
      templateDialogVisible.value = false
      ElMessage.success(`本次模板已保存（${res.page_count} 页），开始识别学生试卷前仍可再次编辑`)
      return true
    } catch (err) {
      if (templateId === pendingTemplateId.value && err.name !== 'CanceledError' && err.name !== 'AbortError') notifyProcessingError(err, err.message || '模板确认失败，当前修改已保留')
      return false
    } finally {
      if (templateConfirmController.value === controller) templateConfirmController.value = null
    }
  }

  async function loadTemplateCorrectionCandidates() {
    if (!pendingTemplateId.value || confirmingTemplate.value || templateCorrectionCandidatesLoading.value) return false
    const generation = ++templateCorrectionCandidatesGeneration
    const templateId = pendingTemplateId.value
    const signature = templateQuestionSignature()
    templateCorrectionCandidatesLoading.value = true
    try {
      const res = await api.grade.templateCorrectionCandidates({
        template_id: templateId,
        pages: templatePages.value.map((page) => ({
          page_index: page.page_index,
          questions: (page.questions || []).map((q) => ({
            question_no: q.question_no,
            question_text: q.question_text || '',
            standard_answer: q.standard_answer || '',
            chapter: q.chapter || '',
            topic: q.topic || '',
            correction_excluded: q.correction_excluded === true,
            ...answerSpecFields(q),
          })),
        })),
      })
      if (generation !== templateCorrectionCandidatesGeneration || templateId !== pendingTemplateId.value
        || signature !== templateQuestionSignature() || confirmingTemplate.value) return false
      const byNo = new Map((res.candidates || []).map((item) => [String(item.question_no), item]))
      templatePages.value.forEach((page) => page.questions.forEach((q) => {
        const discovery = byNo.get(String(q.question_no))
        q.correction_candidates = discovery?.candidates || []
        q.correction_knowledge_summary = discovery?.knowledge_summary || null
        q.authorized_wrong_cycle_ids = q.correction_excluded
          ? []
          : q.correction_candidates.map((candidate) => candidate.cycle_id)
        if (q.correction_candidates.length) q._open = true
      }))
      return true
    } catch {
      return false
    } finally {
      templateCorrectionCandidatesLoading.value = false
    }
  }

  function toggleQuestionCorrection(question) {
    question.correction_excluded = !question.correction_excluded
    question.authorized_wrong_cycle_ids = question.correction_excluded
      ? []
      : (question.correction_candidates || []).map((candidate) => candidate.cycle_id)
  }

  function openTemplateEditor() {
    if (recognizing.value || grading.value) {
      ElMessage.warning('学生试卷已开始处理，本次模板已冻结')
      return
    }
    pendingTemplateId.value = confirmedTemplateId.value
    syncTemplateBatchScores()
    templateDialogVisible.value = true
    void loadTemplateCutState(pendingTemplateId.value)
    void loadTemplateCorrectionCandidates()
  }

  async function onImportTemplateBank() {
    if (confirmingTemplate.value || importingTemplateBank.value) return false
    if (templateBankImported.value) return false
    // Always save the latest editor contents before the one-time import.
    const saved = await onConfirmTemplate()
    if (!saved || !confirmedTemplateId.value) return
    importingTemplateBank.value = true
    try {
      const res = await api.grade.templateBankImport(confirmedTemplateId.value)
      if (!res.success) throw new Error(res.detail || '入库失败')
      templateBankImported.value = true
      ElMessage.success(res.already_imported
        ? '该模板已写入私库，本次未重复写入'
        : `已将 ${res.imported_count || 0} 道题按去重规则写入私库`)
    } catch (err) {
      notifyProcessingError(err, err.message || '模板入库失败')
    } finally {
      importingTemplateBank.value = false
    }
  }

  function clearTemplateDraft() {
    recognitionAutoArmed.value = true
    templateCutRunToken.value += 1
    templateCutCompletedRunToken.value = null
    templateRecognizeController.value?.abort()
    templateConfirmController.value?.abort()
    templateCutController.value?.abort()
    templateRecognizeController.value = null
    templateConfirmController.value = null
    templateCutController.value = null
    stopTemplateCutPolling()
    templateCutBusy.value = false
    recognizingTemplate.value = false
    confirmingTemplate.value = false
    importingTemplateBank.value = false
    templatePreprocessing.value = false
    revokeTemplateCutPreviews()
    forgetTemplateRecognition()
    confirmedTemplateId.value = ''
    pendingTemplateId.value = ''
    templatePages.value = []
    templateFiles.value = []
    templatePageCount.value = 0
    templateBankImported.value = false
    templateCutResult.value = null
    templateCutPreviewError.value = ''
    templateCutProgress.value = { completed: 0, total: 0, remaining: 0 }
    templateCutActionId.value = ''
    templateCutImageType.value = 'photo'
    templateRecognizeMessage.value = ''
    templateRecognizeError.value = ''
    templateMode.value = 'marked'
    templateColumnMode.value = 'double'
    templateChoiceScore.value = 5
    templateFillScore.value = 5
    templateDialogVisible.value = false
  }

  function cancelTemplateDialog() {
    if (confirmingTemplate.value || importingTemplateBank.value) return
    clearTemplateDraft()
    ElMessage.info('已取消本次模板识别，当前模板已清空')
  }

  function resetTemplate() {
    clearTemplateDraft()
  }

  return {
    templateCorrectionCandidatesGeneration, onConfirmTemplate, templateEditorProblems,
    templatePreSaveBlockers, warnTemplateSaveBlockers, removePendingTemplateQuestions,
    saveAndConfirmTemplateDraft, loadTemplateCorrectionCandidates, toggleQuestionCorrection,
    openTemplateEditor, onImportTemplateBank, clearTemplateDraft, cancelTemplateDialog,
    resetTemplate,
  }
}
