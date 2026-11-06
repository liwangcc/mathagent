import assert from 'node:assert/strict'
import test from 'node:test'
import { MElement } from './helpers/memoryDom.js'
import { cutPage, openTemplateEditor } from './helpers/templateDrawingSlots.js'

// The template editor's shared page canvas: the real Upload page, opened from
// a recognized template, with its real DrawingReferenceEditor per drawing
// question. A teacher switches between geometry review and the standard
// figure, draws, edits questions and saves; the tests read what the page
// shows and what it sends.

const candidate = () => ({ version: 'drawing-reference-v1', coordinate_space: 'normalized_1000_v1', comparison_mode: 'fixed', variants: [{ strokes: [{ kind: 'point', point: [800, 450] }] }], context_regions: [], teacher_confirmed: false })

// One template page as the server returns it: the recognized questions and
// the stored cut page. An entry names a question, its answer kind, its
// drawing area (region) and / or text answer box (box), and any other field
// of the recognized question.
function templatePage(pageIndex, entries, cutFields = {}) {
  return {
    page: {
      page_index: pageIndex,
      questions: entries.map(({ number, kind, region, box, ...fields }) => ({
        question_no: number, question_type: 'solve', question_text: `第 ${number} 题题干`, standard_answer: `第 ${number} 题答案`,
        answer_kind: kind, ...(region ? { drawing_regions: [region] } : {}), ...fields,
      })),
    },
    cut: cutPage({
      pageIndex,
      anchors: entries.map(entry => entry.number),
      answerRegions: entries.filter(entry => entry.box).map(entry => ({ question_number: entry.number, regions: [entry.box] })),
      schema: entries.map(({ number, kind, region }) => ({ question_number: number, answer_kind: kind, ...(region ? { drawing_regions: [region] } : {}) })),
      ...cutFields,
    }),
  }
}
const template = (...pages) => ({ pages: pages.map(item => item.page), cutPages: pages.map(item => item.cut) })

// Q1 answers in a text box, Q2.3 is drawn in its area, Q3 has both.
const REGION = [600, 300, 900, 600]
const standardPage = (fields = {}, pageIndex = 0, cutFields = {}) => templatePage(pageIndex, [
  { number: '1', kind: 'non_graphic', box: [100, 100, 500, 200], ...fields['1'] },
  { number: '2.3', kind: 'drawing', region: REGION, drawing_reference: null, ...fields['2.3'] },
  { number: '3', kind: 'mixed', region: [600, 700, 900, 900], box: [100, 700, 500, 800], drawing_reference: null, ...fields['3'] },
], cutFields)

// What the page shows, by template page index (or the page's place in the
// editor's question list for the cards below the canvas).
function view(editor) {
  const article = i => editor.article(i)
  const editorFor = (i, number) => {
    const index = article(i).allByClass('tpl-reference-caption').findIndex(caption => caption.text.endsWith(`Q${number}`))
    return index < 0 ? null : editor.editors(i)[index]
  }
  const svg = section => editor.canvas(section).find(el => el.tagName === 'svg')
  return {
    editorFor,
    svg,
    captions: i => article(i).allByClass('tpl-reference-caption').map(el => el.text),
    visibleCanvases: i => editor.editors(i).filter(section => editor.shown(editor.canvas(section))),
    canvasShown: (i, number) => editor.shown(editor.canvas(editorFor(i, number))),
    overlay: section => editor.canvas(section).byClass('tpl-reference-geometry'),
    summary: (position, number) => editor.questionCard(position, number).byClass('drawing-reference-summary').find(el => el.tagName === 'span').text,
    link: (position, number) => editor.questionCard(position, number).byText('button', '到上方模板核对作图区域与标准图'),
    blockReason: i => article(i).byClass('tpl-question-management').find(el => el.tagName === 'p' && el.attributes.role === 'status')?.text ?? '',
    managementButtons: i => article(i).byClass('tpl-question-management').findAll(el => el.tagName === 'button'),
    reviewAlert: () => editor.dialog().byClass('tpl-cut-panel').children.find(el => el.text.startsWith('模板定位仍需教师复核'))?.text ?? '',
    bankLocked: () => editor.upload.button('写入私库').hasAttribute('disabled'),
    points: section => svg(section).findAll(el => el.tagName === 'circle' && el.classList.contains('reference-point'))
      .map(el => [Number(el.attributes.cx), Number(el.attributes.cy)]),
  }
}

const settle = editor => editor.upload.settle()
const click = async (editor, el) => {
  assert.ok(el, 'the control is on the page')
  await el.dispatch('click')
  await settle(editor)
}
// No layout engine here: an element the page measures is laid out as 1000 × 1000.
const layOut = (el) => {
  el.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 1000, bottom: 1000, width: 1000, height: 1000 })
  return el
}

// The teacher picks the 点 tool of a question's standard figure and clicks the canvas.
async function drawPoint(editor, section, [x, y]) {
  await click(editor, section.find(el => el.tagName === 'button' && el.text === '点'))
  const canvas = layOut(view(editor).svg(section))
  await canvas.dispatch('pointerdown', { button: 0, pointerId: 9, clientX: x, clientY: y })
  await settle(editor)
}

