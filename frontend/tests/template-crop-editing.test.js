import test from 'node:test'
import assert from 'node:assert/strict'
import { loadApiClient } from './helpers/apiClient.js'
import { memoryDocument } from './helpers/memoryDom.js'
import { jsonResponse } from './helpers/memoryFetch.js'
import {
  anchor,
  CHECKPOINT_KEY,
  cutPage,
  openTemplateEditor,
  question,
  regions,
  ROUTES,
} from './helpers/templateCrop.js'
import { mountUpload, paperFile } from './helpers/uploadPage.js'

// The template editor of the real 批改 page (Upload.vue), driven the way a
// teacher drives it: dragging the page's markers, picking tools, typing into
// the answer editor and clicking 保存模板（全部页）. The tests read what the page
// renders and what it sends to the server.

const buttonTexts = (el, predicate = () => true) => el
  .findAll(node => node.tagName === 'button' && predicate(node))
  .map(node => node.text)

const answerBox = (card, number, nth = 0) => card.allByClass('tpl-handwriting-region')
  .filter(el => el.attributes.title?.startsWith(`Q${number} 作答框`))[nth]

const regionOf = (list, number) => (list || []).find(item => item.question_number === String(number))

// Clicks the legend entry, as the teacher picks a marker before 删除所选.
async function pickInLegend(editor, label, pageIndex = 0) {
  const entry = editor.card(pageIndex).byClass('template-geometry-legend')
    .find(el => el.tagName === 'button' && el.text.replace(/\s+/g, ' ').includes(label))
  assert.ok(entry, `legend entry ${label}`)
  await entry.dispatch('click')
  await editor.settle()
}

// Clicks as a user does: without waiting for what the click started.
async function clickIn(editor, el) {
  assert.ok(el, 'button to click')
  assert.equal(el.hasAttribute('disabled'), false, `${el.text} is disabled`)
  el.dispatch('click').catch(err => editor.page.errors.push({ err, info: 'click handler' }))
  await editor.settle()
}

test('label points retain geometry while their text stays in the external legend', async (t) => {
  const editor = await openTemplateEditor(t, {
    questions: [[question('1'), question('2'), question('3')]],
    cut: { pages: [cutPage(0, {
      // The server asks the teacher to review this page, so it is saved with the template.
      geometry_review_pending: true,
      question_anchors: [anchor('1', [10, 10, 40, 30]), anchor('2', [10, 300, 40, 320]), anchor('3', [10, 500, 40, 520])],
      template_answer_regions: [
        regions('1', [[50, 60, 400, 200]]),
        regions('2', [[600, 100, 610, 110]], { geometry_kind: 'label_anchor' }),
      ],
      // An older artifact kept a label point in the legacy field, marked on the region itself.
      template_handwriting_regions: [regions('3', [{ bbox: [600, 400, 610, 410], geometry_kind: 'label_anchor' }])],
    })] },
  })
  const card = editor.card()

  const box = answerBox(card, 1)
  assert.equal(box.classList.contains('is-label-anchor'), false)
  assert.ok(box.byClass('tpl-handwriting-resize'), 'an answer box can be resized')
  for (const number of ['2', '3']) {
    const point = answerBox(card, number)
    assert.ok(point.classList.contains('is-label-anchor'), `Q${number} is drawn as a label point`)
    assert.ok(!point.byClass('tpl-handwriting-resize'), 'a label point has no resize handle')
    assert.equal(point.children.length, 0, 'a label point carries no label of its own')
    assert.equal(point.text, '')
  }

  // The question labels live outside the image, in the legend of the page's labels area.
  const legend = card.byClass('drawing-reference-labels').byClass('template-geometry-legend')
  assert.ok(legend, 'the legend renders in the geometry labels area')
  const entries = buttonTexts(legend).map(text => text.replace(/\s+/g, ' ').trim())
  for (const label of ['Q1 作答框 1', 'Q2 作答框 1', 'Q3 作答框 1', 'Q1 题号位置 1']) assert.ok(entries.includes(label), label)

  await editor.save()
  const [saved] = editor.layoutSaves()
  for (const field of ['template_answer_regions', 'template_handwriting_regions']) {
    assert.equal(regionOf(saved[field], 1).geometry_kind, undefined, field)
    assert.equal(regionOf(saved[field], 2).geometry_kind, 'label_anchor', `${field} keeps Q2's geometry kind`)
    assert.equal(regionOf(saved[field], 3).geometry_kind, 'label_anchor', `${field} keeps Q3's geometry kind`)
    assert.deepEqual(regionOf(saved[field], 2).regions, [[600, 100, 610, 110]])
  }
  assert.equal(editor.confirms().length, 1)
})

// Starts one of the editor's own gesture handlers on a rendered marker, then
// moves and releases the pointer over the page as a browser delivers it.
async function gestureFrom(editor, start, el, target, [x0, y0], [x1, y1]) {
  editor.layOut()
  const pointer = { pointerId: 1, button: 0 }
  start({ ...pointer, currentTarget: el, target: el, clientX: x0, clientY: y0, preventDefault() {} }, editor.preview(), target)
  const surface = editor.card().byClass('drawing-reference-editor')
  await surface.dispatch('pointermove', { ...pointer, clientX: x1, clientY: y1 })
  await surface.dispatch('pointerup', { ...pointer, clientX: x1, clientY: y1 })
  await editor.settle()
}

test('teacher can adjust and persist template columns and blue answer regions', async (t) => {
  const editor = await openTemplateEditor(t, {
    cut: { pages: [cutPage(0, {
      question_anchors: [anchor('1', [10, 10, 40, 30], { confidence: 0.9 })],
      template_answer_regions: [regions('1', [[100, 100, 400, 200]])],
    })] },
  })
  const dialog = editor.dialog()
  const card = editor.card()
  assert.match(dialog.byClass('tpl-confirm-hint').text, /统一保存所有页，无需逐页保存/)
  // One save for every page: no per-page save button, and the only deletions
  // are of a marker or of a whole question (never of a crop).
  assert.deepEqual(buttonTexts(dialog, el => el.text.includes('保存')), ['保存模板（全部页）'])
  assert.equal(buttonTexts(dialog, el => /保存本页(?:定位)?调整/.test(el.text)).length, 0)
  assert.deepEqual(buttonTexts(card, el => el.text.includes('删除')), ['删除整道题', '删除所选'])

  await editor.drag(card.byClass('tpl-column-divider'), [500, 0], [550, 0])
  assert.equal(card.byText('div', '第1栏').style.width, '55%')
  await editor.drag(card.byClass('tpl-question-anchor'), [20, 20], [120, 70])
  await editor.drag(card.byClass('tpl-question-anchor').byClass('tpl-handwriting-resize'), [140, 80], [160, 90])
  assert.deepEqual(card.byClass('tpl-question-anchor').style, { left: '11%', top: '6%', width: '5%', height: '3%' })
  await editor.drag(answerBox(card, 1), [150, 150], [200, 170])
  await editor.drag(answerBox(card, 1).byClass('tpl-handwriting-resize'), [450, 220], [470, 240])
  assert.deepEqual(answerBox(card, 1).style, { left: '15%', top: '12%', width: '32%', height: '12%' })
  assert.match(card.text, /已调整 · 随模板保存/)

  // The handwriting gestures move the same unified 作答框 (no separate marker renders them).
  await gestureFrom(editor, editor.page.setup.startTemplateHandwritingMove, answerBox(card, 1), editor.preview().answerBoxes[0], [200, 200], [210, 230])
  await gestureFrom(editor, editor.page.setup.startTemplateHandwritingResize, answerBox(card, 1), editor.preview().answerBoxes[0], [480, 270], [490, 280])

  await editor.save()
  const saves = editor.layoutSaves()
  assert.equal(saves.length, 1)
  const [saved] = saves
  assert.deepEqual(saved.columns.map(column => [column.x1_1000, column.x2_1000, column.column_index]), [[0, 550, 1], [550, 1000, 2]])
  assert.deepEqual(saved.question_anchors.map(item => [item.question_number, item.question_number_bbox, item.human_confirmed]), [['1', [110, 60, 160, 90], true]])
  assert.deepEqual(regionOf(saved.template_answer_regions, 1).regions, [[160, 150, 490, 280]])
  assert.deepEqual(regionOf(saved.template_handwriting_regions, 1).regions, [[160, 150, 490, 280]])
  const order = editor.client.requests.map(config => `${config.method} ${config.url}`)
  assert.ok(order.indexOf('put /batch-grade/template/tpl-1/layout-overrides') < order.indexOf('post /batch-grade/template/confirm'))
  assert.equal(editor.confirms().length, 1)
})

