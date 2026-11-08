/**
 * The template editor's geometry on a page preview: columns, question / answer / handwriting / drawing
 * boxes, and the gestures that create, move, resize and delete them.
 *
 * - Page selection and locking (selectTemplatePreviewPage, templatePageEditingLocked), column reset and
 *   slot initialization, the layout conflict notice.
 * - Tools and selection (armTemplateGeometryTool, selectTemplateGeometryTarget, clearTemplateSelection, the
 *   drawing reference editors), creating a box (startTemplateGeometryCreation, commitTemplateGeometryCreation)
 *   and deleting one (deleteTemplateSelection).
 * - Pointer gestures (startTemplateGeometryGesture and its move/resize variants, moveTemplateGeometry,
 *   finishTemplateGeometry, cancelTemplateGeometry); every change marks its page dirty.
 *
 * Moved from views/Upload.vue; the page passes its earlier parts in ctx (see Upload.vue).
 */
import { nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  templateFillAnswerSlots,
  templateQuestionIdentity,
  templateQuestionMetadata,
  serializeTemplateDrawingReview,
} from '@/utils/templateReview'
import {
  prepareTemplateSlots,
  swapTemplateSlotGeometry,
  templateSlotBoxes,
  addTemplateSlot,
  removeTemplateSlot,
  convertTemplateDrawingSlots,
} from '@/utils/templateSlots'
import {
  normalizeTemplateBBoxForPage,
  markTemplateGeometryConfirmed,
  templateQuestionAnchorBBox,
  templateVisibleAnswerBoxes,
  equalTemplateColumns,
} from '@/utils/templateGeometry'
import {
  templateHasDrawingQuestion,
  ensureTemplateDrawingLabel,
  templateFollowingDrawingLabel,
} from '@/utils/templateDrawing'

