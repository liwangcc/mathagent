import assert from 'node:assert/strict'
import test from 'node:test'
import { jsonResponse } from './helpers/memoryFetch.js'
import { mountUpload, TEACHER } from './helpers/uploadPage.js'

// Saving the template editor of the real 批改 page (Upload.vue). The editor is
// opened the way a teacher finds it after a reload: the page resumes the
// unfinished template recognition from its checkpoint, reads the server's
// recognized questions and finished layout (cut-candidates) state, loads each
// page image and shows every page for review. “保存模板（全部页）” is the one
// save action: it writes each page that still needs saving (PUT
// layout-overrides, in page order) and then confirms the template (POST
// template/confirm). The tests read what was sent, the editor's warnings and
// what it shows the teacher.

const TEMPLATE = 'tpl-1'
const LAYOUT = (id = TEMPLATE) => `PUT /api/batch-grade/template/${id}/layout-overrides`
const QUESTION_EDIT = (id = TEMPLATE) => `PUT /api/batch-grade/template/${id}/questions`
const CONFIRM = 'POST /api/batch-grade/template/confirm'
const CORRECTIONS = 'POST /api/batch-grade/template/correction-candidates'
const SAVE = '保存模板（全部页）'

const clone = value => JSON.parse(JSON.stringify(value))
const routeOf = config => `${String(config.method).toUpperCase()} /api${String(config.url).split('?')[0]}`

function deferred() {
  let resolve
  const promise = new Promise((yes) => { resolve = yes })
  return { promise, resolve }
}

// A teacher-drawn standard figure inside the default drawing area.
function standardFigure(overrides = {}) {
  return {
    version: 'drawing-reference-v1', coordinate_space: 'normalized_1000_v1', comparison_mode: 'fixed',
    variants: [{ strokes: [{ kind: 'point', point: [300, 400] }] }], context_regions: [], teacher_confirmed: true,
    ...overrides,
  }
}

// One template page as the server reports it: its recognized question (with
// the teacher-facing answer) and the model's layout for it. By default the
// question is a 作图题 whose drawing area sits at [100, 200, 600, 700] on a
// single-column page, and the model asks for the page to be reviewed (which
// the editor treats as an unsaved decision of the teacher).
function templatePage(index, { review = true, question = {}, schema = {}, cut = {} } = {}) {
  const number = String(question.question_no ?? index + 1)
  return {
    review,
    questions: [{
      question_no: number, question_type: 'solve',
      question_text: 'teacher problem', standard_answer: 'teacher answer',
      answer_kind: 'drawing', drawing_regions: [[100, 200, 600, 700]], label_anchor: [90, 250],
      drawing_reference: standardFigure(),
      ...question,
    }],
    cut: {
      page_index: index,
      page_label: `page_${index + 1}`,
      anchor_crop_plan: { parameters: { columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }], column_count: 1 }, blocks: [] },
      question_anchors: [{ question_number: number, question_number_bbox: [40, 200, 90, 240] }],
      question_schema: [{
        question_number: number, question_type: question.question_type ?? 'solve',
        answer_kind: 'drawing', drawing_regions: [[100, 200, 600, 700]], label_anchor: [90, 250],
        ...schema,
      }],
      template_answer_regions: [],
      ...cut,
    },
  }
}

// A non-drawing question with one answer box, for pages that need no figure.
function textPage(index, { question = {}, ...options } = {}) {
  return templatePage(index, {
    ...options,
    question: { answer_kind: 'non_graphic', drawing_regions: [], label_anchor: null, drawing_reference: null, ...question },
    schema: { answer_kind: 'non_graphic', drawing_regions: [], label_anchor: null, ...options.schema },
    cut: { template_answer_regions: [{ question_number: String(question.question_no ?? index + 1), regions: [[100, 300, 500, 360]] }], ...options.cut },
  })
}

// What the server keeps for one template: its recognition result, its layout
// state and the page images.
function templateRoutes(id, pages) {
  const recognition = {
    status: 'ocr_done', template_id: id, category: 'daily', template_mode: 'blank', column_mode: 'single',
    pages: pages.map(page => ({ page_index: page.cut.page_index, questions: page.questions })),
  }
  const cut = {
    status: 'complete', template_id: id,
    geometry_review_pending_pages: pages.filter(page => page.review).map(page => page.cut.page_index),
    pages: pages.map(page => page.cut),
  }
  return {
    responses: Object.fromEntries(pages.map(page => [
      `GET /api/batch-grade/template/${id}/pages/${page.cut.page_index}/image`,
      () => ({ status: 200, data: new Blob(['png'], { type: 'image/png' }) }),
    ])),
    streams: {
      [`GET /api/batch-grade/template/${id}/recognition`]: () => jsonResponse(clone(recognition)),
      [`GET /api/batch-grade/template/${id}/cut-candidates`]: () => jsonResponse(clone(cut)),
    },
  }
}

// The layout service's receipt for a page write: the page as saved.
function receipt(body, extra = {}, id = TEMPLATE) {
  return { status: 200, data: { success: true, template_id: id, ...clone(body), ...extra } }
}