test('模板分栏与作答框以 PUT 持久化到 layout-overrides，失败时不额外弹通用提示', async () => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'tok-layout' } })
  const payload = {
    page_index: 1,
    columns: [{ x1_1000: 0, x2_1000: 500 }, { x1_1000: 500, x2_1000: 1000 }],
    question_anchors: [{ question_number: '1', question_number_bbox: [10, 10, 40, 30], human_confirmed: true }],
    template_answer_regions: [{ question_number: '1', regions: [[50, 60, 400, 200]] }],
    template_handwriting_regions: [{ question_number: '1', regions: [[50, 60, 400, 200]] }],
  }
  client.respond(() => ({ data: { success: true, page_index: 1, columns: payload.columns } }))

  const saved = await client.api.grade.templateLayoutOverrides('tpl 1/2', payload)

  assert.deepEqual(saved, { success: true, page_index: 1, columns: payload.columns })
  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'put')
  assert.equal(request.url, '/batch-grade/template/tpl%201%2F2/layout-overrides')
  assert.deepEqual(JSON.parse(request.data), payload)
  assert.equal(request.headers.Authorization, 'Bearer tok-layout')

  client.respond(() => ({ status: 422, data: { detail: '第 1 题作答框越界' } }))
  await assert.rejects(client.api.grade.templateLayoutOverrides('tpl 1/2', payload), { message: '第 1 题作答框越界' })
  assert.deepEqual(client.messages, [])
})

test('整题增删折叠在题目管理内，并与仅删除定位标记明确区分', async (t) => {
  const renumbered = {
    success: true,
    action: 'insert',
    pages: [{ page_index: 0, questions: [question('1'), question('1.1', { question_text: '' }), question('2')] }],
    page_artifacts: {
      'page-0': {
        question_anchors: [anchor('1', [10, 10, 40, 30]), anchor('1.1', [10, 200, 40, 220]), anchor('2', [10, 400, 40, 420])],
        template_answer_regions: [regions('1', [[100, 100, 300, 160]]), regions('2', [[100, 450, 300, 520]])],
        template_handwriting_regions: [regions('1.1', [[100, 230, 300, 300]])],
      },
    },
  }
  const editor = await openTemplateEditor(t, {
    questions: [[question('1'), question('2')]],
    cut: { pages: [cutPage(0, {
      question_anchors: [anchor('1', [10, 10, 40, 30]), anchor('2', [10, 400, 40, 420])],
      template_answer_regions: [regions('1', [[100, 100, 300, 160], [100, 170, 300, 190]]), regions('2', [[100, 450, 300, 520]])],
    })] },
    responses: { [ROUTES.questions]: renumbered },
  })
  const card = editor.card()
  const management = card.byClass('tpl-question-management')
  assert.equal(management.tagName, 'details', 'whole-question edits stay folded away')
  assert.equal(management.byTag('summary')[0].text, '题目管理')
  assert.deepEqual(buttonTexts(management), ['在前面插入题目', '末尾追加题目', '删除整道题'])
  assert.match(management.text, /以下操作增删整道题及答案，不是删除图上的框/)
  assert.match(management.text, /新增时请输入题号（仅数字和点）；其他题号保持不变/)
  const deleteSelected = card.find(el => el.tagName === 'button' && el.text === '删除所选')
  assert.equal(management.contains(deleteSelected), false, '删除所选 is a marker tool, outside 题目管理')
  assert.ok(card.byClass('tpl-add-tools').contains(deleteSelected))

  // 删除所选 removes only the picked marker: the question stays and nothing is sent.
  await pickInLegend(editor, 'Q1 作答框 2')
  await clickIn(editor, deleteSelected)
  assert.match(card.text, /2 个题号 · 2 个作答框/)
  assert.deepEqual(editor.questionEdits(), [])
  assert.deepEqual(editor.page.setup.templatePages[0].questions.map(item => item.question_no), ['1', '2'])

  // A whole-question insert sends the number the teacher typed, unchanged.
  await editor.select(management.byTag('select')[0], '2')
  editor.prompt('2a')
  await clickIn(editor, management.byText('button', '末尾追加题目'))
  assert.ok(editor.warnings().includes('只能输入数字和点；开头与结尾必须是数字；点不能连续'))
  assert.deepEqual(editor.questionEdits(), [])

  editor.prompt('1.1')
  await clickIn(editor, management.byText('button', '在前面插入题目'))
  const prompt = editor.page.boxes.filter(box => box.kind === 'prompt').at(-1)
  assert.equal(prompt.title, '在第 2 题前新增')
  assert.deepEqual(editor.questionEdits(), [{ page_index: 0, action: 'insert', before_question_number: '2', new_question_number: '1.1' }])
  // The unsaved marker deletion was saved first, so the renumbering cannot overwrite it.
  assert.equal(editor.layoutSaves().length, 1)
  assert.deepEqual(regionOf(editor.layoutSaves()[0].template_answer_regions, 1).regions, [[100, 100, 300, 160]])

  // The page takes the server's numbering; its answer and handwriting boxes are one collection.
  assert.match(card.text, /3 个题号 · 3 个作答框/)
  const preview = editor.preview()
  assert.ok(preview.handwritingBoxes === preview.answerBoxes, 'one collection under both names')
  assert.deepEqual(preview.answerBoxes.map(box => box.question_number), ['1', '2', '1.1'])
  assert.deepEqual(editor.page.setup.templatePages[0].questions.map(item => item.question_no), ['1', '1.1', '2'])
  assert.ok(editor.client.messages.some(message => message.type === 'success' && message.message === '已新增第 1.1 题'))

  editor.answer(ROUTES.questions, { success: true, action: 'delete', pages: [{ page_index: 0, questions: [question('1'), question('2')] }] })
  await editor.select(management.byTag('select')[0], '1.1')
  await clickIn(editor, management.byText('button', '删除整道题'))
  const confirmBox = editor.page.boxes.at(-1)
  assert.equal(confirmBox.kind, 'confirm')
  assert.match(confirmBox.message, /若只删除图上的框，请取消并使用“删除所选”/)
  assert.deepEqual(editor.questionEdits().at(-1), { page_index: 0, action: 'delete', question_number: '1.1' })
  assert.equal(editor.page.setup.templatePages[0].questions.length, 2)
  assert.ok(editor.storage()[CHECKPOINT_KEY], 'question edits keep the unfinished template recoverable')
})

