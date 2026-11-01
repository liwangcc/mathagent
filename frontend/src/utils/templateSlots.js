import { templateFillAnswerSlots } from './templateReview.js'

const numberOf = q => String(q.question_number ?? q.question_no ?? '')
const owns = (q, key) => Object.hasOwn(q, key)
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

export function templateSlotIdsValid(question, count = templateFillAnswerSlots(question).length) {
  if (!owns(question, 'slot_ids')) return true
  const ids = question.slot_ids
  const prefix = `${numberOf(question)}[`
  return Array.isArray(ids) && ids.length === count && count > 0
    && new Set(ids).size === count
    && ids.every(id => typeof id === 'string' && id.startsWith(prefix)
      && /^[1-9]\d*\]$/.test(id.slice(prefix.length)))
}

export function templateSlotLabel(question, index) {
  return `Q${question.slot_ids?.[index] ?? `${numberOf(question)}[${index + 1}]`}`
}

/** Stage legacy IDs only in the review draft; never renumber explicit server IDs. */
export function prepareTemplateSlots(page, questions = []) {
  for (const schema of page.questionSchema || []) {
    const number = numberOf(schema)
    const question = questions.find(q => numberOf(q) === number)
    if (!question) continue
    const boxes = (page.answerBoxes || []).filter(b => String(b.question_number) === number)
    const explicit = owns(schema, 'slot_ids') || owns(question, 'slot_ids')
    if (!explicit && !owns(schema, 'region_kinds') && !owns(question, 'region_kinds') && boxes.length < 2) continue
    // The schema may own types even when an older questions response omits them.
    if (owns(schema, 'region_kinds') && !owns(question, 'region_kinds')) question.region_kinds = [...schema.region_kinds]
    const slots = templateFillAnswerSlots(question)
    // Pending geometry belongs to a draft, never to the confirmed artifact.
    // Stage empty placeholders so teachers can locate or delete solver-only IDs.
    const explicitIds = question.slot_ids ?? schema.slot_ids
    if (explicit && Array.isArray(explicitIds) && slots.length === explicitIds.length
      && (page.geometryReviewPending || page.geometry_review_pending)
      && boxes.every(box => typeof box.slot_id === 'string' && explicitIds.includes(box.slot_id))) {
      for (const id of explicitIds) {
        if (boxes.some(box => box.slot_id === id)) continue
        const box = { question_number: number, slot_id: id, bbox: [], pendingGeometry: true }
        page.answerBoxes.push(box)
        boxes.push(box)
      }
    }
    const count = boxes.length
    if (!count || slots.length !== count) throw new Error(`Q${number} 作答框与答案槽数量不一致，不能自动编号`)
    if (owns(schema, 'slot_ids') && owns(question, 'slot_ids') && !same(schema.slot_ids, question.slot_ids)) {
      throw new Error(`Q${number} 槽编号回包不一致，请重新打开模板`)
    }
    const ids = explicit ? (question.slot_ids ?? schema.slot_ids) : boxes.map((_, index) => `${number}[${index + 1}]`)
    if (!templateSlotIdsValid({ question_no: number, slot_ids: ids }, count)) throw new Error(`Q${number} 槽编号无效`)
    if (boxes.some(b => owns(b, 'slot_id'))) {
      if (boxes.some(b => !ids.includes(b.slot_id)) || new Set(boxes.map(b => b.slot_id)).size !== count) {
        throw new Error(`Q${number} 作答框槽编号缺失或重复`)
      }
    } else {
      boxes.forEach((box, index) => { box.slot_id = ids[index] })
    }
    question.slot_ids = [...ids]
    schema.slot_ids = [...ids]
    // Arrays preserve multiline expressions and blank drawing positions exactly.
    question.answer_slots_text = [...slots]
    for (const box of boxes) {
      const index = ids.indexOf(box.slot_id)
      box.region_kind = question.region_kinds?.[index] || 'answer'
      if (box.region_kind === 'drawing') {
        const drawing = (page.drawingBoxes || []).find(b => String(b.question_number) === number && same(b.bbox, box.bbox))
        if (drawing) drawing.slot_id = box.slot_id
      }
    }
    if (!explicit) page.dirty = true
  }
}