async function openTemplateEditor(t, {
  pages = [templatePage(0), templatePage(1)],
  others = {},
  layout = body => receipt(body),
  confirm = () => ({ status: 200, data: { success: true, page_count: pages.length, template_mode: 'blank', column_mode: 'single' } }),
  corrections = () => ({ status: 200, data: { candidates: [] } }),
  questionEdit = () => ({ status: 200, data: { success: true, pages: [] } }),
  messageBox,
} = {}) {
  const served = [templateRoutes(TEMPLATE, pages),
    ...Object.entries(others).map(([id, otherPages]) => templateRoutes(id, otherPages))]
  // Whether the editor showed its save lock when each write reached the server.
  const locks = []
  const lockShown = () => upload.page.root.find(el => el.tagName === 'button' && el.text === '保存中...') !== null
  const upload = await mountUpload(t, {
    localStorage: {
      [`mathagent_template_recognition:${TEACHER.id}`]: JSON.stringify({ template_id: TEMPLATE, category: 'daily', template_mode: 'blank', column_mode: 'single' }),
    },
    responses: {
      ...Object.assign({}, ...served.map(item => item.responses)),
      [LAYOUT()]: (config) => {
        const body = JSON.parse(config.data)
        locks.push({ stage: 'layout', page: body.page_index, locked: lockShown() })
        return layout(body, config)
      },
      [QUESTION_EDIT()]: config => questionEdit(JSON.parse(config.data)),
      [CONFIRM]: (config) => {
        locks.push({ stage: 'confirm', locked: lockShown() })
        return confirm(JSON.parse(config.data))
      },
      [CORRECTIONS]: config => corrections(JSON.parse(config.data)),
    },
    streams: Object.assign({}, ...served.map(item => item.streams)),
    messageBox,
  })
  const { page, client } = upload
  // The page images finish loading, as a browser would report it (also for an
  // editor that appears later, such as a question turned into 作图题).
  const loadImages = async () => {
    for (const img of page.root.findAll(el => el.tagName === 'img' && el.getAttribute('alt') === '教师模板原图')) {
      await img.dispatch('load')
    }
    await upload.settle()
  }
  await loadImages()
  assert.equal(page.errors.length, 0, String(page.errors[0]?.err?.stack))
  const mark = { requests: client.requests.length, messages: client.messages.length }
  const dialog = () => page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板')
  const editor = {
    upload,
    setup: page.setup,
    root: page.root,
    locks,
    loadImages,
    // Requests and toasts since the editor opened.
    sent: () => client.requests.slice(mark.requests).map(routeOf),
    bodies: route => client.requests.slice(mark.requests).filter(config => routeOf(config) === route).map(config => JSON.parse(config.data)),
    notices: type => client.messages.slice(mark.messages).filter(message => !type || message.type === type),
    lastWarning: () => client.messages.slice(mark.messages).filter(message => message.type === 'warning').at(-1)?.message ?? '',
    dialogOpen: () => dialog()?.attributes['data-open'] === 'true',
    saveButton: () => upload.button(SAVE) ?? upload.button('保存中...'),
    save: () => upload.click(SAVE),
    article: index => page.root.find(el => el.id === `template-reference-page-${index}`),
    // The page's “已调整 · 随模板保存” / “请核对本页定位” tag, '' when it is saved.
    pageTag: index => editor.article(index)?.byClass('tag-pending')?.text ?? '',
    pageAlert: index => editor.article(index)?.find(el => el.classList.contains('tpl-layout-review-alert') && el.getAttribute('role') === 'alert')?.text ?? '',
    // The answer editor card of a question (page position, question position).
    card: (pageIndex, questionIndex = 0) => page.root.find(el => el.id === `template-question-${pageIndex}-${questionIndex}`),
    question: (pageIndex, questionIndex = 0) => page.setup.templatePages[pageIndex].questions[questionIndex],
    previewPage: index => page.setup.templateCutPreviewPages[index],
    reviewRows: label => page.root.find(el => el.tagName === 'section' && el.getAttribute('aria-label') === label)?.findAll(el => el.tagName === 'strong').map(el => el.text) ?? [],
  }
  return editor
}

// A change as a browser delivers it: the control takes the value, then
// v-model's listener (registered when the element is created) runs before the
// element's own @change / @input handler. (helpers/memoryDom.js's dispatch runs
// `on*` handlers first, so the order is spelled out here.) A disabled control
// cannot be changed, as with upload.select / upload.type.
async function edit(editor, el, value, type = el.tagName === 'select' ? 'change' : 'input') {
  assert.ok(el, 'edit: no such control on the page')
  if (el.disabled === true || el.hasAttribute('disabled') || el.hasAttribute('loading')) {
    throw new Error(`edit: the ${el.tagName} is disabled`)
  }
  if (el.tagName === 'select') {
    for (const option of el.options) option.selected = String(option._value ?? option.value) === String(value)
  }
  el.value = String(value)
  const event = { type, target: el, currentTarget: el, preventDefault() {}, stopPropagation() {} }
  for (const listener of el.listeners.get(type) ?? []) listener(event)
  await Promise.all([el.handlers[type]].flat().filter(Boolean).map(handler => handler(event)))
  await editor.upload.settle()
}

const textareaOf = (card, placeholder) => card.find(el => el.tagName === 'textarea' && el.getAttribute('placeholder') === placeholder)
const drawingKindSelect = (card, number) => card.find(el => el.tagName === 'select' && el.getAttribute('aria-label') === `第 ${number} 题作图属性`)

// Select a geometry item from the legend beside the page image, then press
// “删除所选”, as a teacher deletes a box.
async function deleteGeometry(editor, pageIndex, label) {
  const article = editor.article(pageIndex)
  const legend = article.find(el => el.tagName === 'section' && el.getAttribute('aria-label') === '图外题号与作答框对应表')
  const item = legend?.find(el => el.tagName === 'button' && el.text === label)
  assert.ok(item, `legend item ${label}: ${legend?.text}`)
  await item.dispatch('click')
  await editor.upload.settle()
  await article.find(el => el.tagName === 'button' && el.text === '删除所选').dispatch('click')
  await editor.upload.settle()
}