test('整题增删以 PUT 发往模板题目接口，原样携带教师输入的题号并返回服务端编号', async () => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'tok-question' } })
  const renumbered = { success: true, questions: [{ question_number: '2.1' }, { question_number: '3' }] }
  client.respond(() => ({ data: renumbered }))
  const insert = { page_index: 0, action: 'insert', before_question_number: '3', new_question_number: '2.1' }

  const result = await client.api.grade.templateQuestionEdit('tpl 1/2', insert)

  assert.deepEqual(result, renumbered)
  const remove = { page_index: 0, action: 'delete', question_number: '3' }
  await client.api.grade.templateQuestionEdit('tpl 1/2', remove)
  assert.equal(client.requests.length, 2)
  client.requests.forEach((request) => {
    assert.equal(request.method, 'put')
    assert.equal(request.url, '/batch-grade/template/tpl%201%2F2/questions')
    assert.equal(request.headers.Authorization, 'Bearer tok-question')
  })
  assert.deepEqual(client.requests.map((request) => JSON.parse(request.data)), [insert, remove])
})

// Whether a template is blank is not the teacher's choice any more; the
// recognized mode only decides the legacy answer-region field of a save.
async function editBoxesAndSave(t, mode) {
  const editor = await openTemplateEditor(t, {
    checkpoint: { template_mode: mode },
    recognition: { template_mode: mode },
    questions: [[question('1')], [question('2')]],
    cut: { pages: [
      cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])] }),
      cutPage(1, { question_anchors: [anchor('2', [10, 10, 40, 30])] }),
    ] },
  })
  const dialog = editor.dialog()
  assert.match(dialog.text, /紫色豁免框用于去除模板笔迹/)
  assert.doesNotMatch(dialog.text, /红色题号框/)
  assert.deepEqual(buttonTexts(editor.card(0), el => el.classList.contains('tpl-tool-btn') && !el.classList.contains('tpl-delete-selected')),
    ['红色题号', '蓝色作答框', '绿色画图区', '作图题题号'])

  // Page 1: only a drawing area. Page 2: two answer boxes, one deleted again, and a drawing area.
  await editor.draw('绿色画图区', '1', [500, 500, 700, 700], 0)
  await editor.draw('蓝色作答框', '2', [100, 100, 300, 200], 1)
  await editor.draw('蓝色作答框', '2', [100, 250, 300, 350], 1)
  assert.match(editor.card(1).text, /1 个题号 · 2 个作答框 · 0 个作图框/)
  assert.equal(editor.card(1).allByClass('tpl-handwriting-region').length, 2, 'both answer boxes are drawn on the page')
  await pickInLegend(editor, 'Q2 作答框 2', 1)
  await clickIn(editor, editor.card(1).find(el => el.tagName === 'button' && el.text === '删除所选'))
  assert.match(editor.card(1).text, /1 个题号 · 1 个作答框 · 0 个作图框/)
  await editor.draw('绿色画图区', '2', [500, 500, 700, 700], 1)
  assert.match(editor.card(1).text, /1 个题号 · 1 个作答框 · 1 个作图框/)
  assert.deepEqual(editor.card(1).allByClass('tpl-drawing-region').map(el => el.style.left), ['50%'])

  await editor.save()
  return editor
}

test('空白与有痕迹模板都允许教师查看、编辑并保存多个豁免框', async (t) => {
  const saved = {}
  for (const mode of ['blank', 'marked']) {
    const editor = await editBoxesAndSave(t, mode)
    const saves = editor.layoutSaves()
    assert.deepEqual(saves.map(body => body.page_index), [0, 1], `${mode}: both edited pages are saved`)
    saved[mode] = saves
    assert.equal(editor.confirms().length, 1, `${mode}: the template is confirmed`)
    editor.unmount()
  }
  for (const mode of ['blank', 'marked']) {
    const [first, second] = saved[mode]
    // Every box, drawing areas included, is a handwriting region in both modes.
    assert.deepEqual(regionOf(first.template_handwriting_regions, 1).regions, [[500, 500, 700, 700]], mode)
    assert.deepEqual(regionOf(second.template_handwriting_regions, 2).regions, [[100, 100, 300, 200], [500, 500, 700, 700]], mode)
    // Answer regions: the text boxes, once a page has them (or always for a blank template).
    assert.deepEqual(regionOf(second.template_answer_regions, 2).regions, [[100, 100, 300, 200]], mode)
    for (const item of [regionOf(first.template_handwriting_regions, 1), regionOf(second.template_handwriting_regions, 2)]) {
      assert.deepEqual([item.human_confirmed, item.confidence_source], [true, 'teacher'], `${mode}: drawn boxes are the teacher's`)
    }
  }
  assert.deepEqual(saved.blank[0].template_answer_regions, [{ question_number: '1', regions: [] }])
  assert.equal(Object.hasOwn(saved.marked[0], 'template_answer_regions'), false, 'a marked page without answer boxes sends no answer regions')
  assert.deepEqual(saved.marked[0].template_handwriting_regions, saved.blank[0].template_handwriting_regions)
  assert.deepEqual(saved.marked[1].template_handwriting_regions, saved.blank[1].template_handwriting_regions)
})

const fixedReference = () => ({
  version: 'drawing-reference-v1',
  coordinate_space: 'normalized_1000_v1',
  comparison_mode: 'fixed',
  variants: [{ strokes: [{ kind: 'polyline', points: [[520, 520], [680, 680]] }] }],
  context_regions: [],
  teacher_confirmed: true,
})

function questionCard(editor, pageIndex, questionIndex) {
  return editor.dialog().find(el => el.id === `template-question-${pageIndex}-${questionIndex}`)
}

const textarea = (el, placeholder) => el.find(node => node.tagName === 'textarea' && node.attributes.placeholder === placeholder)

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

const CORRECTION_CANDIDATES = { candidates: [{ question_no: '1', candidates: [{ cycle_id: 'c-1', student_id: 3, student_name: '张三', relation: 'same_original', historical_question: '旧题' }] }] }