export function templateSlotBoxes(page) {
  return (page.answerBoxes || []).filter(box => box.slot_id).map(box => {
    if (box.region_kind !== 'drawing') return box
    return (page.drawingBoxes || []).find(d => d.slot_id === box.slot_id && String(d.question_number) === String(box.question_number)) || box
  })
}

/** Swap physical positions, not IDs, answers, kinds or scores. */
export function swapTemplateSlotGeometry(page, first, second) {
  if (first === second) return false
  const boxes = templateSlotBoxes(page)
  const a = boxes.find(b => b.slot_id === first)
  const b = boxes.find(b => b.slot_id === second)
  if (!a || !b || String(a.question_number) !== String(b.question_number)) throw new Error('只能调换同一题的两个槽位')
  const originals = (page.answerBoxes || []).filter(box => [first, second].includes(box.slot_id))
  if (new Set(originals.map(box => box.region_kind || 'answer')).size !== 1) throw new Error('文字槽与作图槽不能互换；请调整框坐标并核对作图区域')
  const old = [...a.bbox]
  a.bbox = [...b.bbox]
  b.bbox = old
  for (const box of originals) box.bbox = [...(box.slot_id === first ? a.bbox : b.bbox)]
  page.dirty = true
  return true
}

/** Keep all physical slots (including drawing) in schema and region wire order. */
export function serializeTemplateSlots(page, questions, schema) {
  const boxes = templateSlotBoxes(page)
  return schema.map(item => {
    if (item._slotsCleared) {
      const { _slotsCleared, ...rest } = item
      return { ...rest, slot_ids: null, region_kinds: [], blank_count: 1, answer_slots: [] }
    }
    if (!owns(item, 'slot_ids')) return item
    const q = questions.find(question => numberOf(question) === numberOf(item))
    if (!q || !templateSlotIdsValid(q) || !same(q.slot_ids, item.slot_ids)) throw new Error(`Q${numberOf(item)} 槽合同不一致`)
    const bound = item.slot_ids.map(id => boxes.find(box => box.slot_id === id && String(box.question_number) === numberOf(item)))
    const unboxed = item.slot_ids.filter((_, index) => !Array.isArray(bound[index]?.bbox) || bound[index].bbox.length !== 4)
    if (unboxed.length) throw new Error(`${unboxed.map(id => `Q${id}`).join('、')} 还没有框选位置：请在“作答槽顺序”中点该编号后框出位置，或删除该作答位`)
    return { ...item, slot_ids: [...q.slot_ids], regions: bound.map(box => [...box.bbox]),
      ...(owns(q, 'region_kinds') ? { region_kinds: [...q.region_kinds] } : {}),
      blank_count: q.blank_count, answer_slots: templateFillAnswerSlots(q),
      blank_scores: [...(q.blank_scores || [])], subquestion_count: q.subquestion_count || 1,
      subquestion_scores: [...(q.subquestion_scores || [])],
    }
  })
}

export function templateSlotStructureLocked(page, number) {
  return (page.questionSchema || []).some(q => numberOf(q) === String(number)
    && (owns(q, 'slot_ids') || owns(q, 'region_kinds')))
    || (page.answerBoxes || []).filter(b => String(b.question_number) === String(number)).length > 1
}