// The standard-figure canvas of a page, laid out as a 1000 × 1000 px box.
function figureCanvas(editor, pageIndex) {
  const svg = editor.article(pageIndex).find(el => el.tagName === 'svg' && String(el.getAttribute('aria-label')).startsWith('教师标准图画布'))
  svg.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 1000, bottom: 1000, width: 1000, height: 1000 })
  svg.setPointerCapture = () => {}
  return svg
}

async function pickFigureTool(editor, pageIndex, label) {
  const tool = editor.article(pageIndex).find(el => el.tagName === 'button' && el.text === label && el.parentNode?.classList.contains('drawing-reference-toolbar'))
  assert.ok(tool, `figure tool ${label}`)
  await tool.dispatch('click')
  await editor.upload.settle()
}

// “末尾追加题目” in a page's 题目管理: renumbering first writes every page that
// still needs saving, one page save at a time, then sends the question edit.
// The prompt for the new number is answered with 9.
const APPEND = '末尾追加题目'
const answerPrompt = box => (box.kind === 'prompt' ? { value: '9' } : 'confirm')

test('page save predicate includes unsaved edits and server-only review, not clean pages', async (t) => {
  // A page the model and the teacher are both done with is not written again.
  const clean = await openTemplateEditor(t, { pages: [templatePage(0, { review: false })] })
  assert.equal(clean.pageTag(0), '')
  await clean.save()
  assert.deepEqual(clean.sent(), [CONFIRM])
  clean.upload.unmount()

  // Every way the server can still hold a page open for review, and an
  // unsaved teacher edit, makes the save write that page first. Most of them
  // also make the page an unsaved decision when it opens (已调整); a pending
  // diagnostic alone only asks the teacher to check the page (请核对本页定位).
  const unsaved = '已调整 · 随模板保存'
  const pending = {
    'listed for geometry review': { tag: unsaved, pages: [templatePage(0, { review: true })] },
    'layout review on the page': { tag: unsaved, pages: [templatePage(0, { review: false, cut: { layout_review_pending: true } })] },
    'anchor review on the page': { tag: unsaved, pages: [templatePage(0, { review: false, cut: { anchor_review_pending: true } })] },
    'geometry review on the page': { tag: unsaved, pages: [templatePage(0, { review: false, cut: { geometry_review_pending: true } })] },
    'geometry review of the question cut': { tag: unsaved, pages: [templatePage(0, { review: false, cut: { modes: { question: { geometry_review_pending: true } } } })] },
    'a diagnostic still awaiting geometry review': { tag: '请核对本页定位', pages: [templatePage(0, { review: false, cut: { anchor_diagnostics: { geometry_review_pending: true, review_reason: 'column_identity_conflict' } } })] },
  }
  for (const [signal, { tag, ...options }] of Object.entries(pending)) {
    const editor = await openTemplateEditor(t, options)
    assert.equal(editor.pageTag(0), tag, signal)
    await editor.save()
    assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM], signal)
    assert.equal(editor.dialogOpen(), false, signal)
    editor.upload.unmount()
  }

  // Only the server's review list still holds a page that has no unsaved
  // edit: a question edit's receipt lists the clean second page for geometry
  // review again without sending that page's layout. The page asks to be
  // checked, and the save writes it (and only it) before confirming.
  const listed = await openTemplateEditor(t, {
    pages: [templatePage(0, { review: false }), templatePage(1, { review: false })],
    messageBox: answerPrompt,
    questionEdit: () => ({ status: 200, data: { success: true, pages: [], page_artifacts: {}, geometry_review_pending_pages: [1] } }),
  })
  assert.deepEqual([0, 1].map(listed.pageTag), ['', ''])
  await listed.upload.click(APPEND)
  assert.deepEqual(listed.sent(), [QUESTION_EDIT()])
  assert.deepEqual([0, 1].map(listed.pageTag), ['', '请核对本页定位'])
  await listed.save()
  assert.deepEqual(listed.sent(), [QUESTION_EDIT(), LAYOUT(), CONFIRM])
  assert.deepEqual(listed.bodies(LAYOUT()).map(body => body.page_index), [1])
  assert.equal(listed.dialogOpen(), false)
  listed.upload.unmount()

  const edited = await openTemplateEditor(t, { pages: [templatePage(0, { review: false })] })
  await deleteGeometry(edited, 0, 'Q1 作图锚点 1')
  assert.equal(edited.pageTag(0), unsaved)
  await edited.save()
  assert.deepEqual(edited.sent(), [LAYOUT(), CONFIRM])
})

test('a generated draft stays editable while missing answers block final confirmation', async (t) => {
  const slots = { slot_ids: ['1[1]', '1[2]', '1[3]'], region_kinds: ['answer', 'answer', 'answer'] }
  const editor = await openTemplateEditor(t, { pages: [textPage(0, {
    question: { question_type: 'fill', blank_count: 3, answer_slots: ['6', '', ''], standard_answer: '6', ...slots },
    schema: { question_type: 'fill', ...slots },
    cut: { template_answer_regions: [{ question_number: '1', regions: [[100, 300, 200, 360], [250, 300, 350, 360], [400, 300, 500, 360]], slot_ids: slots.slot_ids }] },
  })] })
  assert.match(editor.reviewRows('待补充标准答案').join('\n'), /第 1 题有 2 个作答区域未设置标准答案/)
  await editor.save()
  assert.equal(editor.dialogOpen(), true)
  assert.deepEqual(editor.sent(), [])
  assert.match(editor.lastWarning(), /第 1 题有 2 个作答区域未设置标准答案/)
  // The teacher fills the two empty positions in the answer card.
  const card = editor.card(0)
  for (const [slot, value] of [[2, '7'], [3, '8']]) {
    const input = card.find(el => el.tagName === 'input' && el.getAttribute('aria-label') === `第 1 题第 ${slot} 空答案`)
    await edit(editor, input, value, 'change')
  }
  assert.deepEqual(editor.reviewRows('待补充标准答案'), [])
  assert.deepEqual(editor.reviewRows('保存前需要调整'), [])
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.deepEqual(editor.bodies(CONFIRM)[0].pages[0].questions[0].answer_slots, ['6', '7', '8'])
})