test('题干或题型编辑只使作图参考失效，不再自动请求答案草稿', async (t) => {
  const correction = deferred()
  const drawing = number => question(number, { answer_kind: 'drawing', drawing_reference: fixedReference() })
  const editor = await openTemplateEditor(t, {
    questions: [[drawing('1'), drawing('2')]],
    cut: { pages: [cutPage(0, {
      question_anchors: [anchor('1', [10, 10, 40, 30]), anchor('2', [10, 400, 40, 420])],
      question_schema: [
        { question_number: '1', answer_kind: 'drawing', drawing_regions: [[500, 500, 700, 700]], label_anchor: [490, 500] },
        { question_number: '2', answer_kind: 'drawing', drawing_regions: [[500, 750, 700, 900]], label_anchor: [490, 750] },
      ],
    })] },
    responses: { [ROUTES.correction]: () => correction.promise.then(() => ({ status: 200, data: CORRECTION_CANDIDATES })) },
  })
  const summary = index => questionCard(editor, 0, index).byClass('drawing-reference-summary').text
  assert.match(summary(0), /已随模板保存/)
  assert.match(summary(1), /已随模板保存/)
  const sent = () => [...editor.client.requests.map(config => config.url), ...editor.fetch.requests.map(request => request.url)]
  const before = sent().length

  const first = questionCard(editor, 0, 0)
  await editor.type(textarea(first, '请输入题干'), '改过的题干')
  assert.match(summary(0), /随保存模板一起确认/, 'a text edit only asks to re-check the teacher-drawn figure')
  assert.equal(textarea(first, '请输入标准答案').value, '答案1', 'the standard answer stays as the teacher saw it')
  assert.equal(editor.page.setup.templatePages[0].questions[0].drawing_reference.variants.length, 1, 'the figure is kept')

  await editor.choose(questionCard(editor, 0, 1).find(el => el.tagName === 'select' && el.classList.contains('tpl-q-type')), 'solve')
  assert.match(summary(1), /随保存模板一起确认/)
  assert.equal(editor.page.setup.templatePages[0].questions[1].question_type, 'solve')
  assert.equal(sent().length, before, 'editing the question sends nothing')
  assert.equal(sent().some(url => /answer-draft/.test(url)), false)

  // Correction candidates requested for the old question text are dropped.
  correction.resolve()
  await editor.settle()
  assert.equal(editor.dialog().text.includes('发现可纠错题目'), false)
})

test('学生答案不属于纠错候选的题目签名：只改学生答案时候选照常显示', async (t) => {
  const correction = deferred()
  const editor = await openTemplateEditor(t, {
    questions: [[question('1', { student_answer: '' })]],
    cut: { pages: [cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])] })] },
    responses: { [ROUTES.correction]: () => correction.promise.then(() => ({ status: 200, data: CORRECTION_CANDIDATES })) },
  })
  editor.page.setup.templatePages[0].questions[0].student_answer = '模板上残留的手写答案'
  correction.resolve()
  await editor.settle()
  assert.match(questionCard(editor, 0, 0).text, /发现可纠错题目/)
  assert.match(questionCard(editor, 0, 0).text, /张三/)
})

test('保存以老师可见答案为准，不要求再次生成', async (t) => {
  const cut = deferred()
  const confirm = deferred()
  const bank = deferred()
  // The page's question list has Q2; the model found no position for it.
  const page = cutPage(0, {
    question_anchors: [anchor('1', [10, 10, 40, 30])],
    question_schema: [{ question_number: '1', answer_kind: 'non_graphic' }, { question_number: '2', answer_kind: 'non_graphic' }],
    template_answer_regions: [regions('1', [[100, 100, 300, 200]])],
  })
  const editor = await openTemplateEditor(t, {
    timers: true,
    questions: [[question('1'), question('2')]],
    cut: { pages: [page] },
    streams: { [ROUTES.cutRun]: () => cut.promise.then(() => new Response(JSON.stringify({ status: 'complete', template_id: 'tpl-1', pages: [page] }), { status: 200 })) },
    responses: {
      [ROUTES.confirm]: config => confirm.promise.then(() => ({ status: 200, data: { success: true, page_count: JSON.parse(config.data).pages.length } })),
      [ROUTES.bankImport]: () => bank.promise.then(() => ({ status: 200, data: { success: true, imported_count: 2 } })),
    },
  })
  const saveButton = () => editor.button('保存模板（全部页）') ?? editor.button('保存中...')
  const importButton = () => editor.dialog().find(el => el.tagName === 'button' && /写入私库|入库中/.test(el.text))

  // While the crop reruns the template cannot be saved.
  await editor.click('重新自动裁剪')
  assert.ok(saveButton().hasAttribute('disabled'))
  cut.resolve()
  await editor.settle()
  assert.equal(saveButton().hasAttribute('disabled'), false)

  // The confirmation gate: Q2 has no position on the page yet.
  assert.match(editor.alert(), /缺失题号 2/)
  assert.ok(importButton().hasAttribute('disabled'))
  await editor.save()
  assert.deepEqual(editor.warnings(), ['请补齐缺失题号定位：2'])
  assert.equal(editor.confirms().length, 0)

  await editor.draw('红色题号', '2', [10, 400, 40, 420])
  await editor.type(textarea(questionCard(editor, 0, 0), '请输入标准答案'), '42')
  assert.equal(editor.alert(), '')
  assert.equal(importButton().hasAttribute('disabled'), false)

  await clickIn(editor, importButton())
  assert.equal(saveButton().text, '保存中...')
  assert.ok(saveButton().hasAttribute('disabled'), 'saving cannot be clicked twice')
  assert.ok(importButton().hasAttribute('disabled'))
  confirm.resolve()
  await editor.settle()
  assert.equal(editor.dialog().attributes['data-open'], 'false', 'the saved template closes the editor')

  // Reopened while the one-time import is still running: neither the import
  // nor a new save can start until it is done.
  await editor.click('再次编辑模板')
  assert.equal(editor.dialog().attributes['data-open'], 'true')
  assert.equal(importButton().text, '入库中...')
  assert.ok(importButton().hasAttribute('disabled'))
  assert.equal(saveButton().text, '保存模板（全部页）')
  assert.ok(saveButton().hasAttribute('disabled'), 'no save while the template is being imported')
  bank.resolve()
  await editor.settle()
  assert.equal(importButton().text, '已写入私库')
  assert.equal(saveButton().hasAttribute('disabled'), false)
  // The teacher closes the editor (the dialog's close button).
  editor.dialog().handlers['update:modelValue'](false)
  await editor.settle()

  const writes = editor.client.requests.map(config => `${config.method} ${config.url}`).filter(route => !route.startsWith('get ') && !route.includes('correction-candidates'))
  assert.deepEqual(writes, [
    'put /batch-grade/template/tpl-1/layout-overrides',
    'post /batch-grade/template/confirm',
    'post /batch-grade/template/tpl-1/bank-import',
  ])
  assert.equal([...editor.client.requests, ...editor.fetch.requests].some(request => /answer-draft/.test(request.url)), false)
  const [confirmed] = editor.confirms()
  assert.deepEqual(confirmed.pages[0].questions.map(item => [item.question_no, item.standard_answer]), [['1', '42'], ['2', '答案2']])
  assert.equal(editor.dialog().attributes['data-open'], 'false')
  assert.match(editor.text(), /题目已一次性写入私库，后续模板修改不会重复入库/)

  // Reopened, the template can still be saved, but it is not imported twice.
  await editor.click('再次编辑模板')
  assert.equal(importButton().text, '已写入私库')
  assert.ok(importButton().hasAttribute('disabled'))
  const sent = editor.client.requests.length
  assert.equal(await editor.page.setup.onImportTemplateBank(), false)
  assert.equal(editor.client.requests.length, sent)
  assert.match(saveButton().text, /保存模板（全部页）/)
})

const importButtonOf = editor => editor.dialog().find(el => el.tagName === 'button' && /写入私库/.test(el.text))