function updateSlotStructure(page, question) {
  const schema = page.questionSchema.find(q => numberOf(q) === numberOf(question))
  question.blank_count = question.slot_ids.length
  // 自动调整: parallel arrays track the slot structure. Pad/trim against the
  // structural truth (slot order + box kinds) so a teacher edit never leaves
  // the contract half-aligned — compatibility, not rejection. An empty score
  // array means "channel off" and must stay empty (never create a 0-score
  // channel that would override real scores); answer texts always align.
  const size = question.slot_ids.length
  for (const key of ['answer_slots_text', 'blank_scores', 'subquestion_scores']) {
    const values = question[key]
    if (!Array.isArray(values) || (!values.length && key !== 'answer_slots_text')) continue
    while (values.length > size) values.pop()
    while (values.length < size) values.push(key === 'answer_slots_text' ? '' : 0)
  }
  if (Array.isArray(question.region_kinds)) {
    const kinds = question.region_kinds
    while (kinds.length > size) kinds.pop()
    while (kinds.length < size) {
      const id = question.slot_ids[kinds.length]
      const box = (page.answerBoxes || []).find(b => b.slot_id === id && String(b.question_number) === numberOf(question))
      kinds.push(box?.region_kind || 'answer')
    }
  }
  // Directory leaves score by answer regions (blank_scores); building a
  // subquestion layer on them violates the leaf contract (不能另建小问层级).
  if (question.question_type === 'solve' && !question.region_kinds?.includes('drawing') && !question.directory_node_id) question.subquestion_count = question.slot_ids.length
  if (question.region_kinds) {
    question.answer_kind = question.region_kinds.includes('drawing')
      ? (question.region_kinds.includes('answer') ? 'mixed' : 'drawing') : 'non_graphic'
    // The last drawing position is gone, so the standard figure has nothing
    // to describe; drop it as 作图属性 → 非作图 does (server rejects it).
    if (question.answer_kind === 'non_graphic' && question.drawing_reference) {
      delete question.drawing_reference
      question._drawingReferenceDirty = false
    }
    if (question.answer_kind === 'non_graphic') {
      // No drawing position left: its Q marker and drawing geometry go too.
      page.drawingLabels = (page.drawingLabels || []).filter(label => String(label.question_number) !== numberOf(question))
      if (schema) Object.assign(schema, { drawing_regions: [], label_anchor: null })
    }
    // A drawing position scores with the whole question, never per blank —
    // the same rule the editor applies when it loads a drawing question.
    if (question.region_kinds.includes('drawing')) {
      question.blank_scores = []
      question.fill_scoring_mode = 'equal'
    }
  }
  question.answer_slots = [...question.answer_slots_text]
  if (!question.region_kinds?.includes('drawing')) question.standard_answer = question.answer_slots.join('; ')
  Object.assign(schema, { slot_ids: [...question.slot_ids], blank_count: question.blank_count, answer_kind: question.answer_kind,
    ...(question.region_kinds ? { region_kinds: [...question.region_kinds] } : {}) })
  page.dirty = true
}

export function addTemplateSlot(page, question, bbox, kind = 'answer') {
  if (!question?.slot_ids) throw new Error('请先核对已有槽编号')
  if (question.slot_ids.length >= 20) throw new Error('每题最多20个作答槽')
  const next = Math.max(question._nextSlotIndex || 1, ...question.slot_ids.map(id => Number(id.slice(id.lastIndexOf('[') + 1, -1)) + 1))
  const id = `${numberOf(question)}[${next}]`
  question._nextSlotIndex = next + 1
  question.answer_slots_text = templateFillAnswerSlots(question)
  question.slot_ids.push(id)
  question.answer_slots_text.push('')
  if (question.region_kinds) question.region_kinds.push(kind)
  // 自动调整: typing a slot makes the marks representable — derive every
  // existing mark from its physical box (never guess identity from position).
  else question.region_kinds = question.slot_ids.map((id, index) => {
    if (index === question.slot_ids.length - 1) return kind
    const box = (page.answerBoxes || []).find(b => b.slot_id === id && String(b.question_number) === numberOf(question))
    return box?.region_kind || (['drawing', 'mixed'].includes(question.answer_kind) ? 'drawing' : 'answer')
  })
  for (const key of ['blank_scores', 'subquestion_scores']) if (question[key]?.length) question[key].push(0)
  const box = { question_number: numberOf(question), slot_id: id, region_kind: kind, bbox: [...bbox] }
  page.answerBoxes.push(box)
  if (kind === 'drawing') page.drawingBoxes.push({ ...box, bbox: [...bbox] })
  updateSlotStructure(page, question)
  return kind === 'drawing' ? page.drawingBoxes.at(-1) : box
}