test('one save attempt names every blocker together and writes nothing', async (t) => {
  // A model box over a quarter of the page, a missing stem and a Max report.
  const editor = await openTemplateEditor(t, { pages: [templatePage(0, {
    question: { question_text: '', max_flags: [{ status: 'extra', reason: '合成' }] },
    schema: { drawing_regions: [[40, 60, 900, 700]] },
  })] })
  const problems = editor.reviewRows('保存前需要调整')
  assert.equal(problems.length, 2)
  assert.match(problems[0], /Q1 的画图区超过整页的四分之一/)
  assert.match(problems[1], /第 1 页第 1 题缺少题干/)
  // The oversized box itself is marked on the page image; the stem has no box.
  const marked = editor.article(0).findAll(el => el.classList.contains('is-missing-answer'))
  assert.deepEqual(marked.map(el => el.className.split(' ')[0]), ['tpl-drawing-region'])
  assert.match(editor.reviewRows('Max 判定待核对').join('\n'), /Max 判定整题为目录多余项/)

  await editor.save()
  assert.deepEqual(editor.sent(), [])
  assert.equal(editor.notices().length, 1)
  const text = editor.lastWarning()
  assert.match(text, /^保存前还有 3 处需要处理/)
  for (const part of [/Max 判定整题为目录多余项/, /Q1 的画图区超过整页的四分之一/, /第 1 页第 1 题缺少题干/]) assert.match(text, part)

  // Fixing all three in the editor lets the very next save go through: the
  // box is dragged back to its question's area (what a resize gesture leaves),
  // the stem typed in and the Max report kept.
  editor.previewPage(0).drawingBoxes[0].bbox = [100, 200, 600, 700]
  await editor.upload.settle()
  await edit(editor, textareaOf(editor.card(0), '请输入题干'), 'teacher problem')
  await editor.root.find(el => el.tagName === 'button' && el.text === '确认保留').dispatch('click')
  await editor.upload.settle()
  assert.deepEqual(editor.reviewRows('保存前需要调整'), [])
  assert.deepEqual(editor.reviewRows('Max 判定待核对'), [])
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.equal(editor.dialogOpen(), false)
})

test('a question due for removal raises no geometry conflict before it leaves', async (t) => {
  // Q1 and Q2 are plain questions with one answer box each, on a page the
  // model asked to review.
  const page = textPage(0)
  page.questions.push({ ...clone(page.questions[0]), question_no: '2' })
  page.cut.question_anchors.push({ question_number: '2', question_number_bbox: [40, 400, 90, 440] })
  page.cut.question_schema.push({ ...page.cut.question_schema[0], question_number: '2' })
  page.cut.template_answer_regions.push({ question_number: '2', regions: [[100, 500, 500, 560]] })
  const editor = await openTemplateEditor(t, { pages: [page] })

  // Turning a question with boxes into a 作图题 asks for its drawing area...
  await edit(editor, drawingKindSelect(editor.card(0, 1), '2'), 'drawing')
  assert.match(editor.pageAlert(0), /Q2 是作图题，但还没有画图区/)
  await edit(editor, drawingKindSelect(editor.card(0, 1), '2'), 'non_graphic')
  assert.equal(editor.pageAlert(0), '')

  // ...but the teacher deleted every box of Q1, then switched it to 作图题: the
  // save removes Q1 first, so its missing drawing area must not block the save.
  await deleteGeometry(editor, 0, 'Q1 作答框 1')
  assert.match(editor.reviewRows('保存时删除的题目').join('\n'), /第 1 题的作答框和画图区已全部删除/)
  await edit(editor, drawingKindSelect(editor.card(0, 0), '1'), 'drawing')
  assert.equal(editor.pageAlert(0), '')
  await editor.loadImages() // Q1's 作图 editor shows the template image
  await editor.save()
  assert.doesNotMatch(editor.lastWarning(), /还没有画图区/)
  assert.deepEqual(editor.sent(), [QUESTION_EDIT(), LAYOUT(), CONFIRM])
  assert.deepEqual(editor.bodies(QUESTION_EDIT())[0], { page_index: 0, action: 'delete', question_number: '1' })
  assert.deepEqual(editor.bodies(CONFIRM)[0].pages[0].questions.map(question => question.question_no), ['2'])
})

test('a kept position still waiting for its box is named with the other blockers', async (t) => {
  // The model kept two positions for Q1 but boxed only the first one.
  const slots = { slot_ids: ['1[1]', '1[2]'], region_kinds: ['answer', 'answer'] }
  const editor = await openTemplateEditor(t, { pages: [textPage(0, {
    question: { question_type: 'fill', question_text: '', blank_count: 2, answer_slots: ['3', '4'], standard_answer: '3; 4', ...slots },
    schema: { question_type: 'fill', ...slots },
    cut: { template_answer_regions: [{ question_number: '1', regions: [[100, 200, 300, 260]], slot_ids: ['1[1]'] }] },
  })] })
  assert.ok(editor.article(0).find(el => el.tagName === 'button' && el.text === 'Q1[2] 待框选'))
  await editor.save()
  assert.deepEqual(editor.sent(), [])
  assert.match(editor.lastWarning(), /Q1\[2\] 还没有框选位置/)
  assert.match(editor.lastWarning(), /第 1 页第 1 题缺少题干/)
})