// The teacher opens Q<number>'s standard figure from its card and draws a point.
async function drawOn(editor, number, point, { position = 0, pageIndex = 0 } = {}) {
  await click(editor, view(editor).link(position, number))
  await drawPoint(editor, view(editor).editorFor(pageIndex, number), point)
}

// templateConfirm answers whatever the test decides, when it decides.
function heldConfirm() {
  const held = []
  return {
    confirmResponse: () => new Promise((resolve) => { held.push(resolve) }),
    get waiting() { return held.length },
    answer(data) { held.shift()({ status: 200, data }) },
  }
}
const SAVED = { success: true, page_count: 1, template_mode: 'marked', column_mode: 'single' }

// Save while the confirmation is on its way, change the draft in the
// meantime, then let the server answer.
async function saveWhile(editor, held, during, data = SAVED) {
  await editor.save()
  assert.equal(held.waiting, 1, 'the confirmation request is on its way')
  during()
  held.answer(data)
  await settle(editor)
}
const draftQuestion = (editor, number, position = 0) => editor.page.setup.templatePages[position].questions.find(q => q.question_no === number)

test('one shared whole-page canvas hosts geometry and standard reference; per-question cards do not repeat images', async (t) => {
  const held = heldConfirm()
  const editor = await openTemplateEditor(t, { ...template(standardPage()), confirmResponse: held.confirmResponse })
  const v = view(editor)
  assert.deepEqual(editor.page.errors, [], 'the editor renders without errors')
  // One editor per drawing question, all on the page preview; one canvas is shown.
  assert.deepEqual(v.captions(0), ['标准图 / 自定义作图判定 · Q2.3', '标准图 / 自定义作图判定 · Q3'])
  const [shared] = v.visibleCanvases(0)
  assert.equal(v.visibleCanvases(0).length, 1)
  assert.equal(shared, v.editorFor(0, '2.3'))
  const stage = editor.canvas(shared)
  assert.ok(stage.classList.contains('tpl-cut-preview-stage'))
  // The canvas shows the page's own template image, fetched once for the page.
  assert.equal(editor.upload.requests('GET /api/batch-grade/template/tpl-1/pages/0/image').length, 1)
  const images = editor.editors(0).map(section => section.find(el => el.tagName === 'img').attributes.src)
  assert.match(images[0], /^blob:/)
  assert.deepEqual(new Set(images).size, 1, 'every editor of the page uses the same image')
  // Geometry overlays are drawn on that canvas and its geometry tools sit above it.
  assert.ok(v.overlay(shared))
  assert.equal(stage.allByClass('tpl-handwriting-region').length, 2)
  assert.equal(stage.allByClass('tpl-drawing-region').length, 2)
  assert.equal(editor.article(0).allByClass('tpl-add-tools').length, 1)
  // 教师标注：默认状态即选择，几何工具行没有「选择」按钮。
  assert.deepEqual(shared.byClass('tpl-add-tools').findAll(el => el.tagName === 'button').map(el => el.text),
    ['红色题号', '蓝色作答框', '绿色画图区', '作图题题号', '删除所选'])
  // The question cards below repeat neither the image nor an editor; drawing
  // questions link up to the shared canvas instead.
  for (const card of editor.page.root.allByClass('tpl-q')) {
    assert.equal(card.find(el => el.tagName === 'img'), null)
    assert.equal(card.byClass('drawing-reference-editor'), null)
  }
  assert.ok(v.link(0, '2.3') && v.link(0, '3'))
  assert.equal(v.link(0, '1'), null)
  // No control generates a standard figure; the teacher draws it.
  assert.equal(editor.dialog().find(el => ['button', 'el-button'].includes(el.tagName) && /生成/.test(el.text)), null)
  // One save for all pages: no per-page save button.
  assert.equal(editor.article(0).find(el => el.tagName === 'button' && /保存/.test(el.text)), null)
  assert.match(editor.dialog().byClass('tpl-confirm-hint').text, /统一保存所有页/)
  assert.ok(editor.upload.button('保存模板（全部页）'))

  await editor.loadImages()
  // Geometry review: every figure is interactive, none has priority, overlays take input.
  for (const number of ['2.3', '3']) {
    const classes = v.svg(v.editorFor(0, number)).classList
    assert.equal(classes.contains('is-readonly'), false, `Q${number} is interactive`)
    assert.equal(classes.contains('is-reference-priority'), false)
  }
  assert.equal(v.overlay(shared).classList.contains('is-disabled'), false)
  // While the template is being saved its overlays and geometry tools take no input either.
  const geometryTools = () => v.editorFor(0, '2.3').byClass('tpl-add-tools').findAll(el => el.tagName === 'button')
  await editor.save()
  assert.equal(held.waiting, 1, 'the confirmation request is on its way')
  assert.ok(v.overlay(v.editorFor(0, '2.3')).classList.contains('is-disabled'), 'a saving template locks its overlays')
  for (const button of geometryTools()) assert.ok(button.hasAttribute('disabled'), `${button.text} is locked while saving`)
  // The server refuses the save: the editor takes input again.
  held.answer({ success: false, detail: '模板确认失败' })
  await settle(editor)
  assert.equal(v.overlay(v.editorFor(0, '2.3')).classList.contains('is-disabled'), false)
  assert.deepEqual(geometryTools().map(button => [button.text, button.hasAttribute('disabled')]), [
    ['红色题号', false], ['蓝色作答框', false], ['绿色画图区', false], ['作图题题号', false], ['删除所选', true],
  ], 'every tool is free again; 删除所选 waits for a selection')
  // Q3's card opens its standard figure: its canvas takes priority over the overlays.
  await click(editor, v.link(0, '3'))
  const q3 = v.editorFor(0, '3')
  assert.ok(v.svg(q3).classList.contains('is-reference-priority'))
  assert.equal(v.svg(q3).classList.contains('is-readonly'), false)
  assert.ok(v.overlay(q3).classList.contains('is-disabled'))
  assert.ok(v.svg(v.editorFor(0, '2.3')).classList.contains('is-readonly'), 'Q2.3 is read-only meanwhile')
  assert.equal(v.svg(v.editorFor(0, '2.3')).classList.contains('is-reference-priority'), false)
  // A drawing tool of Q2.3 asks to edit Q2.3 instead: the canvas changes owner.
  await click(editor, v.editorFor(0, '2.3').find(el => el.tagName === 'button' && el.text === '点'))
  assert.ok(v.canvasShown(0, '2.3') && !v.canvasShown(0, '3'))
  assert.ok(v.svg(v.editorFor(0, '2.3')).classList.contains('is-reference-priority'))
  assert.ok(v.svg(v.editorFor(0, '3')).classList.contains('is-readonly'))
})