export function useTemplateGeometry(ctx) {
  const {
    templatePages, templateCutPreviewPages, templateCropGesture, templateReferenceEditorInstances,
    templateActivePreviewPage, TEMPLATE_GEOMETRY_TOOLS, templateEditorBusy, questions,
    setTemplatePreviewEditMode,
  } = ctx

  function templatePreviewQuestionIdentity(page, anchor) {
    const number = String(anchor.question_number ?? anchor.question_no ?? '')
    const schema = page.questionSchema.find((item) => String(item.question_number ?? item.question_no ?? '') === number)
    return templateQuestionIdentity({ ...anchor, ...schema })
  }

  function templateLayoutConflictNotice(page) {
    const candidateReviewPending = Boolean(page?.layoutReviewPending || page?.layout_review_pending
      || page?.anchorReviewPending || page?.anchor_review_pending
      || page?.geometryReviewPending || page?.geometry_review_pending
      || page?.question_schema_candidates?.length)
    if (!candidateReviewPending) return ''
    const schema = serializeTemplateDrawingReview(
      page?.questionSchema || [], page?.drawingBoxes || [], page?.drawingLabels || [],
      { preserveConflictingGeometry: true },
    )
    const validBox = (value) => Array.isArray(value) && value.length === 4
      && value.every((part) => typeof part === 'number' && Number.isFinite(part) && part >= 0 && part <= 1000)
      && value[0] < value[2] && value[1] < value[3]
    const validPoint = (value) => Array.isArray(value) && value.length === 2
      && value.every((part) => typeof part === 'number' && Number.isFinite(part) && part >= 0 && part <= 1000)
    const conflicts = []
    for (const question of schema) {
      const number = String(question.question_number ?? question.question_no ?? '').trim()
      if (!number) continue
      // A question whose boxes were all deleted leaves on save; its geometry no longer matters.
      if (templateAnswerQuestion(page, number)?._pendingRemoval) continue
      const regions = Array.isArray(question.drawing_regions) ? question.drawing_regions : []
      const hasAnyRegion = regions.length > 0
      const validRegions = regions.filter((region) => validBox(region))
      const hasAnchorValue = question.label_anchor != null
      const hasValidAnchor = validPoint(question.label_anchor)
      if (question.answer_kind === 'non_graphic' && (hasAnyRegion || hasAnchorValue)) {
        conflicts.push(`Q${number} 是非作图题，但仍带有画图区或作图题号（删除它们，或把作图属性改为作图题）`)
        continue
      }
      if (!['drawing', 'mixed'].includes(question.answer_kind)) continue
      // A missing Q marker is re-derived beside the drawing area at save time.
      if (!validRegions.length) conflicts.push(`Q${number} 是${question.answer_kind === 'mixed' ? '图文混合题' : '作图题'}，但还没有画图区`)
      else if (hasAnchorValue && !hasValidAnchor) conflicts.push(`Q${number} 的作图题号位置无效`)
    }
    if (!conflicts.length) return ''
    return `${conflicts.join('；')}。请用“绿色画图区”框出作图位置，或把该题“作图属性”改为非作图；当前草稿中的修改已保留。`
  }

  function setTemplateDrawingKind(page, kind) {
    if (templatePageEditingLocked(page)) return
    if (!['non_graphic', 'drawing', 'mixed'].includes(kind)) return
    const question = page.questionSchema.find((item) => String(item.question_number ?? item.question_no) === String(page.drawingQuestionNumber))
    const staleNonGraphicGeometry = kind === 'non_graphic'
      && (Array.isArray(question?.drawing_regions) && question.drawing_regions.length > 0
        || question?.label_anchor != null
        || question?.region_kinds?.includes('drawing'))
    if (!question || (question.answer_kind === kind && !staleNonGraphicGeometry)) return
    const number = String(page.drawingQuestionNumber)
    const slotQuestion = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions
      ?.find((item) => String(item.question_no) === number)
    if (kind !== 'non_graphic' && slotQuestion?.slot_ids
      && !(page.drawingBoxes || []).some((item) => String(item.question_number) === number)) {
      // 作图属性以教师框出的画图区为准：先框选，属性随框生效。
      page.geometryQuestionNumber = number
      ElMessage.info(`请在原图上框出 Q${number} 的画图区`)
      void armTemplateGeometryTool(page, 'drawing')
      return
    }
    if (kind !== 'non_graphic' && slotQuestion?.slot_ids && slotQuestion.region_kinds?.includes('drawing')) {
      // 有画图区时，“作图题/图文混合”由框决定：另有文字作答框即为图文混合。
      const derived = slotQuestion.region_kinds.includes('answer') ? 'mixed' : 'drawing'
      if (derived !== kind) {
        ElMessage.info(derived === 'mixed'
          ? `Q${number} 还有文字作答框，按图文混合保存；只作图请删除文字作答框`
          : `Q${number} 只有画图区，按作图题保存；需要文字作答请用“蓝色作答框”框出位置`)
      }
      return
    }
    clearTemplateSelection(page)
    page.geometryTool = ''
    if (kind === 'non_graphic' && slotQuestion?.slot_ids && slotQuestion.region_kinds?.includes('drawing')) {
      if (slotQuestion.region_kinds.includes('answer')) {
        // 图文混合改为非作图：作图位置随之删除（与逐个删除画图区相同）。
        const drawingSlots = slotQuestion.slot_ids.filter((_, index) => slotQuestion.region_kinds[index] === 'drawing')
        for (const slotId of drawingSlots.reverse()) removeTemplateSlot(page, slotQuestion, slotId)
      } else {
        // 纯作图题改为非作图：画图区改作文字作答框，位置保留。
        convertTemplateDrawingSlots(page, slotQuestion)
      }
    } else if (kind === 'non_graphic' && !slotQuestion?.slot_ids
      && !(page.answerBoxes || []).some((box) => String(box.question_number) === number)) {
      // 无槽编号的纯作图题：同样把画图区改作文字作答框，不让题目失去位置。
      for (const box of (page.drawingBoxes || []).filter((item) => String(item.question_number) === number)) {
        ;(page.answerBoxes ||= []).push({ question_number: number, bbox: [...box.bbox] })
        page.hasAnswerRegionContract = true
      }
    }
    question.answer_kind = kind
    question._teacherDrawingKindEdited = true
    if (kind === 'non_graphic') {
      question.drawing_regions = []
      question.label_anchor = null
      delete question.drawing_reference
      page.drawingBoxes = page.drawingBoxes.filter((item) => String(item.question_number) !== String(page.drawingQuestionNumber))
      page.drawingLabels = page.drawingLabels.filter((item) => String(item.question_number) !== String(page.drawingQuestionNumber))
    }
    const answerQuestion = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions
      ?.find((item) => String(item.question_no) === String(page.drawingQuestionNumber))
    if (answerQuestion) {
      Object.assign(answerQuestion, templateQuestionMetadata(question))
      if (kind === 'non_graphic') {
        delete answerQuestion.drawing_reference
        answerQuestion._drawingReferenceDirty = false
      } else {
        page.referenceQuestionNumber = String(page.drawingQuestionNumber)
      }
    }
    markTemplatePageGeometryDirty(page, page.drawingQuestionNumber)
  }


  function resetTemplateColumns(page) {
    if (templatePageEditingLocked(page)) return
    clearTemplateSelection(page)
    page.geometryTool = ''
    const count = Math.max(1, Math.min(3, Number(page.columnCount) || 2))
    page.columns = equalTemplateColumns(count)
    // Equal partitions are only a teacher-editable draft.  They must not be
    // mistaken for a model-detected layout until this page is saved.
    page.columnsSource = 'teacher_draft'
    page.columnsConfirmed = false
    page.layoutReviewPending = true
    page.layout_review_pending = true
    markTemplatePageGeometryDirty(page)
  }


  function initializeTemplatePageSlots(page) {
    const questions = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions || []
    try {
      prepareTemplateSlots(page, questions)
      page.slotContractError = ''
    } catch (error) {
      page.slotContractError = error.message
    }
  }

  function templateSlotAnswer(page, box) {
    const question = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions
      ?.find(item => String(item.question_no) === String(box.question_number))
    const index = question?.slot_ids?.indexOf(box.slot_id) ?? -1
    if (index < 0) return '答案未绑定'
    return question.region_kinds?.[index] === 'drawing' ? '作图（不填文字）' : templateFillAnswerSlots(question)[index] || '待填写答案'
  }

  async function correctTemplateSlotPosition(page, box) {
    if (templatePageEditingLocked(page) || templateCropGesture.value || page.slotContractError) return
    const choices = templateSlotBoxes(page).filter(item => String(item.question_number) === String(box.question_number)
      && item.slot_id !== box.slot_id && item.bbox?.length === 4)
    if (!choices.length) return
    try {
      const { value } = await ElMessageBox.prompt(
        `将 Q${box.slot_id} 的物理框与另一槽调换。答案、类型和分数仍绑定原编号，不随框移动。可选：${choices.map(item => item.slot_id).join('、')}`,
        '修正槽位对应位置',
        { inputPlaceholder: choices[0].slot_id, inputValidator: value => choices.some(item => item.slot_id === value) || '请输入上方完整槽编号（不含 Q）', confirmButtonText: '仅调换物理框', cancelButtonText: '取消' },
      )
      if (templatePageEditingLocked(page) || !templateCutPreviewPages.value.includes(page)) return
      if (swapTemplateSlotGeometry(page, box.slot_id, value)) {
        markTemplatePageGeometryDirty(page, box.question_number)
        clearTemplateSelection(page)
        ElMessage.success('已调换物理框；槽编号对应的答案未改变，保存模板后生效')
      }
    } catch (error) {
      if (error instanceof Error) ElMessage.warning(error.message)
    }
  }

  function templatePageEditingLocked(page) {
    return templateEditorBusy.value || !!page.saving
  }

  function templatePreviewPageIndex() {
    return templateCutPreviewPages.value.some((page) => Number(page.pageIndex) === templateActivePreviewPage.value)
      ? templateActivePreviewPage.value : Number(templateCutPreviewPages.value[0]?.pageIndex || 0)
  }

  function templatePreviewPagePosition() {
    return templateCutPreviewPages.value.findIndex((page) => Number(page.pageIndex) === templatePreviewPageIndex())
  }

  function selectTemplatePreviewPage(page) {
    if (!page || templatePageEditingLocked(page)) return
    if (templateCropGesture.value) cancelTemplateGeometry({ pointerId: templateCropGesture.value.pointerId })
    templateActivePreviewPage.value = Number(page.pageIndex)
    void nextTick(() => document.getElementById(`template-reference-page-${page.pageIndex}`)?.scrollIntoView({ block: 'start' }))
  }

  function templateGeometryQuestions(page) {
    const byNumber = new Map()
    for (const item of [...(page.questionAnchors || []), ...(page.questionSchema || [])]) {
      const number = String(item.question_number ?? item.question_no ?? '').trim()
      if (number) byNumber.set(number, { ...byNumber.get(number), ...item, question_number: number })
    }
    return [...byNumber.values()]
  }

  function templateGeometryQuestionNumber(page) {
    const questions = templateGeometryQuestions(page)
    const preferred = page.geometryQuestionNumber || page.drawingQuestionNumber || page.editQuestionNumber
    return questions.some((item) => item.question_number === String(preferred))
      ? String(preferred) : questions[0]?.question_number || ''
  }

  function templateGeometryToolLabel(kind) {
    return TEMPLATE_GEOMETRY_TOOLS.find((tool) => tool.key === kind)?.label || '标记'
  }

  function selectTemplateGeometryTarget(page, selected) {
    clearTemplateSelection(page)
    page.geometrySelection = selected
    const number = selected.target?.question_number
    if (number) {
      page.geometryQuestionNumber = String(number)
      page.drawingQuestionNumber = String(number)
      page.editQuestionNumber = String(number)
      page.newHandwritingQuestion = String(number)
      if (templateHasDrawingQuestion(page, number)) page.referenceQuestionNumber = String(number)
    }
  }

  function templateGeometryHint(page) {
    if (page.pendingSlotId) return `请在原图上为 Q${page.pendingSlotId} 划出作答区域；若为多余区域，请点击“删除所选”。`
    if (page.geometrySelection?.target?.slot_id) return `已选中 Q${page.geometrySelection.target.slot_id}，移动只改坐标；使用图外“调换位置”纠正槽顺序。删除将同步移除该槽答案及分值，不顺延其余编号。`
    if (page.geometryTool === 'drawing-label') return '在作图区左侧点一下，放置作图 Q；标签向左展开。'
    if (page.geometryTool) return `用鼠标拖拽或触屏划框，新增 Q${templateGeometryQuestionNumber(page)} ${templateGeometryToolLabel(page.geometryTool)}${page.geometryTool === 'question' ? '（替换当前题号框）' : ''}。`
    if (page.referenceStrokeSelected) return '已选中标准图笔画，可点击“删除所选”。'
    if (page.geometrySelection) return `已选中${page.geometrySelection.kind === 'divider' ? '分栏线（删除后合并相邻栏）' : templateGeometryToolLabel(page.geometrySelection.kind)}，可拖动或删除；只删标记，不删题目。`
    return '鼠标 / 触屏：选新增类型后划框；点选标记移动、缩放或删除。'
  }

  function setTemplateReferenceEditor(page, question, instance) {
    const key = `${page.pageIndex}:${String(question?.question_no || '')}`
    if (instance) templateReferenceEditorInstances.set(key, instance)
    else templateReferenceEditorInstances.delete(key)
  }

  function activeTemplateReferenceEditor(page) {
    return templateReferenceEditorInstances.get(`${page.pageIndex}:${String(page.referenceQuestionNumber || '')}`) || null
  }

  function clearTemplateSelection(page) {
    if (page.pendingSlotId) page.geometryTool = ''
    page.pendingSlotId = ''
    page.geometrySelection = null
    page.referenceStrokeSelected = false
    activeTemplateReferenceEditor(page)?.clearSelection()
  }

  function setTemplateStrokeSelection(page, selected) {
    page.referenceStrokeSelected = selected
    if (selected) {
      page.geometrySelection = null
      page.geometryTool = ''
    }
  }

  function markTemplatePageGeometryDirty(page, number = '') {
    page.dirty = true
    const answerPage = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))
    for (const question of answerPage?.questions || []) {
      if (number && String(question.question_no) !== String(number)) continue
      if (question.drawing_reference) {
        question.drawing_reference = { ...question.drawing_reference, teacher_confirmed: false }
        question._drawingReferenceDirty = true
      }
    }
  }

  async function armTemplateGeometryTool(page, kind) {
    if (templatePageEditingLocked(page)) return false
    if (kind && !TEMPLATE_GEOMETRY_TOOLS.some((tool) => tool.key === kind)) return false
    // Every newly created marker must be explicitly bound to a teacher-entered
    // question identity.  The fallback keeps non-browser unit harnesses (which
    // do not provide Element Plus' prompt) deterministic; the real page always
    // shows the validated editor before arming the canvas tool.
    let number = templateGeometryQuestionNumber(page)
    if (typeof ElMessageBox !== 'undefined' && typeof ElMessageBox.prompt === 'function') {
      page.geometryQuestionPrompting = true
      try {
        const { value } = await ElMessageBox.prompt(
          '请输入此框所属题号（仅数字和“.”，如 2.3）',
          `设置${templateGeometryToolLabel(kind)}题号`,
          {
            inputValue: number,
            inputPattern: /^\d+(\.\d+)*$/,
            inputErrorMessage: '只能输入数字和点；首尾必须是数字；点不能连续',
            inputPlaceholder: '例如 2.3',
            confirmButtonText: '继续框选',
            cancelButtonText: '取消',
          },
        )
        number = String(value || '').trim()
      } catch {
        return false
      } finally {
        page.geometryQuestionPrompting = false
      }
    }
    if (!/^\d+(\.\d+)*$/.test(number)) {
      ElMessage.warning('只能输入数字和点；首尾必须是数字；点不能连续')
      return false
    }
    page.geometryQuestionNumber = number
    page.drawingQuestionNumber = number
    page.editQuestionNumber = number
    page.newHandwritingQuestion = number
    // A drawing area may be drawn on any question (the box makes it a drawing
    // question); the Q marker alone needs an area to sit beside.
    if (kind === 'drawing-label' && !(page.drawingBoxes || []).some((box) => String(box.question_number) === number)) {
      ElMessage.info(`请先用“绿色画图区”框出 Q${number} 的作图位置；作图题号会自动放在画图区左侧`)
      return false
    }
    setTemplatePreviewEditMode(page, 'geometry')
    page.geometryTool = kind
    return true
  }

  function startTemplateGeometryCreation(event, page) {
    if (templatePageEditingLocked(page) || !page.geometryTool || templateCropGesture.value) return
    if (event.button !== undefined && event.button !== 0) return
    const stage = event.currentTarget.closest('.tpl-cut-preview-stage')
    const rect = stage?.getBoundingClientRect()
    if (!rect?.width || !rect?.height) return
    event.preventDefault()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    const point = [
      Math.max(0, Math.min(1000, Math.round((event.clientX - rect.left) * 1000 / rect.width))),
      Math.max(0, Math.min(1000, Math.round((event.clientY - rect.top) * 1000 / rect.height))),
    ]
    templateCropGesture.value = {
      pointerId: event.pointerId, page, mode: 'create', kind: page.geometryTool,
      questionNumber: templateGeometryQuestionNumber(page), point, endPoint: point,
      startX: event.clientX, startY: event.clientY, width: rect.width, height: rect.height,
    }
    page.geometryDraft = [...point, ...point]
  }

  function commitTemplateGeometryCreation(gesture) {
    const { page, kind, questionNumber: number } = gesture
    let target
    if (kind === 'drawing-label') {
      target = page.drawingLabels.find((item) => String(item.question_number) === number)
      if (!target) { target = { question_number: number }; page.drawingLabels.push(target) }
      target.label_anchor = [...gesture.endPoint]
      target = page.drawingLabels.find((item) => String(item.question_number) === number)
    } else {
      const bbox = normalizeTemplateBBoxForPage(page.geometryDraft)
      if (bbox.length !== 4 || (bbox[2] - bbox[0]) * gesture.width / 1000 < 4 || (bbox[3] - bbox[1]) * gesture.height / 1000 < 4) return
      // Retain the question identity even if its printed-number marker was deleted.
      let anchor = page.questionAnchors.find((item) => String(item.question_number) === number)
      if (!anchor) {
        page.questionAnchors.push({ question_number: number, question_number_bbox: [] })
        anchor = page.questionAnchors.find((item) => String(item.question_number) === number)
      }
      if (kind === 'question') {
        anchor.question_number_bbox = bbox
        target = anchor
      } else {
        // 兼容回退: a slot-aware question exists in the page schema even when the
        // questions response row is missing — never fall to the legacy branch and
        // create a mixed box shape (slot-less box among slot boxes).
        const slotQuestion = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions?.find(q => String(q.question_no) === number)
          || (page.questionSchema || []).find(q => String(q.question_no ?? q.question_number) === number && (q.slot_ids || q.region_kinds))
        const clearedSchema = (page.questionSchema || []).find(q => String(q.question_no ?? q.question_number) === number && q._slotsCleared)
        if (clearedSchema && slotQuestion && !slotQuestion.slot_ids) {
          // Every slot of this question was deleted; a new box starts a fresh slot list.
          Object.assign(slotQuestion, { slot_ids: [], region_kinds: [], answer_slots_text: [] })
          delete clearedSchema._slotsCleared
        }
        if (slotQuestion?.slot_ids) {
          try {
            const pending = page.answerBoxes.find(box => box.slot_id === page.pendingSlotId
              && String(box.question_number) === number && box.bbox.length !== 4)
            if (pending) {
              const pendingKind = pending.region_kind || 'answer'
              if (pendingKind !== kind) {
                ElMessage.warning(`Q${pending.slot_id} 是${pendingKind === 'drawing' ? '作图区' : '文字作答框'}，请用对应工具框选`)
                return
              }
              pending.bbox = [...bbox]
              delete pending.pendingGeometry
              target = pending
              if (kind === 'drawing') {
                // Same shape as addTemplateSlot: a drawing slot also owns a
                // drawing region, which feeds drawing_regions and grading crops.
                page.drawingBoxes.push({ ...pending, bbox: [...bbox] })
                target = page.drawingBoxes.at(-1)
              }
            } else target = addTemplateSlot(page, slotQuestion, bbox, kind)
          }
          catch (error) { ElMessage.warning(error.message); return }
        } else {
        const boxes = kind === 'drawing' ? page.drawingBoxes : page.answerBoxes
        if (boxes.filter((item) => String(item.question_number) === number).length >= 8) {
          ElMessage.warning(`Q${number} 最多添加 8 个${templateGeometryToolLabel(kind)}`)
          return
        }
        boxes.push({ question_number: number, bbox })
        target = boxes[boxes.length - 1]
        const schemaQuestion = (page.questionSchema || []).find((item) => String(item.question_number ?? item.question_no) === number)
        if (kind === 'drawing' && schemaQuestion && !['drawing', 'mixed'].includes(schemaQuestion.answer_kind)) {
          // 教师框出画图区即表示此题需要作图：属性随框生效，不再等待另选作图属性。
          const hasText = templateVisibleAnswerBoxes(page).some((item) => String(item.question_number) === number)
          schemaQuestion.answer_kind = hasText ? 'mixed' : 'drawing'
          const answerQuestion = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions
            ?.find((item) => String(item.question_no) === number)
          if (answerQuestion) answerQuestion.answer_kind = schemaQuestion.answer_kind
        }
        }
        if (kind === 'answer') page.hasAnswerRegionContract = true
        if (kind === 'drawing') ensureTemplateDrawingLabel(page, number)
        // Re-framing a position keeps a question that was due for removal.
        const keptQuestion = templateAnswerQuestion(page, number)
        if (keptQuestion) delete keptQuestion._pendingRemoval
      }
    }
    markTemplateGeometryConfirmed(target)
    markTemplatePageGeometryDirty(page, number)
    selectTemplateGeometryTarget(page, { kind, target })
    page.geometryTool = ''
  }

  function deleteTemplateSelection(page) {
    if (templatePageEditingLocked(page) || templateCropGesture.value) return
    if (page.referenceStrokeSelected) {
      activeTemplateReferenceEditor(page)?.deleteSelectedStroke()
      return
    }
    const selected = page.geometrySelection
    if (!selected) return
    if (['answer', 'drawing'].includes(selected.kind) && selected.target?.slot_id) {
      const question = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions?.find(q => String(q.question_no) === String(selected.target.question_number))
      try { removeTemplateSlot(page, question, selected.target.slot_id) }
      catch (error) { ElMessage.warning(error.message); return }
      markTemplateQuestionRemovalIfEmpty(page, selected.target.question_number)
      markTemplatePageGeometryDirty(page, selected.target.question_number)
      clearTemplateSelection(page)
      return
    }
    if (selected.kind === 'divider') {
      const left = page.columns[selected.index]
      const right = page.columns[selected.index + 1]
      if (!left || !right) return
      left.x2_1000 = right.x2_1000
      page.columns.splice(selected.index + 1, 1)
      page.columns.forEach((item, index) => { item.column_index = index + 1 })
      page.columnCount = page.columns.length
    } else if (selected.kind === 'question') {
      if (!page.questionAnchors.includes(selected.target)) return
      selected.target.question_number_bbox = []
      delete selected.target.question_bbox
      delete selected.target.bbox
    } else {
      const boxes = { answer: page.answerBoxes, drawing: page.drawingBoxes, 'drawing-label': page.drawingLabels }[selected.kind]
      const index = boxes?.indexOf(selected.target) ?? -1
      if (index < 0) return
      boxes.splice(index, 1)
      if (['drawing', 'drawing-label'].includes(selected.kind)) {
        const number = String(selected.target?.question_number || '')
        const question = (page.questionSchema || []).find((item) => String(item.question_number ?? item.question_no) === number)
        const hasRemainingDrawing = (page.drawingBoxes || []).some((item) => String(item.question_number) === number)
        if (question && !hasRemainingDrawing) {
          // 教师删掉了该题最后一个画图区：作图题号与标准图随之消失，题目按非作图保存。
          page.drawingQuestionNumber = number
          setTemplateDrawingKind(page, 'non_graphic')
        }
      }
      if (['answer', 'drawing'].includes(selected.kind)) markTemplateQuestionRemovalIfEmpty(page, selected.target?.question_number)
    }
    markTemplatePageGeometryDirty(page, selected.target?.question_number || '')
    clearTemplateSelection(page)
  }

  function templateQuestionHasBoxes(page, number) {
    return (page.answerBoxes || []).some((box) => String(box.question_number) === String(number) && box.bbox?.length === 4)
      || (page.drawingBoxes || []).some((box) => String(box.question_number) === String(number))
  }

  function templateAnswerQuestion(page, number) {
    return templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions
      ?.find((item) => String(item.question_no) === String(number))
  }

  /** 教师删掉了该题全部作答框和画图区：题号随之消失，保存模板时删除这道题（其余题号不变）。 */
  function markTemplateQuestionRemovalIfEmpty(page, number) {
    const question = templateAnswerQuestion(page, number)
    if (question && !templateQuestionHasBoxes(page, number)) question._pendingRemoval = true
  }

  function startTemplateGeometryGesture(event, page, target, mode, dividerIndex = -1, bboxKey = 'bbox') {
    if (templatePageEditingLocked(page) || page.geometryTool || templateCropGesture.value) return
    if (event.button !== undefined && event.button !== 0) return
    const stage = event.currentTarget.closest('.tpl-cut-preview-stage')
    if (!stage) return
    event.preventDefault()
    selectTemplateGeometryTarget(page, { kind: mode === 'drawing-label' ? mode : mode.split('-')[0], target, index: dividerIndex })
    event.currentTarget.setPointerCapture?.(event.pointerId)
    const followLabel = mode === 'drawing-move' ? templateFollowingDrawingLabel(page, target) : null
    templateCropGesture.value = {
      pointerId: event.pointerId, page, target, mode, dividerIndex, bboxKey,
      startX: event.clientX, startY: event.clientY,
      bbox: bboxKey === 'question_number_bbox' ? templateQuestionAnchorBBox(target) : target?.[bboxKey] ? [...target[bboxKey]] : null,
      wasDirty: page.dirty,
      boundary: dividerIndex >= 0 ? page.columns[dividerIndex].x2_1000 : null,
      width: stage.clientWidth, height: stage.clientHeight,
      followLabel, followPoint: followLabel ? [...followLabel.label_anchor] : null,
    }
  }

  function startTemplateDividerMove(event, page, dividerIndex) {
    startTemplateGeometryGesture(event, page, null, 'divider', dividerIndex)
  }

  function startTemplateHandwritingMove(event, page, box) {
    startTemplateGeometryGesture(event, page, box, 'handwriting-move')
  }

  function startTemplateHandwritingResize(event, page, box) {
    startTemplateGeometryGesture(event, page, box, 'handwriting-resize')
  }

  function startTemplateQuestionMove(event, page, anchor) {
    startTemplateGeometryGesture(event, page, anchor, 'question-move', -1, 'question_number_bbox')
  }

  function startTemplateQuestionResize(event, page, anchor) {
    startTemplateGeometryGesture(event, page, anchor, 'question-resize', -1, 'question_number_bbox')
  }

  function startTemplateAnswerMove(event, page, box) {
    startTemplateGeometryGesture(event, page, box, 'answer-move')
  }

  function startTemplateAnswerResize(event, page, box) {
    startTemplateGeometryGesture(event, page, box, 'answer-resize')
  }

  function startTemplateDrawingLabelMove(event, page, label) {
    startTemplateGeometryGesture(event, page, label, 'drawing-label')
    if (templateCropGesture.value?.target === label) {
      templateCropGesture.value.point = [...label.label_anchor]
    }
  }

  function moveTemplateGeometry(event) {
    const gesture = templateCropGesture.value
    if (!gesture || gesture.pointerId !== event.pointerId) return
    if (templatePageEditingLocked(gesture.page)) { cancelTemplateGeometry(event); return }
    const dx = Math.round((event.clientX - gesture.startX) * 1000 / Math.max(1, gesture.width))
    const dy = Math.round((event.clientY - gesture.startY) * 1000 / Math.max(1, gesture.height))
    if (gesture.mode === 'create') {
      const [x, y] = gesture.point
      const endX = Math.max(0, Math.min(1000, x + dx))
      const endY = Math.max(0, Math.min(1000, y + dy))
      gesture.endPoint = [endX, endY]
      gesture.page.geometryDraft = [Math.min(x, endX), Math.min(y, endY), Math.max(x, endX), Math.max(y, endY)]
      return
    }
    if (!dx && !dy && !gesture.changed) return
    gesture.changed = true
    if (gesture.mode === 'drawing-label' && gesture.point) {
      // The point is the Q label's right-edge midpoint, not its top-left corner.
      gesture.target.label_anchor = [
        Math.max(0, Math.min(1000, gesture.point[0] + dx)),
        Math.max(0, Math.min(1000, gesture.point[1] + dy)),
      ]
      gesture.page.dirty = true
      return
    }
    if (gesture.mode === 'divider') {
      const leftColumn = gesture.page.columns[gesture.dividerIndex]
      const rightColumn = gesture.page.columns[gesture.dividerIndex + 1]
      const boundary = Math.max(
        leftColumn.x1_1000 + 60,
        Math.min(rightColumn.x2_1000 - 60, gesture.boundary + dx),
      )
      leftColumn.x2_1000 = boundary
      rightColumn.x1_1000 = boundary
      gesture.page.dirty = true
      return
    }
    if (!gesture.bbox || !gesture.target) return
    const [left, top, right, bottom] = gesture.bbox
    const bboxKey = gesture.bboxKey || 'bbox'
    if (gesture.mode.endsWith('resize')) {
      gesture.target[bboxKey] = [
        left,
        top,
        Math.max(left + 8, Math.min(1000, right + dx)),
        Math.max(top + 8, Math.min(1000, bottom + dy)),
      ]
    } else {
      const width = right - left
      const height = bottom - top
      const nextLeft = Math.max(0, Math.min(1000 - width, left + dx))
      const nextTop = Math.max(0, Math.min(1000 - height, top + dy))
      gesture.target[bboxKey] = [nextLeft, nextTop, nextLeft + width, nextTop + height]
      if (gesture.followLabel) {
        gesture.followLabel.label_anchor = [
          Math.max(0, Math.min(1000, gesture.followPoint[0] + nextLeft - left)),
          Math.max(0, Math.min(1000, gesture.followPoint[1] + nextTop - top)),
        ]
      }
    }
    gesture.page.dirty = true
  }

  function finishTemplateGeometry(event) {
    const gesture = templateCropGesture.value
    if (!gesture || gesture.pointerId !== event.pointerId) return
    if (templatePageEditingLocked(gesture.page)) { cancelTemplateGeometry(event); return }
    if (Number.isFinite(event.clientX) && Number.isFinite(event.clientY)) moveTemplateGeometry(event)
    if (gesture.mode === 'create') commitTemplateGeometryCreation(gesture)
    else if (gesture.changed) {
      markTemplateGeometryConfirmed(gesture.target)
      markTemplatePageGeometryDirty(gesture.page, gesture.target?.question_number || '')
    }
    gesture.page.geometryDraft = null
    templateCropGesture.value = null
  }

  function setTemplateAnswerDrawingKind(pageIndex, question, kind) {
    const page = templateCutPreviewPages.value.find((item) => Number(item.pageIndex) === Number(pageIndex))
    if (!page) {
      if (!question || !['non_graphic', 'drawing', 'mixed'].includes(kind)) return
      question.answer_kind = kind
      question._teacherDrawingKindEdited = true
      if (kind === 'non_graphic') {
        question.drawing_regions = []
        question.label_anchor = null
        delete question.drawing_reference
      }
      return
    }
    page.drawingQuestionNumber = String(question?.question_no || '')
    setTemplateDrawingKind(page, kind)
  }

  function cancelTemplateGeometry(event) {
    const gesture = templateCropGesture.value
    if (!gesture || gesture.pointerId !== event.pointerId) return
    if (gesture.mode !== 'create') {
      if (gesture.mode === 'divider') {
        gesture.page.columns[gesture.dividerIndex].x2_1000 = gesture.boundary
        gesture.page.columns[gesture.dividerIndex + 1].x1_1000 = gesture.boundary
      } else if (gesture.point) gesture.target.label_anchor = [...gesture.point]
      else if (gesture.bbox) gesture.target[gesture.bboxKey] = [...gesture.bbox]
      if (gesture.followLabel) gesture.followLabel.label_anchor = [...gesture.followPoint]
      gesture.page.dirty = gesture.wasDirty
    }
    gesture.page.geometryDraft = null
    templateCropGesture.value = null
  }

  return {
    templatePreviewQuestionIdentity, templateLayoutConflictNotice, setTemplateDrawingKind,
    resetTemplateColumns, initializeTemplatePageSlots, templateSlotAnswer,
    correctTemplateSlotPosition, templatePageEditingLocked, templatePreviewPageIndex,
    templatePreviewPagePosition, selectTemplatePreviewPage, templateGeometryQuestions,
    templateGeometryQuestionNumber, templateGeometryToolLabel, selectTemplateGeometryTarget,
    templateGeometryHint, setTemplateReferenceEditor, activeTemplateReferenceEditor,
    clearTemplateSelection, setTemplateStrokeSelection, markTemplatePageGeometryDirty,
    armTemplateGeometryTool, startTemplateGeometryCreation, commitTemplateGeometryCreation,
    deleteTemplateSelection, templateQuestionHasBoxes, templateAnswerQuestion,
    markTemplateQuestionRemovalIfEmpty, startTemplateGeometryGesture, startTemplateDividerMove,
    startTemplateHandwritingMove, startTemplateHandwritingResize, startTemplateQuestionMove,
    startTemplateQuestionResize, startTemplateAnswerMove, startTemplateAnswerResize,
    startTemplateDrawingLabelMove, moveTemplateGeometry, finishTemplateGeometry,
    setTemplateAnswerDrawingKind, cancelTemplateGeometry,
  }
}