test('a standard figure outside its drawing area is named before anything is saved', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [templatePage(0, {
    question: { drawing_reference: standardFigure({ variants: [{ strokes: [{ kind: 'polyline', points: [[150, 250], [800, 250]] }] }] }) },
  })] })
  await editor.save()
  assert.deepEqual(editor.sent(), [])
  assert.match(editor.lastWarning(), /第 1 页第 1 题：标准图有笔画画在画图区外/)
})

test('model geometry conflicts are shown to the teacher before any layout request', async (t) => {
  const editor = await openTemplateEditor(t, {
    pages: [templatePage(0, {
      question: { question_no: '5', answer_kind: 'non_graphic', standard_answer: '山顶1200m', drawing_regions: [], label_anchor: null, drawing_reference: null },
      schema: { answer_kind: 'non_graphic', drawing_regions: [[120, 180, 520, 600]], label_anchor: [90, 220] },
    })],
    messageBox: answerPrompt,
  })
  // The page shows the conflict where the teacher reviews it.
  assert.match(editor.pageAlert(0), /Q5 是非作图题.*画图区或作图题号/)
  // A page save (here before renumbering) refuses it without writing and keeps the draft.
  await editor.upload.click(APPEND)
  assert.deepEqual(editor.sent(), [])
  assert.equal(textareaOf(editor.card(0), '请输入标准答案').value, '山顶1200m')
  assert.equal(editor.pageTag(0), '已调整 · 随模板保存')
  assert.match(editor.lastWarning(), /Q5 是非作图题.*修改已保留/)
  // So does the final save.
  await editor.save()
  assert.deepEqual(editor.sent(), [])
  assert.match(editor.lastWarning(), /第 1 页：Q5 是非作图题.*修改已保留/)
  assert.equal(editor.dialogOpen(), true)

  // The answer card's 作图属性 offers 作图题: choosing it adopts the drawing
  // area and the conflict is gone, so the page can be saved.
  const kind = drawingKindSelect(editor.card(0), '5')
  assert.deepEqual(kind.options.map(option => [option.value, option.text]), [['non_graphic', '非作图'], ['drawing', '作图题'], ['mixed', '图文混合']])
  await edit(editor, kind, 'drawing')
  assert.equal(kind.value, 'drawing')
  assert.equal(editor.pageAlert(0), '')
  await editor.loadImages() // Q5's 作图 editor shows the template image
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  const [schema] = editor.bodies(LAYOUT())[0].question_schema
  assert.equal(schema.answer_kind, 'drawing')
  assert.deepEqual(schema.drawing_regions, [[120, 180, 520, 600]])
  assert.equal(editor.bodies(CONFIRM)[0].pages[0].questions[0].standard_answer, '山顶1200m')
})

test('a graphic candidate without complete geometry stays unresolved until the teacher fixes it', async (t) => {
  const editor = await openTemplateEditor(t, {
    pages: [templatePage(0, {
      question: { question_no: '6', drawing_regions: [], label_anchor: null, drawing_reference: null },
      schema: { drawing_regions: [], label_anchor: null },
    })],
    messageBox: answerPrompt,
  })
  assert.match(editor.pageAlert(0), /Q6 是作图题，但还没有画图区.*绿色画图区/)
  await editor.upload.click(APPEND)
  assert.deepEqual(editor.sent(), [])
  assert.match(editor.lastWarning(), /Q6 是作图题，但还没有画图区/)
  await editor.save()
  assert.deepEqual(editor.sent(), [])
  assert.equal(editor.pageTag(0), '已调整 · 随模板保存')
})

test('one final action saves every dirty page and current answers without extra model or correction requests', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [templatePage(0), templatePage(1, { review: false }), templatePage(2)] })
  assert.deepEqual([0, 1, 2].map(editor.pageTag), ['已调整 · 随模板保存', '', '已调整 · 随模板保存'])
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), LAYOUT(), CONFIRM])
  assert.deepEqual(editor.bodies(LAYOUT()).map(body => body.page_index), [0, 2])
  assert.deepEqual(editor.locks, [
    { stage: 'layout', page: 0, locked: true },
    { stage: 'layout', page: 2, locked: true },
    { stage: 'confirm', locked: true },
  ])
  assert.deepEqual(editor.bodies(CONFIRM)[0].pages.map(page => page.questions[0].standard_answer), ['teacher answer', 'teacher answer', 'teacher answer'])
  assert.ok(editor.setup.templateCutPreviewPages.every(page => page.dirty === false))
  assert.equal(editor.setup.confirmingTemplate, false)
  assert.equal(editor.dialogOpen(), false)
  assert.deepEqual(editor.notices('success').map(message => message.message), ['本次模板已保存（3 页），开始识别学生试卷前仍可再次编辑'])
})