test('exactly one shared template canvas is visible per review state', async (t) => {
  const editor = await openTemplateEditor(t, template(standardPage()))
  const v = view(editor)
  // 几何模式：画布固定跟随第一道作图题，其余题目不再各渲染一张一模一样的模板。
  assert.ok(v.canvasShown(0, '2.3') && !v.canvasShown(0, '3'))
  // 参考编辑模式：画布跟随当前编辑的题目，任何时刻只有一张可见。
  await click(editor, v.link(0, '3'))
  assert.ok(!v.canvasShown(0, '2.3') && v.canvasShown(0, '3'))
  await click(editor, v.link(0, '2.3'))
  assert.ok(v.canvasShown(0, '2.3') && !v.canvasShown(0, '3'))
  assert.equal(v.visibleCanvases(0).length, 1)
})

test('pages without graphic questions still keep one template canvas for geometry review', async (t) => {
  const editor = await openTemplateEditor(t, template(templatePage(0, [{ number: '1', kind: 'non_graphic', box: [100, 100, 500, 200] }])))
  const v = view(editor)
  // 无作图题：仍需一个编辑器宿主承载原图与几何工具（不带作图参考控件）。
  const [host] = editor.editors(0)
  assert.equal(editor.editors(0).length, 1)
  assert.ok(editor.shown(editor.canvas(host)))
  assert.ok(editor.canvas(host).find(el => el.tagName === 'img'))
  assert.ok(host.byClass('tpl-add-tools'))
  assert.equal(host.byClass('drawing-reference-drawing-tools'), null)
  assert.deepEqual(v.captions(0), [])
  // The teacher makes Q1 a drawing question: its editor takes the host's place.
  const kind = editor.questionCard(0, '1').find(el => el.tagName === 'select' && el.attributes['aria-label'] === '第 1 题作图属性')
  kind.value = 'drawing'
  await kind.dispatch('change')
  await settle(editor)
  assert.equal(editor.editors(0).length, 1)
  assert.deepEqual(v.captions(0), ['标准图 / 自定义作图判定 · Q1'])
  assert.ok(editor.editors(0)[0].byClass('drawing-reference-drawing-tools'))
  assert.ok(v.canvasShown(0, '1'))
})

test('graphic references bind by both page and original question number, including nested Q identifiers', async (t) => {
  // Page 1 has a Q2.3 of its own, a mixed one with its own drawing area.
  const editor = await openTemplateEditor(t, template(
    templatePage(0, [{ number: '1', kind: 'non_graphic', box: [100, 100, 500, 200] }, { number: '2.3', kind: 'drawing', region: REGION }]),
    templatePage(1, [{ number: '2.3', kind: 'mixed', region: [100, 500, 400, 800], box: [100, 850, 400, 950] }]),
  ))
  const v = view(editor)
  const regions = section => v.svg(section).findAll(el => el.tagName === 'rect' && el.classList.contains('drawing-region'))
    .map(el => ['x', 'y', 'width', 'height'].map(key => Number(el.attributes[key])))
  assert.deepEqual(v.captions(0), ['标准图 / 自定义作图判定 · Q2.3'])
  assert.deepEqual(regions(v.editorFor(0, '2.3')), [[600, 300, 300, 300]])
  await click(editor, editor.upload.button('下一页'))
  assert.ok(editor.shown(editor.article(1)) && !editor.shown(editor.article(0)))
  assert.deepEqual(v.captions(1), ['标准图 / 自定义作图判定 · Q2.3'])
  assert.deepEqual(regions(v.editorFor(1, '2.3')), [[100, 500, 300, 300]])
})