test('模板几何风险会消费服务端复核状态并禁用确认', async (t) => {
  for (const key of ['layout_review_pending_pages', 'anchor_review_pending_pages', 'geometry_review_pending_pages']) {
    const editor = await openTemplateEditor(t, {
      cut: { [key]: [0], pages: [cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])], template_answer_regions: [regions('1', [[100, 100, 300, 200]])] })] },
    })
    // The server's review list alone holds the page for the teacher.
    assert.equal(editor.alert(), '模板定位仍需教师复核：栏边界 第1页', key)
    assert.match(editor.card().text, /已调整 · 随模板保存/, `${key}: the page counts as an unsaved teacher decision`)
    assert.ok(importButtonOf(editor).hasAttribute('disabled'), `${key} blocks confirmation`)
    assert.equal(editor.page.setup.templateCanConfirm, false, key)

    // Saving the page is the teacher's review: the page is sent, the list consumed, the template confirmed.
    await editor.save()
    assert.deepEqual(editor.layoutSaves().map(body => body.page_index), [0], key)
    assert.deepEqual(editor.page.setup.templateCutResult[key], [], key)
    assert.equal(editor.confirms().length, 1, key)
    assert.deepEqual(editor.warnings(), [], key)
    editor.unmount()
  }
})

// Each local geometry risk, as the model's result can carry it: what the
// review alert names and what the save (which then sends nothing) says.
const GEOMETRY_RISKS = {
  '缺失题号': {
    questions: [[question('1'), question('2')]],
    page: { question_anchors: [anchor('1', [10, 10, 40, 30])], question_schema: [{ question_number: '1' }, { question_number: '2' }] },
    alert: '缺失题号 2',
    warning: '请补齐缺失题号定位：2',
  },
  '重复题号': {
    page: { question_anchors: [anchor('1', [10, 10, 40, 30]), anchor('1', [10, 400, 40, 420])] },
    alert: '重复题号 1',
    warning: '请处理重复题号定位：1',
  },
  '未绑定题号': {
    page: { question_anchors: [anchor('1', [10, 10, 40, 30]), anchor('9', [10, 400, 40, 420])] },
    alert: '未绑定题号 9',
    warning: '请处理未绑定到模板题目的题号定位：9',
  },
  // A model-listed answer slot that has no box on the page yet. The editor
  // stages an empty placeholder for it only on a page the server holds for
  // geometry review, so that page is already pending (栏边界 第1页); the review
  // summary names the placeholder 作答区域框越界, and the save stops at the
  // slot's own pre-save check, not at the out-of-page warning. (No answer box
  // outside the page can reach the editor on its own: loading clamps boxes
  // into the page and drops the ones wholly outside, see the next test.)
  '作答槽未框选': {
    questions: [[question('1', { slot_ids: ['1[1]', '1[2]'], region_kinds: ['answer', 'answer'], answer_slots: ['a', 'b'], blank_count: 2, standard_answer: 'a; b' })]],
    page: {
      geometry_review_pending: true,
      question_anchors: [anchor('1', [10, 10, 40, 30])],
      question_schema: [{ question_number: '1', slot_ids: ['1[1]', '1[2]'], region_kinds: ['answer', 'answer'] }],
      template_answer_regions: [regions('1', [[100, 100, 300, 200]], { slot_ids: ['1[1]'] })],
    },
    alert: '栏边界 第1页；作答区域框越界',
    warning: 'Q1[2] 还没有框选位置',
  },
  '低置信题目归属': {
    page: { question_anchors: [anchor('1', [10, 10, 40, 30], { confidence: 0.2 })] },
    alert: '低置信题目归属',
    warning: '低置信题目归属或作答区域需要人工复核：1',
  },
  '低置信作答区域': {
    page: { question_anchors: [anchor('1', [10, 10, 40, 30])], template_answer_regions: [regions('1', [[100, 100, 300, 200]], { confidence: 0.2 })] },
    alert: '低置信作答区域',
    warning: '低置信题目归属或作答区域需要人工复核：1',
  },
  // The model found no geometry at all: saving the page cannot resolve its diagnostic.
  '模型诊断': {
    page: {
      question_anchors: [],
      anchor_diagnostics: { needs_review: true, review_reasons: ['模型未找到第 1 题题号'] },
    },
    alert: '模型诊断待复核（第1页：模型未找到第 1 题题号）',
    warning: '模型定位或逐图关联仍需人工复核，请核对后重新保存模板（第1页：模型未找到第 1 题题号）',
  },
}

for (const [risk, { questions = [[question('1')]], page, alert, warning }] of Object.entries(GEOMETRY_RISKS)) {
  test(`模板几何风险：${risk}时禁用确认并说明原因`, async (t) => {
    const editor = await openTemplateEditor(t, { questions, cut: { pages: [cutPage(0, page)] } })
    assert.ok(editor.alert().includes(alert), editor.alert())
    assert.ok(importButtonOf(editor).hasAttribute('disabled'))
    await editor.save()
    assert.ok(editor.warnings().some(text => text.includes(warning)), JSON.stringify(editor.warnings()))
    assert.equal(editor.confirms().length, 0)
    assert.equal(editor.dialog().attributes['data-open'], 'true')
  })
}

test('模板几何风险：越出页面的框在载入时收回页面内，不会带着越界坐标保存', async (t) => {
  const editor = await openTemplateEditor(t, {
    questions: [[question('1'), question('2')]],
    cut: { pages: [cutPage(0, {
      geometry_review_pending: true,
      question_anchors: [anchor('1', [950, 10, 1100, 30]), anchor('2', [1200, 400, 1300, 420])],
      template_answer_regions: [regions('1', [[-50, 100, 300, 200]])],
    })] },
  })
  const card = editor.card()
  const anchors = card.allByClass('tpl-question-anchor')
  assert.equal(anchors.length, 1, 'a box wholly outside the page is not drawn')
  assert.deepEqual(anchors[0].style, { left: '95%', top: '1%', width: '5%', height: '2%' })
  assert.deepEqual(answerBox(card, 1).style, { left: '0%', top: '10%', width: '30%', height: '10%' })
  await editor.save()
  const [saved] = editor.layoutSaves()
  assert.deepEqual(saved.question_anchors.map(item => [item.question_number, item.question_number_bbox]), [['1', [950, 10, 1000, 30]], ['2', []]])
  assert.deepEqual(regionOf(saved.template_answer_regions, 1).regions, [[0, 100, 300, 200]])
})