test('a failed page aborts final confirmation and preserves all unsaved drafts for retry', async (t) => {
  let failing = true
  const editor = await openTemplateEditor(t, {
    pages: [templatePage(0), templatePage(1), templatePage(2)],
    layout: body => (body.page_index === 1 && failing ? { status: 503, data: { detail: 'temporary unavailable' } } : receipt(body)),
  })
  const draft = editor.question(1)
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), LAYOUT()])
  assert.deepEqual([0, 1, 2].map(editor.pageTag), ['', '已调整 · 随模板保存', '已调整 · 随模板保存'])
  assert.equal(editor.question(1), draft)
  assert.equal(textareaOf(editor.card(1), '请输入标准答案').value, 'teacher answer')
  assert.equal(editor.dialogOpen(), true)
  assert.equal(editor.saveButton().text, SAVE)
  assert.match(editor.notices().at(-1).message, /第 2 页.*修改已保留/)

  failing = false
  const before = editor.sent().length
  await editor.save()
  assert.deepEqual(editor.sent().slice(before), [LAYOUT(), LAYOUT(), CONFIRM])
  assert.deepEqual(editor.bodies(LAYOUT()).slice(2).map(body => body.page_index), [1, 2])
  assert.equal(editor.dialogOpen(), false)
})

test('persisting reviewed geometry does not clear the teacher standard-figure checkbox', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [templatePage(0)] })
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.equal(editor.bodies(CONFIRM)[0].pages[0].questions[0].drawing_reference.teacher_confirmed, true)
})

test('ordinary layout responses preserve the teacher answer type and choice mode instead of replaying the old OCR type', async (t) => {
  // The model read a single-choice question; its layout carries that old type.
  const ocr = { question_type: 'choice', choice_mode: 'single' }
  const edits = {
    // The teacher turns it into a fill-in question and types its answer...
    fill: async (editor) => {
      const card = editor.card(0)
      await edit(editor, card.find(el => el.tagName === 'select' && el.classList.contains('tpl-q-type')), 'fill')
      await edit(editor, textareaOf(editor.card(0), '请输入标准答案'), '12')
      return { question_type: 'fill', choice_mode: 'unknown', standard_answer: '12' }
    },
    // ...or keeps it a choice question that takes several answers.
    multiple: async (editor) => {
      await edit(editor, editor.card(0).find(el => el.tagName === 'select' && el.getAttribute('aria-label') === '选择题类型'), 'multiple')
      await edit(editor, textareaOf(editor.card(0), '请输入标准答案'), 'AC')
      return { question_type: 'choice', choice_mode: 'multiple', standard_answer: 'AC' }
    },
  }
  for (const [name, teacherEdit] of Object.entries(edits)) {
    const editor = await openTemplateEditor(t, {
      pages: [textPage(0, { question: { ...ocr, standard_answer: 'B' }, schema: ocr })],
      // The layout service's receipt replays the old OCR type for the question,
      // whatever the page sent.
      layout: body => receipt({ ...body, question_schema: body.question_schema.map(item => ({ ...item, ...ocr })) }),
    })
    const question = editor.question(0)
    const expected = await teacherEdit(editor)
    await editor.save()
    assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM], name)
    const confirmed = editor.bodies(CONFIRM)[0].pages[0].questions[0]
    for (const [key, value] of Object.entries(expected)) {
      assert.equal(confirmed[key], value, `${name}: ${key}`)
      assert.equal(editor.question(0)[key], value, `${name}: ${key}`)
    }
    assert.equal(editor.question(0), question, name)
    editor.upload.unmount()
  }
})

test('choice and fill drawings retain answer type through geometry save and manual/fixed final confirmation', async (t) => {
  for (const question_type of ['choice', 'fill']) {
    for (const answer_kind of ['drawing', 'mixed']) {
      for (const comparison_mode of ['manual', 'fixed']) {
        const label = `${question_type}/${answer_kind}/${comparison_mode}`
        const choice_mode = question_type === 'choice' ? 'multiple' : 'unknown'
        const reference = standardFigure({
          comparison_mode,
          variants: comparison_mode === 'fixed' ? [{ strokes: [{ kind: 'point', point: [300, 400] }] }] : [],
          teacher_confirmed: comparison_mode === 'fixed',
        })
        const editor = await openTemplateEditor(t, { pages: [templatePage(0, {
          question: { question_type, choice_mode, answer_kind, drawing_reference: reference, standard_answer: question_type === 'choice' ? 'AC' : '4', blank_count: 1, answer_slots: ['4'] },
          schema: { question_type, choice_mode, answer_kind },
        })] })
        const question = editor.question(0)
        await editor.save()
        assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM], label)
        const schema = editor.bodies(LAYOUT())[0].question_schema[0]
        const answer = editor.bodies(CONFIRM)[0].pages[0].questions[0]
        for (const item of [editor.question(0), schema, answer]) {
          assert.equal(item.question_type, question_type, label)
          assert.equal(item.choice_mode, choice_mode, label)
          assert.equal(item.answer_kind, answer_kind, label)
        }
        assert.equal(answer.standard_answer, question.standard_answer, label)
        assert.equal(editor.question(0), question, label)
        assert.deepEqual(answer.drawing_reference, reference, label)
        assert.equal(answer.drawing_reference.teacher_confirmed, comparison_mode === 'fixed', label)
        editor.upload.unmount()
      }
    }
  }
})