test('an unset standard figure remains distinct from a saved manual mode', async (t) => {
  const manual = { ...candidate(), comparison_mode: 'manual' }
  const editor = await openTemplateEditor(t, template(standardPage({ 3: { drawing_reference: manual } })))
  const v = view(editor)
  assert.match(v.summary(0, '2.3'), /未设置标准图/)
  assert.match(v.summary(0, '3'), /自定义作图/)
  await editor.loadImages()
  await drawOn(editor, '2.3', [700, 450])
  assert.match(v.summary(0, '2.3'), /^标准图 1 个完整方案/)
  // Deleting the only scheme leaves a manual figure without saved strokes.
  await click(editor, v.editorFor(0, '2.3').find(el => el.tagName === 'button' && el.text === '删除方案'))
  assert.match(v.summary(0, '2.3'), /未保存标准笔画/)
  assert.match(v.summary(0, '2.3'), /题干和作图区直接交给 Flash/)
})

test('reference edit does not mutate the other page', async (t) => {
  const editor = await openTemplateEditor(t, template(
    templatePage(0, [{ number: '2.3', kind: 'drawing', region: REGION, drawing_reference: null }]),
    templatePage(1, [{ number: '2.3', kind: 'mixed', region: [100, 500, 400, 800], box: [100, 850, 400, 950] }]),
  ))
  const v = view(editor)
  await editor.loadImages()
  await drawOn(editor, '2.3', [700, 450])
  assert.match(v.summary(0, '2.3'), /^标准图 1 个完整方案 · 随保存模板一起确认/)
  assert.match(v.blockReason(0), /尚未最终保存/)
  // Page 1's Q2.3 is untouched.
  assert.match(v.summary(1, '2.3'), /未设置标准图/)
  await click(editor, editor.upload.button('下一页'))
  assert.deepEqual(v.points(v.editorFor(1, '2.3')), [])
  assert.equal(Object.hasOwn(draftQuestion(editor, '2.3', 1), 'drawing_reference'), false)
  // A canvas without a question (the page's geometry host) owns no figure.
  assert.doesNotThrow(() => editor.page.setup.updateDrawingReference(null, candidate()))
})