test('语义待复核候选只初始化编辑器，教师保存后清除候选与待复核标记', async (t) => {
  const editor = await openTemplateEditor(t, {
    questions: [[question('1')], [question('2')]],
    cut: {
      geometry_review_pending_pages: [0],
      pages: [
        cutPage(0, {
          geometry_review_pending: true,
          question_anchors: [],
          question_anchor_candidates: [anchor('1', [10, 10, 40, 30], { confidence: 0.4 })],
          template_answer_regions: [],
          template_answer_region_candidates: [regions('1', [[100, 100, 300, 200]])],
          template_handwriting_regions: [],
          template_handwriting_region_candidates: [regions('1', [[100, 220, 300, 300]])],
          modes: { question: { layout_status: 'needs_review', anchor_status: 'needs_review' } },
        }),
        // Not under review: the approved geometry wins over stale candidates.
        cutPage(1, {
          question_anchors: [anchor('2', [10, 10, 40, 30])],
          question_anchor_candidates: [anchor('2', [500, 500, 540, 520])],
          template_answer_regions: [regions('2', [[100, 100, 300, 200]])],
          template_answer_region_candidates: [regions('2', [[600, 600, 800, 700]])],
        }),
      ],
    },
  })
  assert.match(editor.card(0).text, /1 个题号 · 2 个作答框/, 'the candidates seed the editor under review')
  assert.deepEqual(editor.card(0).byClass('tpl-question-anchor').style, { left: '1%', top: '1%', width: '3%', height: '2%' })
  assert.match(editor.card(1).text, /1 个题号 · 1 个作答框/)
  assert.deepEqual(answerBox(editor.card(1), 2).style, { left: '10%', top: '10%', width: '20%', height: '10%' })
  assert.match(editor.alert(), /栏边界 第1页/)

  await editor.save()
  assert.deepEqual(editor.layoutSaves().map(body => body.page_index), [0], 'only the page under review is saved')
  const [saved] = editor.layoutSaves()
  assert.deepEqual(saved.question_anchors.map(item => [item.question_number, item.question_number_bbox, item.human_confirmed]), [['1', [10, 10, 40, 30], true]])
  assert.deepEqual(regionOf(saved.template_answer_regions, 1).regions, [[100, 100, 300, 200], [100, 220, 300, 300]])
  assert.equal(editor.confirms().length, 1, 'with the review markers gone the template is confirmed')

  const result = editor.page.setup.templateCutResult
  const [resultPage] = result.pages
  for (const field of ['question_anchor_candidates', 'template_answer_region_candidates', 'template_handwriting_region_candidates',
    'layout_review_pending', 'anchor_review_pending', 'geometry_review_pending']) {
    assert.equal(Object.hasOwn(resultPage, field), false, `${field} is gone from the crop result`)
  }
  assert.deepEqual(result.geometry_review_pending_pages, [])
  assert.equal(resultPage.modes.question.layout_status, 'ready')
  assert.equal(resultPage.modes.question.anchor_status, 'ready')
  assert.deepEqual(resultPage.question_anchors.map(item => item.question_number), ['1'])
  const preview = editor.preview(0)
  for (const flag of ['layoutReviewPending', 'anchorReviewPending', 'geometryReviewPending']) assert.equal(preview[flag], false, flag)
  for (const flag of ['layout_review_pending', 'anchor_review_pending', 'geometry_review_pending']) assert.equal(Object.hasOwn(preview, flag), false, flag)
  const recognized = editor.page.setup.templatePages[0]
  assert.equal(Object.hasOwn(recognized, 'layout_review_pending'), false)
  assert.equal(recognized.layoutReviewPending, false)
})

const columnSelect = card => card.find(el => el.tagName === 'select' && el.parentNode?.classList.contains('tpl-column-count'))
const selectedText = select => select.options.find(option => option.selected)?.text

test('复核页不把等分栏伪装成模型检测结果，默认栏数为双栏', async (t) => {
  // Before any template: the upload form proposes two columns.
  const fresh = await openTemplateEditor(t, { resume: false })
  const modeSelect = fresh.page.root.find(el => el.id === 'template-column-mode')
  assert.equal(selectedText(modeSelect), '双栏')
  fresh.unmount()

  const editor = await openTemplateEditor(t, {
    questions: [[question('1')], [question('2')], [question('3')]],
    cut: { pages: [
      cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])] }),
      cutPage(1, {
        anchor_crop_plan: { parameters: { columns: [] }, blocks: [] },
        modes: { question: { column_layout: { columns: [{ x1_1000: 0, x2_1000: 480 }, { x1_1000: 480, x2_1000: 1000 }] } } },
        question_anchors: [anchor('2', [10, 10, 40, 30])],
      }),
      cutPage(2, { anchor_crop_plan: { parameters: {}, blocks: [] }, question_anchors: [anchor('3', [10, 10, 40, 30])] }),
    ] },
  })
  const columnLabels = card => card.allByClass('tpl-column-overlay').map(el => [el.text, el.style.left, el.style.width])
  // The crop plan: the model's columns, confirmed.
  assert.deepEqual(columnLabels(editor.card(0)), [['第1栏', '0%', '50%'], ['第2栏', '50%', '50%']])
  assert.equal(selectedText(columnSelect(editor.card(0))), '双栏')
  assert.doesNotMatch(editor.card(0).text, /已调整|请核对本页定位/)
  // Only the question-mode layout: shown as the model's candidate, unconfirmed and unsaved.
  assert.deepEqual(columnLabels(editor.card(1)), [['第1栏', '0%', '48%'], ['第2栏', '48%', '52%']])
  assert.equal(selectedText(columnSelect(editor.card(1))), '双栏')
  assert.match(editor.card(1).text, /已调整 · 随模板保存/)
  assert.deepEqual([editor.preview(1).columnsSource, editor.preview(1).columnsConfirmed], ['model_candidate', false])
  // No layout at all: no invented equal split, the teacher has to pick a count.
  assert.deepEqual(columnLabels(editor.card(2)), [])
  const emptySelect = columnSelect(editor.card(2))
  assert.equal(selectedText(emptySelect), '选择栏数')
  assert.ok(emptySelect.options.find(option => option.text === '选择栏数').hasAttribute('disabled'))
  assert.match(editor.card(2).text, /请核对本页定位/)
  assert.match(editor.alert(), /栏边界 第2页、第3页/)

  await editor.save()
  assert.ok(editor.warnings().includes('第 3 页尚未设置栏数，请先选择并核对栏边界'))
  assert.deepEqual(editor.layoutSaves(), [])

  // Resetting without a count gives the two-column draft.
  editor.page.setup.resetTemplateColumns(editor.preview(2))
  await editor.settle()
  assert.deepEqual(columnLabels(editor.card(2)).map(([label]) => label), ['第1栏', '第2栏'])
  await editor.choose(columnSelect(editor.card(2)), 3)
  assert.deepEqual(columnLabels(editor.card(2)), [['第1栏', '0%', '33.3%'], ['第2栏', '33.3%', '33.4%'], ['第3栏', '66.7%', '33.3%']])
  assert.deepEqual([editor.preview(2).columnsSource, editor.preview(2).columnsConfirmed], ['teacher_draft', false])
  assert.match(editor.card(2).text, /已调整 · 随模板保存/)

  // A failed page save stops the confirmation.
  editor.answer(ROUTES.layout, () => ({ status: 422, data: { detail: '分栏越界' } }))
  await editor.save()
  assert.equal(editor.confirms().length, 0)
  assert.ok(editor.client.messages.some(message => message.type === 'error' && /第 2 页定位未保存：分栏越界/.test(message.message)), JSON.stringify(editor.client.messages))

  // While the pages are being saved their column counts cannot change.
  let release
  const held = new Promise((done) => { release = done })
  editor.answer(ROUTES.layout, config => held.then(() => ({ status: 200, data: { success: true, template_id: 'tpl-1', page_index: JSON.parse(config.data).page_index, columns: JSON.parse(config.data).columns } })))
  const before = editor.layoutSaves().length
  await editor.save()
  assert.ok([0, 1, 2].every(index => columnSelect(editor.card(index)).hasAttribute('disabled')))
  release()
  await editor.settle()
  const saves = editor.layoutSaves().slice(before)
  assert.deepEqual(saves.map(body => body.page_index), [1, 2], 'every unsaved page is saved before confirming')
  assert.deepEqual(saves[1].columns.map(column => [column.x1_1000, column.x2_1000]), [[0, 333], [333, 667], [667, 1000]])
  const order = editor.client.requests.map(config => `${config.method} ${config.url}`)
  assert.ok(order.lastIndexOf('put /batch-grade/template/tpl-1/layout-overrides') < order.indexOf('post /batch-grade/template/confirm'))
  assert.equal(editor.confirms().length, 1)

  // Reopening the saved template loads its finished crop again: the editor
  // asks the server for the crop state and shows what is stored there now,
  // not the boxes it still holds from before (Q1's marker moved on the server).
  const polls = editor.fetches(ROUTES.cutState).length
  editor.stream(ROUTES.cutState, () => jsonResponse({ status: 'complete', template_id: 'tpl-1', pages: [
    cutPage(0, { question_anchors: [anchor('1', [100, 100, 140, 130])] }),
    cutPage(1, { question_anchors: [anchor('2', [10, 10, 40, 30])] }),
  ] }))
  await editor.click('再次编辑模板')
  assert.equal(editor.dialog().attributes['data-open'], 'true')
  assert.equal(editor.fetches(ROUTES.cutState).length, polls + 1, 'reopening asks for the stored crop')
  assert.equal(editor.page.root.allByClass('tpl-cut-preview-card').length, 2, 'the pages of the stored crop')
  assert.deepEqual(editor.card(0).byClass('tpl-question-anchor').style, { left: '10%', top: '10%', width: '4%', height: '3%' })
})