test('a stale page-save response cannot mutate a new session or clear the detached draft', async (t) => {
  const pending = deferred()
  const nextSession = [templatePage(0, { review: false }), templatePage(1, { review: false })]
  const editor = await openTemplateEditor(t, {
    // Only the second page still needs saving. The teacher renumbers on the
    // first page, so the write on its way belongs to the second page alone:
    // nothing but that page write may clear its saving state.
    pages: [templatePage(0, { review: false }), templatePage(1)],
    others: { 'tpl-2': nextSession },
    layout: () => pending.promise,
    messageBox: answerPrompt,
  })
  const oldPage = editor.previewPage(1)
  await editor.upload.click(APPEND)
  assert.deepEqual(editor.sent(), [LAYOUT()])
  const [payload] = editor.bodies(LAYOUT())
  assert.equal(payload.page_index, 1)
  assert.equal(oldPage.saving, true)
  // A new template session opens while that page write is still on its way.
  await editor.setup.applyTemplateRecognitionResult('tpl-2', nextSession.map(page => ({ page_index: page.cut.page_index, questions: clone(page.questions) })), 'blank', 'single')
  await editor.upload.settle()
  const replacement = editor.previewPage(1)
  assert.notEqual(replacement, oldPage)
  assert.equal(replacement.columnsSource, 'model')
  pending.resolve(receipt(payload))
  await editor.upload.settle()
  assert.equal(oldPage.dirty, true)
  assert.equal(oldPage.saving, false)
  assert.equal(editor.previewPage(1), replacement)
  assert.equal(replacement.columnsSource, 'model')
  assert.deepEqual([0, 1].map(editor.pageTag), ['', ''])
  assert.ok(!editor.sent().includes(QUESTION_EDIT()), 'the old session is not renumbered')
  assert.deepEqual(editor.notices(), [])
})

test('wrong-template and wrong-page layout receipts fail closed without consuming edits', async (t) => {
  for (const change of [{ template_id: 'another-template' }, { page_index: 7 }, { success: false }, { columns: [] }]) {
    const label = JSON.stringify(change)
    const editor = await openTemplateEditor(t, { pages: [templatePage(0)], layout: body => receipt(body, change) })
    await editor.save()
    assert.deepEqual(editor.sent(), [LAYOUT()], label)
    assert.equal(editor.pageTag(0), '已调整 · 随模板保存', label)
    assert.equal(editor.previewPage(0).saving, false, label)
    assert.equal(editor.saveButton().text, SAVE, label)
    assert.equal(editor.dialogOpen(), true, label)
    assert.match(editor.notices().at(-1).message, /第 1 页定位未保存.*本页修改已保留/, label)
    editor.upload.unmount()
  }
})

test('missing columns and concurrent page activity stop saving without making an invalid request', async (t) => {
  const cases = {
    // The model found no column layout for the page.
    'missing columns': {
      options: { pages: [templatePage(0, { cut: { anchor_crop_plan: { parameters: { columns: [] }, blocks: [] } } })] },
      warning: /第 1 页尚未设置栏数/,
    },
    // Another operation is still writing the page.
    'page still saving': {
      options: { pages: [templatePage(0)] },
      before: (editor) => { editor.previewPage(0).saving = true },
      warning: /模板页面仍在保存或生成标准图/,
    },
  }
  for (const [label, { options, before, warning }] of Object.entries(cases)) {
    const editor = await openTemplateEditor(t, options)
    before?.(editor)
    await editor.upload.settle()
    await editor.save()
    assert.deepEqual(editor.sent(), [], label)
    assert.match(editor.lastWarning(), warning, label)
    assert.equal(editor.saveButton().text, SAVE, label)
    assert.equal(editor.setup.confirmingTemplate, false, label)
    assert.equal(editor.dialogOpen(), true, label)
    editor.upload.unmount()
  }
})

test('the confirmation lock is acquired before the first await so duplicate clicks do not duplicate writes', async (t) => {
  const pending = deferred()
  const editor = await openTemplateEditor(t, { pages: [templatePage(0)], layout: () => pending.promise })
  // A double click: both clicks reach the button before the page re-renders.
  const button = editor.saveButton()
  button.dispatch('click')
  button.dispatch('click')
  await editor.upload.settle()
  assert.deepEqual(editor.sent(), [LAYOUT()])
  assert.equal(editor.saveButton().text, '保存中...')
  assert.equal(editor.saveButton().disabled, true)
  pending.resolve(receipt(editor.bodies(LAYOUT())[0]))
  await editor.upload.settle()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.equal(editor.dialogOpen(), false)
})

test('failed final confirmation keeps the editor and answers open after successful layout persistence', async (t) => {
  const editor = await openTemplateEditor(t, {
    pages: [templatePage(0)],
    confirm: () => ({ status: 500, data: { detail: 'retry confirmation' } }),
  })
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.equal(editor.pageTag(0), '')
  assert.equal(editor.dialogOpen(), true)
  assert.equal(textareaOf(editor.card(0), '请输入标准答案').value, 'teacher answer')
  assert.equal(editor.setup.confirmedTemplateId, '')
  assert.equal(editor.saveButton().text, SAVE)
  assert.ok(editor.notices('error').some(message => /retry confirmation/.test(message.message)))
})

test('missing required content points to the exact page and question instead of an invisible approval step', async (t) => {
  for (const [field, placeholder] of [['question_text', '请输入题干'], ['standard_answer', '请输入标准答案']]) {
    const editor = await openTemplateEditor(t, { pages: [textPage(0), textPage(1)] })
    // The teacher empties the field of the second page's question.
    await edit(editor, textareaOf(editor.card(1), placeholder), '')
    assert.equal(editor.question(1)[field], '', field)
    await editor.save()
    assert.deepEqual(editor.sent(), [], field)
    assert.match(editor.lastWarning(), /第 2 页第 2 题缺少/, field)
    assert.equal(editor.dialogOpen(), true, field)
    assert.equal(editor.saveButton().text, SAVE, field)
    editor.upload.unmount()
  }
})