test('changing editor ownership clears old geometry gesture and cannot switch while generation saves its candidate', async (t) => {
  const held = heldConfirm()
  const editor = await openTemplateEditor(t, { ...template(standardPage()), confirmResponse: held.confirmResponse })
  const v = view(editor)
  const scrolls = t.mock.method(MElement.prototype, 'scrollIntoView')
  const preview = () => editor.page.setup.templateCutPreviewPages[0]
  const answerBox = () => editor.canvas(v.visibleCanvases(0)[0]).find(el => el.classList.contains('tpl-handwriting-region') && el.attributes.title.startsWith('Q1 '))
  const style = el => ({ ...el.style })
  // The teacher is dragging Q1's answer box when Q2.3's card opens its figure.
  const before = style(answerBox())
  await answerBox().dispatch('pointerdown', { button: 0, pointerId: 4, clientX: 100, clientY: 100 })
  await answerBox().dispatch('pointermove', { pointerId: 4, clientX: 101, clientY: 101 })
  await settle(editor)
  assert.ok(answerBox().classList.contains('is-selected'))
  assert.notDeepEqual(style(answerBox()), before, 'the box follows the pointer')
  await click(editor, v.link(0, '2.3'))
  assert.deepEqual(style(answerBox()), before, 'the unfinished drag is undone')
  assert.equal(answerBox().classList.contains('is-selected'), false)
  assert.ok(v.svg(v.editorFor(0, '2.3')).classList.contains('is-reference-priority'), 'Q2.3 owns the canvas')
  await answerBox().dispatch('pointerup', { pointerId: 4, clientX: 300, clientY: 300 })
  await settle(editor)
  assert.deepEqual(style(answerBox()), before, 'the cancelled drag is over')
  assert.equal(scrolls.mock.calls.at(-1).this, editor.article(0), 'the card scrolled up to the page canvas')

  // A box tool is armed and a box half drawn when Q3's card opens its figure.
  await click(editor, v.visibleCanvases(0)[0].byText('button', '蓝色作答框'))
  const surface = editor.article(0).byClass('tpl-draw-surface')
  assert.ok(surface, 'the box tool is armed')
  layOut(surface.closest('.tpl-cut-preview-stage'))
  await surface.dispatch('pointerdown', { button: 0, pointerId: 5, clientX: 300, clientY: 300 })
  await settle(editor)
  assert.ok(editor.article(0).byClass('tpl-draw-draft'))
  await click(editor, v.link(0, '3'))
  assert.equal(editor.article(0).byClass('tpl-draw-draft'), null)
  assert.equal(editor.article(0).byClass('tpl-draw-surface'), null, 'the tool is put away')
  assert.ok(v.canvasShown(0, '3'))

  // While the template is being saved the page keeps its owner.
  await editor.loadImages()
  await editor.save()
  assert.equal(held.waiting, 1, 'the confirmation request is on its way')
  assert.ok(editor.dialog().byClass('tpl-editor-content').hasAttribute('disabled'), 'the editor is locked')
  assert.ok(v.visibleCanvases(0)[0].byText('button', '蓝色作答框').hasAttribute('disabled'))
  // The locked editor takes no clicks; a mode switch or a card's link that
  // still reaches its handler (a click queued before the lock) changes nothing.
  editor.page.setup.setTemplatePreviewEditMode(preview(), 'geometry')
  await click(editor, v.link(0, '2.3'))
  assert.ok(v.canvasShown(0, '3') && !v.canvasShown(0, '2.3'))
  assert.ok(v.svg(v.editorFor(0, '3')).classList.contains('is-reference-priority'), 'still editing Q3\'s figure')
  // The server refuses the save: the card's link hands the canvas to Q2.3.
  held.answer({ success: false, detail: '模板确认失败' })
  await settle(editor)
  await click(editor, v.link(0, '2.3'))
  assert.ok(v.canvasShown(0, '2.3') && !v.canvasShown(0, '3'))
  assert.ok(v.svg(v.editorFor(0, '2.3')).classList.contains('is-reference-priority'), 'Q2.3 owns the canvas now')
  assert.equal(scrolls.mock.calls.at(-1).this, editor.article(0))

  // A selected stroke of the standard figure is let go whenever a card
  // (re)opens a figure, its own included.
  await drawPoint(editor, v.editorFor(0, '2.3'), [700, 450])
  const hint = () => editor.article(0).byClass('tpl-pointer-hint').text
  const deleteSelected = () => v.visibleCanvases(0)[0].byText('button', '删除所选')
  const selectStroke = async () => {
    const svg = v.svg(v.editorFor(0, '2.3'))
    // Pointer capture is a browser API the in-memory element does not have.
    Object.assign(svg, { setPointerCapture() {}, hasPointerCapture: () => false, releasePointerCapture() {} })
    const point = svg.find(el => el.tagName === 'circle' && el.classList.contains('reference-point'))
    assert.ok(point, 'Q2.3 has a drawn point')
    await point.dispatch('pointerdown', { button: 0, pointerId: 6, clientX: 700, clientY: 450 })
    await point.dispatch('pointerup', { button: 0, pointerId: 6, clientX: 700, clientY: 450 })
    await settle(editor)
    assert.match(hint(), /已选中标准图笔画/)
    assert.equal(deleteSelected().hasAttribute('disabled'), false, 'the stroke can be deleted')
  }
  await selectStroke()
  await click(editor, v.link(0, '2.3'))
  assert.doesNotMatch(hint(), /已选中标准图笔画/)
  assert.ok(deleteSelected().hasAttribute('disabled'), 'nothing is selected any more')
  await selectStroke()
  await click(editor, v.link(0, '3'))
  assert.doesNotMatch(hint(), /已选中标准图笔画/)
  assert.ok(deleteSelected().hasAttribute('disabled'))
})

test('full-page snapshot binds geometry, problem, answer and reference for stale-response protection', async (t) => {
  const held = heldConfirm()
  const editor = await openTemplateEditor(t, { ...template(standardPage()), confirmResponse: held.confirmResponse })
  const v = view(editor)
  await editor.loadImages()
  await drawOn(editor, '2.3', [700, 450])
  // The question's drawing area changes while the save is on its way: the
  // saved figure no longer matches it and stays an unsaved edit.
  await saveWhile(editor, held, () => { draftQuestion(editor, '2.3').drawing_regions[0][0] = 610 })
  await editor.reopen()
  assert.match(v.blockReason(0), /尚未最终保存/)
  assert.match(v.summary(0, '2.3'), /随保存模板一起确认/)
  // So does a standard-answer change (the reopened editor loads its image again).
  await editor.loadImages()
  await saveWhile(editor, held, () => { draftQuestion(editor, '2.3').standard_answer = 'teacher updated answer' })
  await editor.reopen()
  assert.match(v.blockReason(0), /尚未最终保存/)
  // A change undone before the answer arrives is what was saved.
  await editor.loadImages()
  await saveWhile(editor, held, () => {
    draftQuestion(editor, '2.3').drawing_regions[0][0] = 600
    draftQuestion(editor, '2.3').drawing_regions[0][0] = 610
  })
  await editor.reopen()
  assert.equal(v.blockReason(0), '')
  assert.match(v.summary(0, '2.3'), /已随模板保存/)
})

