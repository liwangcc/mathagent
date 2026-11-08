/**
 * Saving the template editor's pages and editing its question list.
 *
 * - templatePageNeedsSave, saveAllTemplateLayoutPages and saveTemplateLayoutPage send the edited layout
 *   (columns, boxes, slots) of each page to the server.
 * - editTemplateQuestion, insertTemplateQuestion, deleteTemplateQuestion and applyTemplateQuestionEdit
 *   change a page's questions.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import api from '@/api'
import { templateQuestionMetadata, serializeTemplateDrawingReview } from '@/utils/templateReview'
import { serializeTemplateSlots } from '@/utils/templateSlots'
import {
  TEMPLATE_REVIEW_PENDING_PAGE_KEYS,
  cloneTemplateColumns,
  markTemplateGeometryConfirmed,
  cloneQuestionAnchors,
  flattenTemplateHandwritingRegions,
  mergeTemplateAnswerRegionSources,
  serializeTemplateHandwritingRegions,
  templatePageSaveBoxes,
} from '@/utils/templateGeometry'
import { normalizeAnswerSpecForEditor } from '@/utils/answerSpecEditor'
import { applyTemplateDrawingSchema } from '@/utils/templateDrawing'

export function useTemplateLayout(ctx) {
  const {
    templateMode, templatePages, templateCutResult, templateCutPreviewPages, templateCropGesture,
    templateQuestionEditing, pendingTemplateId, questions, templateReviewPendingPageIndexes,
    templateDiagnosticPendingPageIndexes, clearTemplateResolvedGeometryDiagnostics,
    templateReferenceQuestionEditBlockReason, normalizeTemplateMode, templateLayoutConflictNotice,
    initializeTemplatePageSlots, templatePageEditingLocked, clearTemplateSelection,
  } = ctx

  function templatePageNeedsSave(page) {
    return Boolean(page && (page.dirty || page.layoutReviewPending || page.anchorReviewPending
      || page.geometryReviewPending || page.layout_review_pending || page.anchor_review_pending
      || page.geometry_review_pending || page.columnsSource === 'teacher_draft'
      || page.columnsConfirmed === false || templateReviewPendingPageIndexes().has(Number(page.pageIndex))
      || templateDiagnosticPendingPageIndexes().has(Number(page.pageIndex))))
  }

  async function saveAllTemplateLayoutPages() {
    const templateId = pendingTemplateId.value
    if (!templateId) return false
    const pages = [...templateCutPreviewPages.value]
    if (pages.some((page) => page.saving)) {
      ElMessage.warning('模板页面仍在保存或生成标准图，请完成后重试')
      return false
    }
    // Page writes share one server-owned template session. Keep them ordered so
    // a failed page never races with final confirmation or loses another draft.
    for (const page of pages) {
      if (templateId !== pendingTemplateId.value || !templateCutPreviewPages.value.includes(page)) return false
      if (templatePageNeedsSave(page) && !await saveTemplateLayoutPage(page, { silent: true })) return false
    }
    return templateId === pendingTemplateId.value
  }

  async function saveTemplateLayoutPage(page, { silent = false } = {}) {
    if (!pendingTemplateId.value || page.saving || !templateCutPreviewPages.value.includes(page)) return false
    const templateId = pendingTemplateId.value
    const pageIndex = Number(page.pageIndex)
    const isCurrentPage = () => templateId === pendingTemplateId.value && templateCutPreviewPages.value.includes(page)
    if (!Array.isArray(page.columns) || !page.columns.length) {
      ElMessage.warning(`第 ${pageIndex + 1} 页尚未设置栏数，请先选择并核对栏边界`)
      return false
    }
    const conflictNotice = templateLayoutConflictNotice(page)
    if (conflictNotice) {
      ElMessage.warning(conflictNotice)
      return false
    }
    page.saving = true
    try {
      initializeTemplatePageSlots(page)
      if (page.slotContractError) throw new Error(page.slotContractError)
      const slotQuestions = templatePages.value.find(item => Number(item.page_index) === pageIndex)?.questions || []
      // Saving this page is the teacher's explicit acknowledgement of every
      // visible geometry item.  Send that provenance to the server so a low
      // confidence model candidate is promoted only after this action succeeds.
      const confirmedAnchors = cloneQuestionAnchors(page.questionAnchors, { teacherConfirmed: true })
      const { textBoxes, drawingBoxes } = templatePageSaveBoxes(page)
      const confirmedAnswerRegions = serializeTemplateHandwritingRegions(textBoxes, page.questionAnchors, { teacherConfirmed: true })
      const confirmedHandwriting = serializeTemplateHandwritingRegions([...textBoxes, ...drawingBoxes], page.questionAnchors, { teacherConfirmed: true })
      const confirmedSchema = serializeTemplateSlots(page, slotQuestions, serializeTemplateDrawingReview(page.questionSchema, page.drawingBoxes, page.drawingLabels))
      const blankTemplate = normalizeTemplateMode(templateMode.value, 'marked') === 'blank'
      const res = await api.grade.templateLayoutOverrides(templateId, {
        page_index: page.pageIndex,
        columns: cloneTemplateColumns(page.columns),
        question_anchors: cloneQuestionAnchors(page.questionAnchors, { teacherConfirmed: true }),
        question_schema: confirmedSchema,
        // The editor exposes one 作答框 collection. Keep the existing wire
        // contract until the model/prompt compatibility pass is accepted.
        template_answer_regions: blankTemplate || page.hasAnswerRegionContract ? confirmedAnswerRegions : undefined,
        template_handwriting_regions: confirmedHandwriting,
      })
      if (!isCurrentPage()) return false
      if (res?.success !== true || Number(res.page_index) !== pageIndex
        || (res.template_id != null && String(res.template_id) !== String(templateId))
        || !Array.isArray(res.columns) || !res.columns.length) {
        throw new Error('定位保存返回未绑定当前模板页面，未覆盖本页修改')
      }
      page.columns = cloneTemplateColumns(res.columns)
      page.columnCount = page.columns.length
      page.columnsSource = 'teacher'
      page.columnsConfirmed = true
      page.layoutReviewPending = false
      page.layout_review_pending = false
      page.anchorReviewPending = false
      page.geometryReviewPending = false
      page.anchor_review_pending = false
      page.geometry_review_pending = false
      page.questionAnchors = cloneQuestionAnchors(res.question_anchors || confirmedAnchors, { teacherConfirmed: true })
      applyTemplateDrawingSchema(page, res.question_schema || confirmedSchema)
      const answerPage = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))
      for (const question of answerPage?.questions || []) {
        const schema = page.questionSchema.find((item) => String(item.question_number ?? item.question_no) === String(question.question_no))
        // Actual geometry edits invalidate reference approval at edit time. Do
        // not undo a teacher's subsequent review just because it is persisted.
        if (schema) {
          Object.assign(question, templateQuestionMetadata(schema))
          // Drawing geometry and reference mode are independent of answer type;
          // saving a choice/fill drawing must not rewrite the teacher's contract.
        }
      }
      const savedAnswerRegions = res.template_answer_regions
        ?? (blankTemplate || page.hasAnswerRegionContract ? confirmedAnswerRegions : [])
      page.hasAnswerRegionContract = savedAnswerRegions.length > 0
      page.answerBoxes = flattenTemplateHandwritingRegions(savedAnswerRegions).map(markTemplateGeometryConfirmed)
      page.handwritingBoxes = flattenTemplateHandwritingRegions(
        res.template_handwriting_regions || confirmedHandwriting,
      ).map(markTemplateGeometryConfirmed)
      initializeTemplatePageSlots(page)
      if (page.slotContractError) throw new Error(page.slotContractError)
      clearTemplateResolvedGeometryDiagnostics(
        page.pageIndex,
        page.questionAnchors,
        page.handwritingBoxes,
      )
      const questionNumbers = page.questionAnchors.map((anchor) => String(anchor.question_number))
      if (!questionNumbers.includes(String(page.newHandwritingQuestion))) {
        page.newHandwritingQuestion = questionNumbers[0] || ''
      }
      if (templateCutResult.value) {
        TEMPLATE_REVIEW_PENDING_PAGE_KEYS.forEach((key) => {
          if (!Array.isArray(templateCutResult.value[key])) return
          templateCutResult.value[key] = templateCutResult.value[key]
            .map(Number)
            .filter((value) => value !== Number(page.pageIndex))
        })
        const resultPage = (templateCutResult.value.pages || []).find(
          (item) => Number(item.page_index) === Number(page.pageIndex),
        )
        if (resultPage) {
          delete resultPage.layout_review_pending
          delete resultPage.anchor_review_pending
          delete resultPage.geometry_review_pending
          delete resultPage.question_anchor_candidates
          delete resultPage.template_handwriting_region_candidates
          delete resultPage.template_answer_region_candidates
          delete resultPage.question_schema_candidates
          if (resultPage.modes?.question) {
            delete resultPage.modes.question.layout_review_pending
            delete resultPage.modes.question.anchor_review_pending
            delete resultPage.modes.question.geometry_review_pending
            resultPage.modes.question.layout_status = 'ready'
            resultPage.modes.question.anchor_status = 'ready'
          }
          resultPage.anchor_crop_plan = res.question_crop_plan || resultPage.anchor_crop_plan
          resultPage.question_anchors = page.questionAnchors
          resultPage.question_schema = page.questionSchema
          resultPage.template_answer_regions = savedAnswerRegions
          resultPage.template_handwriting_regions = res.template_handwriting_regions || confirmedHandwriting
        }
      }
      page.dirty = false
      page.insertedPlaceholder = false
      if (!silent) ElMessage.success(`第 ${page.pageIndex + 1} 页分栏与作答区域已保存`)
      return true
    } catch (err) {
      if (isCurrentPage()) notifyProcessingError(err, `第 ${pageIndex + 1} 页定位未保存：${err.message || '请重试'}，本页修改已保留`)
      return false
    } finally {
      page.saving = false
    }
  }

  async function editTemplateQuestion(page, payload, successText) {
    if (!pendingTemplateId.value || templatePageEditingLocked(page) || templateCropGesture.value) return
    const blocked = templateReferenceQuestionEditBlockReason()
    if (blocked) { ElMessage.warning(blocked); return }
    const templateId = pendingTemplateId.value
    templateQuestionEditing.value = true
    try {
      // Renumbering rewrites every page artifact. Flush geometry first so it
      // cannot replace a teacher's unsaved boxes with an old server snapshot.
      if (!await saveAllTemplateLayoutPages()) return
      page.saving = true
      const res = await api.grade.templateQuestionEdit(templateId, {
        page_index: page.pageIndex,
        ...payload,
      })
      if (templateId !== pendingTemplateId.value || !templateCutPreviewPages.value.includes(page)) return
      // A shift crosses page boundaries, so the answer editor and every preview
      // page have to take the server's new numbering rather than patch their own.
      applyTemplateQuestionEdit(res)
      ElMessage.success(successText)
    } catch (err) {
      notifyProcessingError(err, err.message || '题号调整失败')
    } finally {
      page.saving = false
      templateQuestionEditing.value = false
    }
  }

  async function insertTemplateQuestion(page, beforeNumber) {
    const target = String(beforeNumber || '').trim()
    if (!pendingTemplateId.value || templatePageEditingLocked(page) || templateCropGesture.value) return
    const blocked = templateReferenceQuestionEditBlockReason()
    if (blocked) { ElMessage.warning(blocked); return }
    // 教师标注：新增题号必须由老师输入——仅数字和点，首尾必须是数字，点不能连续。
    let number = ''
    try {
      const { value } = await ElMessageBox.prompt(
        '请输入新增题号（仅数字和“.”，如 2.3）',
        target ? `在第 ${target} 题前新增` : '末尾新增',
        {
          inputPattern: /^\d+(\.\d+)*$/,
          inputErrorMessage: '只能输入数字和点；开头与结尾必须是数字；点不能连续',
          inputPlaceholder: '例如 2.4',
          confirmButtonText: '新增题目',
          cancelButtonText: '取消',
        },
      )
      number = String(value || '').trim()
    } catch {
      return
    }
    if (!/^\d+(\.\d+)*$/.test(number)) {
      ElMessage.warning('只能输入数字和点；开头与结尾必须是数字；点不能连续')
      return
    }
    return editTemplateQuestion(
      page,
      { action: 'insert', before_question_number: target, new_question_number: number },
      target ? `已新增第 ${number} 题` : `已在本页末尾新增第 ${number} 题`,
    )
  }

  async function deleteTemplateQuestion(page, questionNumber) {
    const target = String(questionNumber || '').trim()
    if (!target) return
    const blocked = templateReferenceQuestionEditBlockReason()
    if (blocked) { ElMessage.warning(blocked); return }
    try {
      await ElMessageBox.confirm(
        `删除整道第 ${target} 题？该题的题干、答案和定位会一并移除，其余题号不变。若只删除图上的框，请取消并使用“删除所选”。`,
        '删除整道题',
        { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
    return editTemplateQuestion(
      page,
      { action: 'delete', question_number: target },
      `已删除第 ${target} 题的题号标注`,
    )
  }

  function applyTemplateQuestionEdit(res) {
    const localDrafts = new Map(templatePages.value.flatMap((page) => (page.questions || []).map((q) => [
      String(res.renamed_question_numbers?.[String(q.question_no)] ?? q.question_no), q,
    ])))
    const byIndex = new Map(
      (res.pages || []).map((page) => [Number(page.page_index), page.questions || []]),
    )
    templatePages.value = templatePages.value.map((page) => {
      const questions = byIndex.get(Number(page.page_index))
      if (!questions) return page
      const openState = new Map(
        (page.questions || []).map((q) => [String(q.question_no), !!q._open]),
      )
      return {
        ...page,
        questions: questions.map((question) => {
          const normalized = normalizeAnswerSpecForEditor(question)
          const local = localDrafts.get(String(normalized.question_no))
          return {
            ...normalized,
            ...local,
            ...templateQuestionMetadata(normalized),
            question_no: normalized.question_no,
            // A question with nothing in it is the one the teacher has to fill in,
            // so it opens; the rest keep whatever the teacher had open.
            _open: !normalized.question_text
              || openState.get(String(normalized.question_no))
              || false,
          }
        }),
      }
    })
    for (const previewPage of templateCutPreviewPages.value) {
      clearTemplateSelection(previewPage)
      previewPage.geometryTool = ''
      const artifact = (res.page_artifacts || {})[previewPage.pageLabel]
      if (!artifact) continue
      const reviewPending = artifact.geometry_review_pending === true
        || (res.geometry_review_pending_pages || []).map(Number).includes(Number(previewPage.pageIndex))
      // A teacher question-number edit is an explicit geometry decision.  Keep
      // the returned provenance even when an older server omits those fields.
      previewPage.questionAnchors = cloneQuestionAnchors(artifact.question_anchors, { teacherConfirmed: !reviewPending })
      applyTemplateDrawingSchema(previewPage, artifact.question_schema || [])
      previewPage.answerBoxes = mergeTemplateAnswerRegionSources(
        artifact.template_answer_regions,
        artifact.template_handwriting_regions,
      )
      previewPage.hasAnswerRegionContract = previewPage.answerBoxes.length > 0
      previewPage.handwritingBoxes = previewPage.answerBoxes
      initializeTemplatePageSlots(previewPage)
      const numbers = previewPage.questionAnchors.map((anchor) => String(anchor.question_number))
      if (!numbers.includes(String(previewPage.editQuestionNumber))) {
        previewPage.editQuestionNumber = numbers[0] || ''
      }
      if (!numbers.includes(String(previewPage.newHandwritingQuestion))) {
        previewPage.newHandwritingQuestion = numbers[0] || ''
      }
      // Insertion creates a visible placeholder, not a model-verified position.
      previewPage.dirty = reviewPending
      previewPage.insertedPlaceholder = reviewPending && res.action === 'insert'
      previewPage.columnsSource = 'teacher'
      previewPage.columnsConfirmed = !reviewPending
      previewPage.layoutReviewPending = reviewPending
      previewPage.layout_review_pending = reviewPending
      previewPage.geometryReviewPending = reviewPending
      previewPage.geometry_review_pending = reviewPending
      previewPage.anchorReviewPending = reviewPending
      if (!numbers.includes(String(previewPage.drawingQuestionNumber))) previewPage.drawingQuestionNumber = numbers[0] || ''
    }
    if (templateCutResult.value && Array.isArray(res.geometry_review_pending_pages)) {
      templateCutResult.value.geometry_review_pending_pages = [...res.geometry_review_pending_pages]
    }
  }

  return {
    templatePageNeedsSave, saveAllTemplateLayoutPages, saveTemplateLayoutPage, editTemplateQuestion,
    insertTemplateQuestion, deleteTemplateQuestion, applyTemplateQuestionEdit,
  }
}