// Picks one template page and starts its recognition, as the teacher does on the upload form.
async function recognizeTemplate(editor) {
  const input = editor.page.root.byClass('template-upload').parentNode.find(el => el.tagName === 'input' && el.type === 'file')
  input.files = [paperFile('template.png')]
  await input.dispatch('change')
  await editor.settle()
  await editor.click('识别模板')
  return editor.fetches(ROUTES.recognize).at(-1).init.body
}

const cutAnswer = (pages, status = 'complete') => new Response(JSON.stringify({ status, template_id: 'tpl-1', pages }), { status: 200 })

test('取消模板识别会清空当前草稿并恢复默认选项，旧裁题响应不能回写', async (t) => {
  // The defaults a fresh page starts from.
  const fresh = await openTemplateEditor(t, { resume: false })
  const category = fresh.page.root.byClass('batch-setup').byTag('select')[0]
  assert.equal(selectedText(category), '日常作业')
  assert.equal(selectedText(fresh.page.root.find(el => el.id === 'template-column-mode')), '双栏')
  fresh.unmount()

  const cutStates = []
  const cutRuns = []
  const page = cutPage(0, { question_anchors: [anchor('1', [10, 10, 40, 30])] })
  const stale = cutPage(0, { question_anchors: [anchor('9', [500, 500, 540, 520])] })
  const editor = await openTemplateEditor(t, {
    timers: true,
    checkpoint: { category: 'exam', exam_name: '期中', template_mode: 'blank', column_mode: 'triple' },
    recognition: { template_mode: 'blank', column_mode: 'triple', exam_name: '期中' },
    // The first state answer is immediate (the reloaded draft), later ones wait for the test.
    cut: (request) => {
      if (!cutStates.length) {
        cutStates.push({ request })
        return cutAnswer([page])
      }
      const state = { request, ...deferred() }
      cutStates.push(state)
      return state.promise
    },
    streams: {
      [ROUTES.cutRun]: (request) => {
        const run = { request, ...deferred() }
        cutRuns.push(run)
        return run.promise
      },
      [ROUTES.recognize]: [['done', { template_id: 'tpl-1', pages: [{ page_index: 0, page_label: 'page-0', questions: [question('1')] }] }]],
    },
  })
  const columnMode = () => selectedText(editor.page.root.find(el => el.id === 'template-column-mode'))
  const previewCards = () => editor.page.root.allByClass('tpl-cut-preview-card').length
  const imageRequests = () => editor.client.requests.filter(config => config.url.includes('/pages/')).length
  assert.equal(columnMode(), '三栏')
  assert.equal(previewCards(), 1)
  const revoked = t.mock.method(URL, 'revokeObjectURL', () => {})
  const imageUrl = editor.preview().imageUrl

  await editor.click('取消')
  assert.ok(revoked.mock.calls.some(call => call.arguments[0] === imageUrl), 'the page image is released')
  assert.equal(editor.dialog().attributes['data-open'], 'false')
  assert.equal(editor.storage()[CHECKPOINT_KEY], undefined, 'the draft is not offered again after a reload')
  assert.ok(editor.client.messages.some(message => message.message === '已取消本次模板识别，当前模板已清空'))
  assert.equal(columnMode(), '双栏')
  assert.ok(editor.page.root.byClass('template-upload'), 'the template can be imported again')

  // The next recognition starts from the default options.
  let form = await recognizeTemplate(editor)
  assert.deepEqual([form.get('template_mode'), form.get('column_mode'), form.get('layout_mode')], ['marked', 'double', 'double'])
  assert.equal(cutStates.length, 2)
  assert.equal(editor.button('取消').hasAttribute('disabled'), false)
  await editor.click('取消')

  // The same template recognized again: a newer run of the same template id.
  form = await recognizeTemplate(editor)
  assert.equal(form.get('template_mode'), 'marked')
  assert.equal(cutStates.length, 3)
  const images = imageRequests()
  // The cancelled run's crop state arrives late and must not land in the new draft.
  cutStates[1].resolve(cutAnswer([stale]))
  await editor.settle()
  assert.equal(editor.page.setup.templateCutResult, null)
  assert.equal(previewCards(), 0)
  assert.equal(imageRequests(), images)
  assert.equal(cutRuns.length, 0)

  // The current run has no crop yet, so it starts one; while it runs the draft cannot be cancelled.
  cutStates[2].resolve(cutAnswer([], 'not_run'))
  await editor.settle()
  assert.equal(cutRuns.length, 1)
  assert.ok(editor.button('取消').hasAttribute('disabled'))
  // The page's own cancel (no button reaches it while cropping) stops the running crop.
  editor.page.setup.cancelTemplateDialog()
  await editor.settle()
  assert.equal(cutRuns[0].request.init.signal.aborted, true)
  cutRuns[0].resolve(cutAnswer([stale]))
  await editor.settle()
  assert.equal(editor.page.setup.templateCutResult, null)
  assert.equal(previewCards(), 0)
  assert.equal(editor.dialog().attributes['data-open'], 'false')
})

// Q5 answers in two positions: a text slot and a drawing slot whose box is
// the question's drawing area.
const SLOTTED_DRAWING = Object.freeze({ slot_ids: ['5[1]', '5[2]'], region_kinds: ['answer', 'drawing'] })
const DRAWING_AREA = [500, 760, 800, 950]