const templateImages = editor => editor.page.root.findAll(el => el.tagName === 'img' && el.attributes.alt === '教师模板原图')
const imageWarnings = editor => editor.warnings().filter(message => /作图模板原图尚未加载成功/.test(message))

test('the shared canvas shows the template image loading and failing, and a loaded page saves', async (t) => {
  const editor = await openTemplateEditor(t, template(standardPage()))
  const text = () => editor.dialog().text
  assert.match(text(), /模板原图加载中/)
  assert.doesNotMatch(text(), /模板原图加载失败/)
  for (const img of templateImages(editor)) await img.dispatch('error')
  await settle(editor)
  assert.match(text(), /模板原图加载失败/)
  assert.doesNotMatch(text(), /模板原图加载中/)
  // The preview is loaded again and this time the image arrives.
  await editor.loadImages()
  assert.doesNotMatch(text(), /模板原图加载中|模板原图加载失败/)
  await editor.save()
  assert.equal(editor.confirms().length, 1)
  assert.deepEqual(imageWarnings(editor), [])
})

// DrawingReferenceEditor reports image-ready with `false` as soon as it mounts
// (and while a new image loads, or after it failed), `true` once it is there;
// the final save follows what it reports.
test('image readiness propagates to the final template confirmation gate, not only editor checkbox', async (t) => {
  const editor = await openTemplateEditor(t, template(standardPage()))
  // The image has not loaded yet: the final save stops and says why.
  await editor.save()
  assert.equal(editor.confirms().length, 0, 'no confirmation before the template image has loaded')
  assert.equal(imageWarnings(editor).length, 1)
  // An image that failed to load stops it too.
  for (const img of templateImages(editor)) await img.dispatch('error')
  await settle(editor)
  await editor.save()
  assert.equal(editor.confirms().length, 0, 'no confirmation over a failed template image')
  assert.equal(imageWarnings(editor).length, 2)
  // Once the image is there, the template is saved.
  await editor.loadImages()
  await editor.save()
  assert.equal(editor.confirms().length, 1)
  assert.equal(imageWarnings(editor).length, 2)
})

test('a teacher-confirmed page can have a different column count from the original batch default', async (t) => {
  // A double-column batch whose page 1 the teacher corrects to one column by
  // deleting its divider.
  const twoColumns = [{ x1_1000: 0, x2_1000: 500 }, { x1_1000: 500, x2_1000: 1000 }]
  let editor = await openTemplateEditor(t, { ...template(standardPage({}, 0, { columns: twoColumns })), columnMode: 'double' })
  let v = view(editor)
  const stage = () => editor.canvas(v.visibleCanvases(0)[0])
  const divider = stage().byClass('tpl-column-divider')
  await divider.dispatch('pointerdown', { button: 0, pointerId: 3, clientX: 500, clientY: 10 })
  await divider.dispatch('pointerup', { pointerId: 3, clientX: 500, clientY: 10 })
  await settle(editor)
  await click(editor, v.visibleCanvases(0)[0].byText('button', '删除所选'))
  assert.deepEqual(stage().allByClass('tpl-column-overlay').map(el => el.text), ['第1栏'])
  assert.equal(v.reviewAlert(), '', 'a one-column page is valid in a double-column batch')
  await editor.loadImages()
  await editor.save()
  assert.deepEqual(editor.layoutSaves().map(sent => sent.columns.length), [1])
  assert.equal(editor.confirms().length, 1, 'the teacher-confirmed page passes the final gate')
  editor.upload.unmount()

  // An older saved plan without an explicit column count: its columns give it.
  editor = await openTemplateEditor(t, { ...template(standardPage({}, 0, { columnCount: null })), columnMode: 'triple' })
  v = view(editor)
  assert.equal(v.reviewAlert(), '', 'legacy saved columns supply the page count when an explicit count is absent')
  await editor.loadImages()
  await editor.save()
  assert.equal(editor.confirms().length, 1)
})

test('per-page column validation still rejects missing, invalid-count and out-of-bounds drafts', async (t) => {
  const editor = await openTemplateEditor(t, template(standardPage()))
  const v = view(editor)
  await editor.loadImages()
  assert.equal(v.reviewAlert(), '')
  assert.equal(v.bankLocked(), false)
  // The automatic cut is run again and the server stores a broken page plan
  // (each cut brings the page image again, which loads).
  for (const plan of [
    { columns: [] },
    { columnCount: 4 },
    { columnCount: 1.5 },
    { columnCount: 2, columns: [{ x1_1000: 0, x2_1000: 1000 }] },
    { columnCount: 1, columns: [{ x1_1000: -1, x2_1000: 1000 }] },
    { columnCount: 1, columns: [{ x1_1000: 200, x2_1000: 200 }] },
  ]) {
    await editor.recut([standardPage({}, 0, plan).cut])
    await editor.loadImages()
    assert.match(v.reviewAlert(), /栏边界 第1页/, JSON.stringify(plan))
    assert.ok(v.bankLocked(), JSON.stringify(plan))
  }
  await editor.recut([standardPage().cut])
  await editor.loadImages()
  assert.equal(v.reviewAlert(), '')
  assert.equal(v.bankLocked(), false)
  // The page's own count says nothing yet (the 分栏 picker's placeholder).
  editor.page.setup.templateCutPreviewPages[0].columnCount = 0
  await settle(editor)
  assert.match(v.reviewAlert(), /栏边界 第1页/)
  await editor.recut([standardPage().cut])
  await editor.loadImages()
  // The teacher picks a column count: an unconfirmed teacher draft.
  await editor.upload.select(editor.article(0).byClass('tpl-column-count').find(el => el.tagName === 'select'), 2)
  assert.match(v.reviewAlert(), /栏边界 第1页/)
  assert.ok(v.bankLocked())
})