// A click on the standard-figure canvas at page coordinates (0–1000).
const tap = (canvas, [x, y]) => canvas.dispatch('pointerdown', { button: 0, clientX: x, clientY: y, pointerId: 1 })

test('one save flushes the visible standard figure before locking and includes its last stroke', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [templatePage(0)] })
  // The teacher places the points of a polyline but does not press 完成图形.
  await pickFigureTool(editor, 0, '折线')
  const canvas = figureCanvas(editor, 0)
  await tap(canvas, [150, 250])
  await tap(canvas, [300, 400])
  await editor.upload.settle()
  assert.ok(editor.article(0).find(el => el.tagName === 'button' && el.text === '完成图形'))
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.deepEqual(editor.bodies(CONFIRM)[0].pages[0].questions[0].drawing_reference.variants, [{ strokes: [
    { kind: 'point', point: [300, 400] },
    { kind: 'polyline', points: [[150, 250], [300, 400]] },
  ] }])
})

test('an unfinished standard figure stops the save before the editor is locked', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [templatePage(0)] })
  await pickFigureTool(editor, 0, '折线')
  await tap(figureCanvas(editor, 0), [150, 250])
  await editor.upload.settle()
  await editor.save()
  assert.deepEqual(editor.sent(), [])
  assert.match(editor.lastWarning(), /第 1 页第 1 题：当前图形至少需要 2 个端点/)
  assert.equal(editor.saveButton().text, SAVE)
  assert.equal(editor.setup.confirmingTemplate, false)
  assert.equal(editor.dialogOpen(), true)
})

test('one save adopts the editor draft when the last v-model event is still pending', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [templatePage(0, { question: { drawing_reference: null } })] })
  await pickFigureTool(editor, 0, '点')
  await tap(figureCanvas(editor, 0), [500, 450])
  await editor.upload.settle()
  // The page has not taken the editor's last change yet when the save click
  // arrives in the same tick: its own copy of the figure is still empty.
  editor.question(0).drawing_reference = null
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.deepEqual(editor.bodies(CONFIRM)[0].pages[0].questions[0].drawing_reference, {
    version: 'drawing-reference-v1', coordinate_space: 'normalized_1000_v1',
    comparison_mode: 'fixed', variants: [{ strokes: [{ kind: 'point', point: [500, 450] }] }],
    context_regions: [], teacher_confirmed: false,
  })
})

test('saving after a geometry change keeps the teacher-visible answer without a model call', async (t) => {
  // The model saw two columns; the teacher sets the page to a single column.
  const twoColumns = { parameters: { columns: [{ column_index: 1, x1_1000: 0, x2_1000: 500 }, { column_index: 2, x1_1000: 500, x2_1000: 1000 }], column_count: 2 }, blocks: [] }
  const editor = await openTemplateEditor(t, { pages: [templatePage(0, { review: false, cut: { anchor_crop_plan: twoColumns } })] })
  assert.equal(editor.pageTag(0), '')
  await edit(editor, editor.article(0).find(el => el.tagName === 'select' && el.parentNode?.classList.contains('tpl-column-count')), 1)
  assert.equal(editor.pageTag(0), '已调整 · 随模板保存')
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.deepEqual(editor.bodies(LAYOUT())[0].columns, [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }])
  const [question] = editor.bodies(CONFIRM)[0].pages[0].questions
  assert.equal(question.standard_answer, 'teacher answer')
  assert.equal(question.drawing_reference.teacher_confirmed, false)
})

test('late correction responses cannot change the draft during or after its save', async (t) => {
  for (const when of ['during', 'after']) {
    const corrections = deferred()
    const layout = deferred()
    const editor = await openTemplateEditor(t, {
      pages: [templatePage(0)],
      corrections: () => corrections.promise,
      layout: body => (when === 'during' ? layout.promise.then(() => receipt(body)) : receipt(body)),
    })
    const question = editor.question(0)
    const late = { status: 200, data: { candidates: [{ question_no: '1', candidates: [{ cycle_id: 99, student_id: 5 }] }] } }
    // The editor asked for correction candidates when it opened; that request
    // is still unanswered when the teacher saves.
    assert.equal(editor.upload.requests(CORRECTIONS).length, 1, when)
    await editor.save()
    corrections.resolve(late)
    await editor.upload.settle()
    layout.resolve()
    await editor.upload.settle()
    assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM], when)
    assert.deepEqual(editor.bodies(CONFIRM)[0].pages[0].questions[0].authorized_wrong_cycle_ids, [], when)
    assert.equal(editor.question(0), question, when)
    assert.equal(question.standard_answer, 'teacher answer', when)
    assert.deepEqual(question.authorized_wrong_cycle_ids, [], when)
    assert.deepEqual(question.correction_candidates, [], when)
    assert.equal(editor.dialogOpen(), false, when)
    editor.upload.unmount()
  }
})

test('the final validity gate remains independent of the in-flight UI lock', async (t) => {
  // A valid draft whose answers still carry an older client's draft-status
  // markers: neither those nor the save's own lock may fail the final gate.
  const editor = await openTemplateEditor(t, { pages: [templatePage(0, { question: { _answerDraftStale: true, templateAnswerDraftStatus: 'stale' } })] })
  await editor.save()
  assert.deepEqual(editor.sent(), [LAYOUT(), CONFIRM])
  assert.deepEqual(editor.locks.map(lock => [lock.stage, lock.locked]), [['layout', true], ['confirm', true]])
  assert.equal(editor.dialogOpen(), false)
  assert.equal(editor.setup.confirmedTemplateId, TEMPLATE)
})