test('教师保存定位会携带并升级人工 provenance，低置信夹具默认仍待复核', async (t) => {
  const editor = await openTemplateEditor(t, {
    questions: [[question('1'), question('2'), question('3'), question('5', {
      ...SLOTTED_DRAWING, question_type: 'solve', answer_kind: 'mixed', blank_count: 2, answer_slots: ['x=1', ''], standard_answer: 'x=1',
    })], [question('4')]],
    cut: { pages: [
      cutPage(0, {
        question_anchors: [
          anchor('1', [10, 10, 40, 30], { confidence: 0.2, confidence_source: 'model' }),
          anchor('2', [10, 300, 40, 320], { confidence: 0.75 }),
          anchor('3', [10, 600, 40, 620], { confidence: 0.74 }),
          anchor('5', [10, 750, 40, 770], { confidence: 0.9 }),
        ],
        question_schema: [{ question_number: '5', answer_kind: 'mixed', ...SLOTTED_DRAWING, drawing_regions: [DRAWING_AREA], label_anchor: [490, 760] }],
        template_answer_regions: [
          regions('1', [[100, 100, 300, 200]], { confidence: 0.2 }),
          regions('5', [[100, 760, 300, 800], DRAWING_AREA], { slot_ids: SLOTTED_DRAWING.slot_ids, confidence: 0.9 }),
        ],
      }),
      cutPage(1, {
        question_anchors: [anchor('4', [10, 10, 40, 30], { confidence: 0.9 })],
        template_answer_regions: [regions('4', [[100, 100, 300, 200]], { confidence: 0.9 })],
        // Semantic conflicts reported by the model for this page.
        anchor_diagnostics: { needs_review: true, review_reason: 'column_identity_conflict' },
        template_ocr_diagnostics: { needs_review: true, review_reason: 'image_association_conflict' },
      }),
    ] },
  })
  assert.match(editor.alert(), /低置信题目归属；低置信作答区域；模型诊断待复核/)
  // Q5's drawing slot is drawn once, as its drawing area.
  assert.deepEqual(editor.card(0).allByClass('tpl-drawing-region').map(el => el.style.left), ['50%'])
  // The page image has loaded, so the drawing question's figure can be checked.
  for (const img of editor.card(0).findAll(el => el.tagName === 'img')) await img.dispatch('load')
  await editor.settle()

  // The diagnostics page is saved (and so confirmed by the teacher); the
  // untouched low-confidence page is still awaiting review.
  await editor.save()
  assert.deepEqual(editor.layoutSaves().map(body => body.page_index), [1])
  assert.ok(editor.warnings().includes('低置信题目归属或作答区域需要人工复核：1、3、1'), JSON.stringify(editor.warnings()))
  assert.equal(editor.confirms().length, 0)
  const resolved = editor.page.setup.templateCutResult.pages[1]
  assert.deepEqual([resolved.anchor_diagnostics.needs_review, resolved.anchor_diagnostics.resolved_by, resolved.anchor_diagnostics.resolved_from], [false, 'teacher', 'column_identity_conflict'])
  assert.deepEqual([resolved.template_ocr_diagnostics.resolved_by, resolved.template_ocr_diagnostics.resolved_from], ['teacher', 'image_association_conflict'])
  assert.doesNotMatch(editor.alert(), /模型诊断待复核/)

  // The teacher checks page 1 (nudging Q1's marker) and saves: every item goes out as teacher-confirmed.
  await editor.drag(editor.card(0).byClass('tpl-question-anchor'), [20, 20], [22, 20])
  await editor.save()
  const saved = editor.layoutSaves().at(-1)
  const errors = editor.client.messages.filter(message => message.type === 'error').map(message => message.message)
  assert.equal(saved.page_index, 0, `page 1 is saved (errors: ${JSON.stringify(errors)})`)
  const boxed = list => list.filter(item => item.regions.length)
  assert.deepEqual(boxed(saved.template_answer_regions).map(item => item.question_number), ['1', '5'])
  for (const item of [...saved.question_anchors, ...boxed(saved.template_answer_regions), ...boxed(saved.template_handwriting_regions)]) {
    assert.deepEqual([item.human_confirmed, item.confidence_source, item.confidence], [true, 'teacher', 1], item.question_number)
  }
  assert.deepEqual(saved.question_anchors.map(item => item.question_number), ['1', '2', '3', '5'])
  // The drawing slot goes out once, under its slot, not again as a separate drawing area.
  const slotted = regionOf(saved.template_handwriting_regions, 5)
  assert.deepEqual(slotted.regions, [[100, 760, 300, 800], DRAWING_AREA])
  assert.deepEqual(slotted.slot_ids, SLOTTED_DRAWING.slot_ids)
  assert.equal(slotted.regions.filter(box => box.join() === DRAWING_AREA.join()).length, 1)
  assert.equal(editor.confirms().length, 1)
})

// A browser parses the markup given to a <textarea> as its text: the page's
// LaTeX renderer decodes entities that way.
function parseTextareaMarkup() {
  const create = memoryDocument.createElement
  const entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }
  memoryDocument.createElement = (tag) => {
    const el = create(tag)
    if (tag === 'textarea') {
      Object.defineProperty(el, 'innerHTML', {
        configurable: true,
        get: () => el.value,
        set: (html) => {
          el.value = String(html).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (entity, name) => {
            if (name[0] === '#') return String.fromCodePoint(name[1].toLowerCase() === 'x' ? parseInt(name.slice(2), 16) : Number(name.slice(1)))
            return entities[name.toLowerCase()] ?? entity
          })
        },
      })
    }
    return el
  }
  return () => { memoryDocument.createElement = create }
}

test('学生答案显示保持原文，不把 4√5 交给模板编辑器归一化', async (t) => {
  const restore = parseTextareaMarkup()
  const row = {
    review_id: 'r1', paper_id: 'p1', student_name: '李四', disposition: 'active', category: 'daily', needs_review: 4, total: 4, correct: 0,
    details: [
      { question_no: '1', question_type: 'choice', student_answer: 'b c', correct_answer: 'B', result: 'needs_review' },
      { question_no: '3', question_type: 'solve', student_answer: '4√5', correct_answer: '2√5', result: 'needs_review' },
      { question_no: '4', question_type: 'solve', student_answer: '', correct_answer: '2', result: 'needs_review' },
      { question_no: '5', question_type: 'solve', answer_kind: 'drawing', student_answer: '作图', correct_answer: '图', result: 'needs_review' },
    ],
  }
  let upload
  try {
    upload = await mountUpload(t, {
      localStorage: { 'mathagent_review_batch_id:7': 'b1' },
      responses: { 'GET /api/batch-grade/review/b1': { batch_id: 'b1', status: 'review_ready', results: [row], summary: { total: 1 } } },
    })
  } catch (error) {
    restore()
    throw error
  }
  // The page's own stand-ins captured the textarea stand-in when it mounted
  // and put it back when it unmounts; after hooks run in the order they are
  // added, so unmount first, then restore the real document.
  t.after(() => {
    upload.unmount()
    restore()
  })
  await upload.click('原卷与批阅结果')
  const studentAnswers = upload.page.root.allByClass('manual-review-solve-row')
    .map(solve => solve.byClass('manual-review-solve-evidence').byTag('p')[0].innerHTML)
  assert.deepEqual(studentAnswers, ['4√5', '未识别到作答，请以左侧原卷为准', '作图作答，请查看下方图形证据或左侧原卷'])
  // Choice answers too are shown as the student wrote them, not normalized into options.
  const compact = upload.page.root.allByClass('manual-review-sheet-answer').map(cell => cell.byTag('span')[0].innerHTML)
  assert.equal(compact[0], 'b c')
})