test('reference-only import failure does not keep the geometry gate closed after reference retry and confirmation', async (t) => {
  const referenceTimeout = {
    needs_review: true, geometry_review_pending: false,
    review_reasons: ['作图标准图候选生成失败，请教师编辑并确认参考图'],
    drawing_reference_candidates: { status: 'timeout' },
    model_stages: { 'template-drawing-reference-v1': { status: 'timeout' } },
  }
  const editor = await openTemplateEditor(t, template(standardPage(
    { '2.3': { drawing_reference: { ...candidate(), teacher_confirmed: true } } }, 0, { anchor_diagnostics: referenceTimeout },
  )))
  const v = view(editor)
  await editor.loadImages()
  assert.equal(v.reviewAlert(), '')
  assert.equal(v.bankLocked(), false)
  await editor.save()
  assert.deepEqual(editor.layoutSaves(), [], 'no geometry left to confirm')
  assert.equal(editor.confirms().length, 1)
})

test('explicit geometry, legacy diagnostics and image association risks still block final confirmation', async (t) => {
  const editor = await openTemplateEditor(t, template(standardPage()))
  const v = view(editor)
  const blocked = async (cutFields, reason) => {
    await editor.recut([standardPage({}, 0, cutFields).cut])
    await editor.loadImages()
    assert.match(v.reviewAlert(), reason, JSON.stringify(cutFields))
    assert.ok(v.bankLocked(), JSON.stringify(cutFields))
  }
  await editor.loadImages()
  assert.equal(v.reviewAlert(), '')
  assert.equal(v.bankLocked(), false)
  for (const diagnostic of [
    { needs_review: true, geometry_review_pending: true },
    { needs_review: false, geometry_review_pending: true },
    { needs_review: true }, { needsReview: true },
    { needs_review: true, geometry_review_pending: false, image_association: { needs_review: true } },
    { needs_review: false, geometry_review_pending: false, image_association: { status: 'needs_review' } },
  ]) await blocked({ anchor_diagnostics: diagnostic }, /模型诊断待复核/)
  const unrelatedStageFailure = { needs_review: true, geometry_review_pending: false }
  await blocked({ anchor_diagnostics: unrelatedStageFailure, layout_review_pending: true }, /栏边界 第1页/)
  await blocked({ anchor_diagnostics: unrelatedStageFailure, question_anchors: [] }, /缺失题号/)
  const anchors = standardPage().cut.question_anchors.map(anchor => (anchor.question_number === '2.3' ? { ...anchor, confidence: 0.1 } : anchor))
  await blocked({ anchor_diagnostics: unrelatedStageFailure, question_anchors: anchors }, /低置信题目归属/)
})

test('existing saved references do not block question changes, but generated or edited local references do', async (t) => {
  const editor = await openTemplateEditor(t, template(standardPage({ '2.3': { drawing_reference: { ...candidate(), teacher_confirmed: true } } })))
  const v = view(editor)
  const managementLocked = () => v.managementButtons(0).map(button => button.hasAttribute('disabled'))
  assert.equal(v.blockReason(0), '')
  assert.deepEqual(managementLocked(), [false, false, false])
  await editor.loadImages()
  await drawOn(editor, '2.3', [700, 450])
  assert.match(v.blockReason(0), /尚未最终保存/)
  assert.deepEqual(managementLocked(), [true, true, true])
  // Saving the template saves the figure: question changes are open again.
  await editor.save()
  assert.equal(editor.confirms().length, 1)
  await editor.reopen()
  assert.equal(v.blockReason(0), '')
  assert.deepEqual(managementLocked(), [false, false, false])
  await editor.loadImages()
  await drawOn(editor, '2.3', [750, 500])
  assert.match(v.blockReason(0), /尚未最终保存/)
  // A page that is saving locks the whole editor.
  editor.page.setup.templateCutPreviewPages[0].saving = true
  await settle(editor)
  assert.ok(editor.dialog().byClass('tpl-editor-content').hasAttribute('disabled'))
})