/**
 * 非作图 on a question whose only positions are drawing areas: the teacher
 * reclassified the areas, not deleted them, so they become text answer boxes.
 */
export function convertTemplateDrawingSlots(page, question) {
  const number = numberOf(question)
  question.answer_slots_text = templateFillAnswerSlots(question)
  question.slot_ids.forEach((id, index) => {
    if (question.region_kinds?.[index] !== 'drawing') return
    question.region_kinds[index] = 'answer'
    const box = (page.answerBoxes || []).find(b => b.slot_id === id && String(b.question_number) === number)
    if (box) box.region_kind = 'answer'
  })
  page.drawingBoxes = (page.drawingBoxes || []).filter(box => String(box.question_number) !== number)
  updateSlotStructure(page, question)
}

/**
 * The teacher deleted the question's last position, so its slot numbers, kinds
 * and drawing marker go with it. The question itself (stem, answer text)
 * stays as one whole-question item; deleting it is a 题目管理 action.
 */
export function clearTemplateSlots(page, question) {
  const number = numberOf(question)
  const schema = (page.questionSchema || []).find(q => numberOf(q) === number)
  page.answerBoxes = (page.answerBoxes || []).filter(box => !(String(box.question_number) === number && box.slot_id))
  page.drawingBoxes = (page.drawingBoxes || []).filter(box => String(box.question_number) !== number)
  page.drawingLabels = (page.drawingLabels || []).filter(label => String(label.question_number) !== number)
  for (const target of [question, schema].filter(Boolean)) {
    // _nextSlotIndex survives: a new box never reuses a deleted slot number
    // (a Max report on the old number must not attach to the new box).
    for (const key of ['slot_ids', 'region_kinds']) delete target[key]
    Object.assign(target, { blank_count: 1, answer_kind: 'non_graphic', drawing_regions: [], label_anchor: null })
  }
  const answers = templateFillAnswerSlots({ ...question, slot_ids: [] }).filter(Boolean)
  question.answer_slots_text = []
  question.answer_slots = []
  if (answers.length) question.standard_answer = answers.join('; ')
  question.blank_scores = []
  if (Array.isArray(question.subquestion_scores) && question.subquestion_scores.length > 1) {
    question.subquestion_scores = [question.subquestion_scores.reduce((sum, value) => sum + (Number(value) || 0), 0)]
  }
  question.subquestion_count = 1
  delete question.drawing_reference
  question._drawingReferenceDirty = false
  // The server keeps unchanged keys, so a removed slot list is sent explicitly.
  if (schema) schema._slotsCleared = true
  page.dirty = true
}

export function removeTemplateSlot(page, question, slotId) {
  const index = question?.slot_ids?.indexOf(slotId) ?? -1
  if (index < 0) throw new Error('槽编号不存在')
  question._nextSlotIndex = Math.max(question._nextSlotIndex || 1, ...question.slot_ids.map(id => Number(id.slice(id.lastIndexOf('[') + 1, -1)) + 1))
  if (question.slot_ids.length <= 1) {
    question.answer_slots_text = templateFillAnswerSlots(question)
    clearTemplateSlots(page, question)
    return
  }
  question.answer_slots_text = templateFillAnswerSlots(question)
  const size = question.slot_ids.length
  for (const key of ['slot_ids', 'answer_slots_text', 'region_kinds', 'blank_scores', 'subquestion_scores']) {
    // Only per-slot arrays lose the slot's entry; a single question total
    // (directory leaf score) is not position-aligned and must survive.
    if (Array.isArray(question[key]) && question[key].length === size) question[key].splice(index, 1)
  }
  page.answerBoxes = page.answerBoxes.filter(box => box.slot_id !== slotId)
  page.drawingBoxes = (page.drawingBoxes || []).filter(box => box.slot_id !== slotId)
  updateSlotStructure(page, question)
}