test('successful final save clears only the exact sent reference and cannot clear newer edits', async (t) => {
  const held = heldConfirm()
  const editor = await openTemplateEditor(t, { ...template(standardPage()), confirmResponse: held.confirmResponse })
  const v = view(editor)
  await editor.loadImages()
  await drawOn(editor, '2.3', [700, 450])
  // A refused save keeps the figure an unsaved edit.
  await saveWhile(editor, held, () => {}, { success: false, detail: '模板确认失败' })
  assert.equal(editor.dialog().attributes['data-open'], 'true')
  assert.match(v.blockReason(0), /尚未最终保存/)
  assert.match(v.summary(0, '2.3'), /随保存模板一起确认/)
  // The answer changes after the save was sent: the saved figure is not this one.
  await saveWhile(editor, held, () => { draftQuestion(editor, '2.3').standard_answer = 'later teacher answer' })
  await editor.reopen()
  assert.match(v.blockReason(0), /尚未最终保存/)
  assert.match(v.summary(0, '2.3'), /随保存模板一起确认/)
  // Changed and changed back before the answer: exactly what was sent is saved.
  await editor.loadImages()
  await saveWhile(editor, held, () => {
    draftQuestion(editor, '2.3').standard_answer = 'later teacher answer, again'
    draftQuestion(editor, '2.3').standard_answer = 'later teacher answer'
  })
  await editor.reopen()
  assert.equal(v.blockReason(0), '')
  assert.match(v.summary(0, '2.3'), /已随模板保存/)
})

test('insert and delete reject unsaved references before server requests or deletion confirmation', async (t) => {
  const editor = await openTemplateEditor(t, template(standardPage()))
  const v = view(editor)
  await editor.loadImages()
  await drawOn(editor, '2.3', [700, 450])
  assert.deepEqual(v.managementButtons(0).map(button => [button.text, button.hasAttribute('disabled')]), [
    ['在前面插入题目', true], ['末尾追加题目', true], ['删除整道题', true],
  ])
  // Even reached, insert and delete only explain why.
  const preview = editor.page.setup.templateCutPreviewPages[0]
  await editor.page.setup.insertTemplateQuestion(preview, '2.3')
  await editor.page.setup.deleteTemplateQuestion(preview, '2.3')
  await settle(editor)
  assert.equal(editor.questionEdits().length, 0)
  assert.deepEqual(editor.page.boxes, [], 'no number prompt and no deletion confirmation')
  assert.equal(editor.warnings().filter(message => /尚未最终保存/.test(message)).length, 2)
  // Once the template is saved, inserting asks for the number and sends it.
  await editor.save()
  await editor.reopen()
  editor.answerPrompts('2.4')
  await click(editor, editor.upload.button('在前面插入题目'))
  assert.deepEqual(editor.page.boxes.map(box => box.kind), ['prompt'])
  assert.equal(editor.questionEdits().length, 1)
  assert.equal(editor.questionEdits()[0].action, 'insert')
  assert.equal(editor.questionEdits()[0].new_question_number, '2.4')
  assert.equal(editor.dialog().byClass('tpl-editor-content').hasAttribute('disabled'), false, 'the editor is free again')
})

test('editing question text or answers keeps the teacher-drawn standard figure', async (t) => {
  const drawn = { ...candidate(), teacher_confirmed: true }
  const editor = await openTemplateEditor(t, template(templatePage(0, [
    { number: '5', kind: 'mixed', region: REGION, box: [100, 100, 500, 200], standard_answer: '12', drawing_reference: drawn },
    // A stale figure on a non-drawing question (left over from the model).
    { number: '6', kind: 'non_graphic', box: [100, 700, 500, 800], drawing_reference: { ...candidate(), teacher_confirmed: true } },
  ])))
  const v = view(editor)
  const card = editor.questionCard(0, '5')
  const field = placeholder => card.find(el => el.tagName === 'textarea' && el.attributes.placeholder === placeholder)
  assert.match(v.summary(0, '5'), /已随模板保存/)
  await editor.upload.type(field('填写关键步骤或扣分依据'), '先列方程')
  assert.match(v.summary(0, '5'), /已随模板保存/, 'solution steps are not part of the figure check')
  await editor.upload.type(field('请输入题干'), '按要求作图并求值')
  assert.match(v.summary(0, '5'), /随保存模板一起确认/)
  await editor.upload.type(field('请输入标准答案'), '13')
  // The figure stays drawn; it only needs re-checking with the next save.
  assert.deepEqual(v.points(v.editorFor(0, '5')), [[800, 450]])
  assert.deepEqual(editor.warnings(), [])
  await editor.loadImages()
  await editor.save()
  const sent = Object.fromEntries(editor.confirms()[0].pages[0].questions.map(q => [q.question_no, q]))
  assert.deepEqual(sent[5].drawing_reference.variants, drawn.variants)
  assert.equal(sent[5].drawing_reference.teacher_confirmed, false)
  assert.equal(sent[5].question_text, '按要求作图并求值')
  // Only the drawing kind decides whether a figure is sent.
  assert.equal(Object.hasOwn(sent[6], 'drawing_reference'), false)
})
