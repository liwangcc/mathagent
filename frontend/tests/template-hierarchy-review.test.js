import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { jsonResponse } from './helpers/memoryFetch.js'
import { acceptLayout, chooseValue, drag, locatedPage, openTemplateEditor, ROUTES } from './helpers/templateHierarchyEditor.js'

import {
  isTemplateDirectoryLeaf,
  mergeTemplateDrawingCandidates,
  serializeTemplateDrawingReview,
  templateDrawingReview,
  templateQuestionIdentity,
  templateQuestionMetadata,
} from '../src/utils/templateReview.js'

// The hierarchy (directory leaf) contract of the template editor on the real
// 批改 page: what it shows the teacher, what it keeps while she edits, and
// what it sends when she saves (helpers/templateHierarchyEditor.js).

const solve = (question_no, extra = {}) => ({
  question_no, question_type: 'solve', question_text: `第 ${question_no} 题题干`, standard_answer: 'x=2', ...extra,
})
const drawingQuestion = (question_no, extra = {}) => solve(question_no, { answer_kind: 'drawing', question_text: '在方格中作图', standard_answer: '', ...extra })
// Text that would re-expand a question: four blank runs and three numbered parts.
const EXPANDING_TEXT = '填空：____，____，____，____。（1）求 a；（2）求 b；（3）求 c'
const byNumber = list => Object.fromEntries(list.map(item => [String(item.question_number ?? item.question_no), item]))
const inputs = el => el.findAll(item => item.tagName === 'input')
// What an input shows (v-model leaves a number as it is; a browser would show its text).
const shown = el => String(el.value)
const pressed = el => el.hasAttribute('aria-pressed') && el.getAttribute('aria-pressed') !== 'false'
// Elements are asserted by what they show, never passed to assert themselves:
// a failing assertion would print the whole page tree.

test('template dialog stays review-only before its first preview has loaded', async (t) => {
  // The cut has not run for this template yet: opening the editor starts it,
  // and the service answers only later.
  let finishCut
  const cutRun = new Promise((resolve) => { finishCut = resolve })
  const questions = [solve('1')]
  const editor = await openTemplateEditor(t, {
    pages: [questions],
    // Recognition already placed the printed number; only the layout is missing.
    recognized: [{ question_anchors: [{ question_number: '1', question_number_bbox: [20, 40, 60, 70] }] }],
    timers: true,
    streams: {
      [ROUTES.cutState]: () => jsonResponse({ status: 'not_run' }),
      [ROUTES.cutRun]: () => cutRun,
    },
  })
  assert.equal(editor.upload.fetches(ROUTES.cutRun).length, 1)
  assert.ok(!editor.preview(0), 'no preview page has loaded')
  assert.equal(editor.reviewAlert()?.text, '模板定位仍需教师复核：栏边界 第1页')

  finishCut(jsonResponse({ status: 'complete', pages: [locatedPage(0, questions)] }))
  await editor.upload.settle()
  assert.ok(editor.preview(0))
  assert.ok(!editor.reviewAlert(), 'located geometry ends the review-only state')
  assert.equal(editor.bankEnabled(), true)
})

test('three stages preserve flat or hierarchical Q as strings and show printed path', () => {
  const examples = [
    { stage: 'primary', question_no: '2.1.3', printed_path: ['二', '1', '③'], expected: 'Q2.1.3 · 原卷 二 / 1 / ③' },
    { stage: 'junior', question_no: '3.2', printed_path: ['三', '(2)'], expected: 'Q3.2 · 原卷 三 / (2)' },
    { stage: 'senior', question_no: '17', printed_path: ['三', '17'], expected: 'Q17 · 原卷 三 / 17' },
  ]
  for (const example of examples) assert.equal(templateQuestionIdentity(example), example.expected)
  assert.equal(templateQuestionIdentity({ question_number: '12' }), 'Q12')
  assert.equal(templateQuestionIdentity({ question_no: '' }), '待绑定题号')
})

test('explicit metadata clone does not mutate identity, expose answers, or reuse drawing arrays', () => {
  const question = {
    directory_node_id: 'n006', directory_parent_id: 'n002', printed_path: ['二', '3'],
    answer_kind: 'drawing', drawing_regions: [[700, 350, 900, 500]], label_anchor: [690, 370],
    standard_answer: 'not geometry', parent_question_no: 'old-scoring-parent',
  }
  const metadata = templateQuestionMetadata(question)
  assert.deepEqual(Object.keys(metadata).sort(), ['answer_kind', 'directory_node_id', 'directory_parent_id', 'drawing_regions', 'label_anchor', 'printed_path'])
  metadata.drawing_regions[0][0] = 600
  metadata.label_anchor[0] = 590
  metadata.printed_path[0] = 'changed'
  assert.equal(question.drawing_regions[0][0], 700)
  assert.equal(question.label_anchor[0], 690)
  assert.equal(question.printed_path[0], '二')
  assert.equal(isTemplateDirectoryLeaf(question), true)
  assert.equal(isTemplateDirectoryLeaf({ question_no: '17', parent_question_no: '17' }), false)
})

test('one graphic Q per leaf, mixed question keeps multiple drawing regions, normal solve unchanged', () => {
  const schema = [
    { question_number: '2.2', question_type: 'solve', answer_kind: 'non_graphic', label_anchor: null, drawing_regions: [], printed_path: ['二', '2'] },
    { question_number: '2.3', question_type: 'solve', answer_kind: 'drawing', label_anchor: [690, 370], drawing_regions: [[700, 350, 900, 500]], directory_node_id: 'n006' },
    { question_number: '3', question_type: 'solve', answer_kind: 'mixed', label_anchor: [130, 820], drawing_regions: [[135, 770, 530, 880], [135, 890, 530, 990]], printed_path: ['三'] },
  ]
  const review = templateDrawingReview(schema)
  assert.equal(review.drawingBoxes.length, 3)
  assert.deepEqual(review.drawingLabels.map((item) => item.question_number), ['2.3', '3'])
  review.drawingLabels[0].label_anchor = [680, 360]
  review.drawingBoxes[0].bbox = [695, 340, 900, 510]
  const saved = serializeTemplateDrawingReview(schema, review.drawingBoxes, review.drawingLabels)
  assert.deepEqual(saved[0], schema[0])
  assert.deepEqual(saved[1].label_anchor, [680, 360])
  assert.deepEqual(saved[1].drawing_regions, [[695, 340, 900, 510]])
  assert.equal(saved[1].directory_node_id, 'n006')
  assert.equal(saved[2].question_number, '3')
  assert.equal(saved[2].drawing_regions.length, 2)
  assert.deepEqual(schema[1].label_anchor, [690, 370])
})

test('pending non-graphic candidates keep conflicting geometry visible for teacher resolution', () => {
  const review = templateDrawingReview([
    { question_number: '5', answer_kind: 'non_graphic', drawing_regions: [[120, 180, 720, 800]], label_anchor: [90, 220] },
  ], { includeConflictingGeometry: true })
  assert.deepEqual(review.drawingBoxes, [{ question_number: '5', bbox: [120, 180, 720, 800] }])
  assert.deepEqual(review.drawingLabels, [{ question_number: '5', label_anchor: [90, 220] }])
})

test('deleting a conflicting non-graphic marker clears stale geometry from the save payload', () => {
  const schema = [{ question_number: '5', answer_kind: 'non_graphic',
    drawing_regions: [[120, 180, 720, 800]], label_anchor: [90, 220] }]
  const saved = serializeTemplateDrawingReview(schema, [], [])[0]
  assert.deepEqual(saved.drawing_regions, [])
  assert.equal(saved.label_anchor, null)
  const pending = serializeTemplateDrawingReview(schema, [], [], { preserveConflictingGeometry: true })[0]
  assert.deepEqual(pending.drawing_regions, schema[0].drawing_regions)
  assert.deepEqual(pending.label_anchor, schema[0].label_anchor)
})

test('missing or malformed Q markers are never clamped; saving re-derives one beside the drawing area', () => {
  for (const label_anchor of [null, [1001, 50], [-1, 50], [5], ['5', 50], [NaN, 50]]) {
    const schema = [{ question_number: '2.3', answer_kind: 'drawing', drawing_regions: [[700, 350, 900, 500]], label_anchor }]
    const review = templateDrawingReview(schema)
    assert.equal(review.drawingLabels.length, 0)
    assert.deepEqual(serializeTemplateDrawingReview(schema, review.drawingBoxes, [])[0].label_anchor, [685, 425])
  }
  // Without a drawing area there is nothing to place a marker beside.
  const bare = [{ question_number: '2.3', answer_kind: 'drawing', drawing_regions: [], label_anchor: null }]
  assert.equal(serializeTemplateDrawingReview(bare, [], [])[0].label_anchor, null)
})

test('a Q marker its drawing area moved past is re-placed beside that area on save; valid markers are kept', () => {
  const schema = [{ question_number: '8', answer_kind: 'mixed', drawing_regions: [[540, 600, 900, 800]], label_anchor: [525, 700] }]
  const review = templateDrawingReview(schema)
  // The teacher moved the area left of the auto-placed marker.
  review.drawingBoxes[0].bbox = [500, 600, 860, 800]
  assert.deepEqual(serializeTemplateDrawingReview(schema, review.drawingBoxes, review.drawingLabels)[0].label_anchor, [485, 700])
  // Boundary contact and any marker left of the area are the teacher's and stay.
  for (const label_anchor of [[500, 650], [120, 90]]) {
    assert.deepEqual(serializeTemplateDrawingReview(schema, review.drawingBoxes, [{ question_number: '8', label_anchor }])[0].label_anchor, label_anchor)
  }
  // The pending-review conflict view keeps the draft as it is.
  assert.deepEqual(serializeTemplateDrawingReview(schema, review.drawingBoxes, review.drawingLabels, { preserveConflictingGeometry: true })[0].label_anchor, [525, 700])
})

test('directory region counts survive editing and confirmation without text-based re-expansion', async (t) => {
  const leaves = [
    { question_no: '2.1.3', question_type: 'fill', directory_node_id: 'n006' },
    { question_no: '2.1.4', question_type: 'solve', directory_node_id: 'n007' },
  ].map(leaf => ({
    ...leaf, directory_parent_id: 'n003', printed_path: ['二', '1', '③'],
    blank_count: 3, subquestion_count: 3, subquestion_scores: [7, 8, 9],
    question_text: EXPANDING_TEXT, standard_answer: '(5,3)；4；6',
  }))
  // Questions outside the directory still take their counts from the text.
  const legacySolve = { question_no: '17', question_type: 'solve', question_text: EXPANDING_TEXT, standard_answer: '略' }
  const legacyFill = { question_no: '18', question_type: 'fill', question_text: EXPANDING_TEXT, standard_answer: 'a；b；c；d' }
  const editor = await openTemplateEditor(t, { category: 'exam', pages: [[...leaves, legacySolve, legacyFill]] })

  const editing = byNumber(editor.page.setup.templatePages[0].questions)
  for (const { question_no } of leaves) {
    assert.equal(editing[question_no].blank_count, 3)
    assert.equal(editing[question_no].subquestion_count, 1)
    assert.deepEqual(Array.from(editing[question_no].subquestion_scores), [7])
  }
  assert.equal(editing['17'].subquestion_count, 3)
  assert.equal(editing['17'].blank_count, 4)

  // What the teacher sees: the leaf's three regions, locked, and one score.
  const leafBlanks = inputs(editor.field('2.1.3', '填空数量'))[0]
  assert.equal(shown(leafBlanks), '3')
  assert.equal(leafBlanks.hasAttribute('disabled'), true)
  assert.equal(editor.field('2.1.3', '各空标准答案').find(el => el.tagName === 'textarea').value, '(5,3)\n4\n6')
  assert.deepEqual(inputs(editor.field('2.1.4', '当前 Q 分值')).map(shown), ['7'])
  assert.ok(!editor.question('2.1.4').byText('button', '+ 增加小问'), 'a leaf gets no more parts')
  assert.deepEqual(inputs(editor.field('17', '各小问分值')).map(shown), ['5', '7', '8'])
  const legacyBlanks = inputs(editor.field('18', '填空数量'))[0]
  assert.equal(shown(legacyBlanks), '4')
  assert.equal(legacyBlanks.hasAttribute('disabled'), false)

  await editor.save()
  assert.equal(editor.confirms().length, 1, 'the template is confirmed')
  for (const payload of [editor.corrections()[0], editor.confirms()[0]]) {
    const sent = editor.sentQuestions(payload)
    for (const { question_no, question_type, directory_node_id } of leaves) {
      assert.equal(sent[question_no].blank_count, 3)
      assert.equal(sent[question_no].subquestion_count, 1)
      assert.equal(sent[question_no].directory_node_id, directory_node_id)
      assert.deepEqual(sent[question_no].printed_path, ['二', '1', '③'])
      assert.ok(sent[question_no].subquestion_scores.length <= 1)
      assert.deepEqual(sent[question_no].subquestion_scores, question_type === 'solve' ? [7] : [])
    }
    assert.equal(sent['17'].subquestion_count, 3)
    assert.equal(sent['18'].blank_count, 4)
  }
})

test('reference geometry is copied separately without changing Q metadata', async (t) => {
  const question = drawingQuestion('2.3', {
    directory_node_id: 'n006',
    drawing_reference: { teacher_confirmed: true, variants: [{ strokes: [{ kind: 'point', point: [800, 450] }] }] },
  })
  const page = locatedPage(0, [question])
  Object.assign(page.question_schema[0], { drawing_regions: [[700, 350, 900, 500]], label_anchor: [690, 370] })
  const editor = await openTemplateEditor(t, { category: 'exam', pages: [[question]], cut: [page] })

  const opened = editor.sentQuestions(editor.corrections()[0])['2.3']
  assert.equal(opened.drawing_reference.teacher_confirmed, true)
  assert.deepEqual(opened.drawing_reference.variants[0].strokes[0].point, [800, 450])
  // A serialized question shares no figure with the one being edited.
  const editing = editor.page.setup.templatePages[0].questions[0]
  const saved = editor.page.setup.answerSpecFields(editing)
  assert.equal(saved.drawing_reference.teacher_confirmed, true)
  saved.drawing_reference.variants[0].strokes[0].point[0] = 700
  assert.equal(editing.drawing_reference.variants[0].strokes[0].point[0], 800)

  await editor.save()
  const confirmed = editor.sentQuestions(editor.confirms()[0])['2.3']
  assert.deepEqual(confirmed.drawing_reference.variants[0].strokes[0].point, [800, 450])
  assert.equal(confirmed.directory_node_id, 'n006')
  assert.equal(templateQuestionMetadata(question).drawing_reference, undefined)
})

test('a stale figure on a question whose marks are no longer graphic is never submitted', async (t) => {
  const drawing_reference = { comparison_mode: 'manual', variants: [] }
  const base = { question_type: 'solve', question_text: '题干', slot_ids: ['3[1]'], answer_slots: ['x=2'], blank_count: 1, drawing_reference }
  const editor = await openTemplateEditor(t, { category: 'exam', pages: [[
    { ...base, question_no: '3', region_kinds: ['answer'], answer_kind: 'mixed' },
    { question_no: '4', question_type: 'solve', question_text: '题干', answer_kind: 'non_graphic', drawing_reference },
    { ...base, question_no: '5', slot_ids: ['5[1]'], region_kinds: ['drawing'], answer_kind: 'drawing' },
  ]] })
  const sent = editor.sentQuestions(editor.corrections()[0])
  assert.equal('drawing_reference' in sent['3'], false)
  assert.equal('drawing_reference' in sent['4'], false)
  assert.deepEqual(sent['5'].drawing_reference, drawing_reference)
})

test('natural Q group keeps repeated answers and an empty middle region in editor round trips', async (t) => {
  for (const question_type of ['fill', 'solve']) {
    const editor = await openTemplateEditor(t, { category: 'exam', pages: [[
      { question_no: '1', directory_node_id: 'n001', directory_parent_id: null,
        question_type, blank_count: 3, answer_slots: ['2', '', '2'], standard_answer: '2；；2' },
      { question_no: '1.1', directory_node_id: 'n002', directory_parent_id: 'n001',
        question_type: 'fill', blank_count: 1, answer_slots: ['2'] },
    ]] })
    const editing = byNumber(editor.page.setup.templatePages[0].questions)
    assert.equal(editing['1'].blank_count, 3)
    assert.equal(editing['1'].answer_slots_text, '2\n\n2')
    if (question_type === 'fill') {
      assert.equal(editor.field('1', '各空标准答案').find(el => el.tagName === 'textarea').value, '2\n\n2')
    }
    assert.equal(editing['1.1'].blank_count, 1)
    assert.equal(shown(inputs(editor.field('1.1', '填空数量'))[0]), '1')

    const sent = editor.sentQuestions(editor.corrections()[0])
    assert.equal(sent['1'].blank_count, 3)
    assert.deepEqual(sent['1'].answer_slots, ['2', '', '2'])
    assert.equal(sent['1.1'].blank_count, 1)
    assert.deepEqual(sent['1.1'].answer_slots, ['2'])
    editor.upload.unmount()
  }
})

test('preview schema is loaded, editable, saved, and refreshed after server renumbering', async (t) => {
  const identity = { directory_node_id: 'n006', printed_path: ['二', '3'] }
  const drawing = drawingQuestion('2.3', identity)
  const plain = solve('5')
  // Page 1 carries its schema only on the page artifact.
  const first = locatedPage(0, [drawing])
  delete first.question_schema
  first.artifact = { question_schema: [{ question_number: '2.3', answer_kind: 'drawing', ...identity, drawing_regions: [[700, 350, 900, 500]], label_anchor: [690, 370] }] }
  // Page 2 has a schema of its own, which wins over the artifact's.
  const second = locatedPage(1, [plain])
  second.artifact = { question_schema: [{ question_number: '5', answer_kind: 'drawing', drawing_regions: [[600, 100, 900, 300]], label_anchor: [585, 200] }] }
  // The server places the geometry anew when it saves a page.
  const placedByServer = [{ question_number: '2.3', answer_kind: 'drawing', ...identity, drawing_regions: [[700, 360, 900, 500]], label_anchor: [685, 430] }]
  const editor = await openTemplateEditor(t, {
    pages: [[drawing], [plain]],
    cut: [first, second],
    responses: {
      [ROUTES.layout]: (config) => {
        const response = acceptLayout(config)
        response.data.question_schema = structuredClone(placedByServer)
        return response
      },
      // The confirmation is refused, so the editor stays open on what the page save left.
      [ROUTES.confirm]: { success: false, detail: '模板确认暂不可用' },
    },
  })
  const { upload } = editor
  const label = () => editor.canvas(0).find(el => el.classList.contains('tpl-drawing-label'))
  const area = () => editor.canvas(0).find(el => el.classList.contains('tpl-drawing-region'))

  assert.equal(editor.counts(0), '1 个题号 · 0 个作答框 · 1 个作图框')
  assert.deepEqual(editor.legendItems(0), ['Q2.3 题号位置 1', 'Q2.3 画图区 1', 'Q2.3 作图锚点 1'])
  assert.equal(label().getAttribute('aria-label'), '拖动 Q2.3 到作图区域左侧')
  assert.deepEqual(label().style, { left: '69%', top: '37%' })
  assert.equal(editor.counts(1), '1 个题号 · 1 个作答框 · 0 个作图框')

  // The teacher drags the Q marker: it follows the pointer.
  await drag(upload, label(), { dx: -10, dy: 5 })
  assert.deepEqual(label().style, { left: '68%', top: '37.5%' })
  assert.match(editor.preview(0).text, /已调整 · 随模板保存/)

  await editor.save()
  assert.deepEqual(editor.layoutSaves().map(save => save.page_index), [0], 'only the edited page is saved')
  assert.deepEqual(editor.layoutSaves()[0].question_schema, [{
    question_number: '2.3', answer_kind: 'drawing', ...identity,
    drawing_regions: [[700, 350, 900, 500]], label_anchor: [680, 375],
  }])
  // The server's schema replaces the draft, and the answer takes its metadata.
  assert.deepEqual(label().style, { left: '68.5%', top: '43%' })
  assert.equal(area().style.top, '36%')
  const confirmed = editor.sentQuestions(editor.confirms()[0])['2.3']
  assert.deepEqual(confirmed.drawing_regions, [[700, 360, 900, 500]])
  assert.deepEqual(confirmed.label_anchor, [685, 430])
  assert.equal(confirmed.directory_node_id, 'n006')
  assert.deepEqual(confirmed.printed_path, ['二', '3'])

  // A server that answers without a schema leaves the saved draft in place.
  upload.answer(ROUTES.layout, (config) => {
    const response = acceptLayout(config)
    delete response.data.question_schema
    return response
  })
  await drag(upload, label(), { dx: 0, dy: 10 })
  await editor.save()
  assert.deepEqual(editor.layoutSaves()[1].question_schema[0].label_anchor, [685, 440])
  assert.deepEqual(label().style, { left: '68.5%', top: '44%' })
  assert.deepEqual(editor.sentQuestions(editor.confirms()[1])['2.3'].label_anchor, [685, 440])
})

test('the editor names its markers and lists them beside the image', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [[solve('1'), drawingQuestion('2.3')]] })
  const tools = editor.preview(0).find(el => el.attributes['aria-label'] === '新增定位标记')
  assert.deepEqual(tools.findAll(el => el.tagName === 'button').map(el => el.text), ['红色题号', '蓝色作答框', '绿色画图区', '作图题题号', '删除所选'])
  assert.match(editor.dialog().byClass('tpl-editor-help').text, /红色为印刷题号/)
  assert.deepEqual(editor.legendItems(0), ['Q1 题号位置 1', 'Q2.3 题号位置 1', 'Q1 作答框 1', 'Q2.3 画图区 1', 'Q2.3 作图锚点 1'])
  // Choosing an entry selects that marker on the image.
  const entry = () => editor.legend(0).byText('button', 'Q2.3 画图区 1')
  await editor.press(entry())
  assert.equal(pressed(entry()), true)
  assert.equal(editor.canvas(0).find(el => el.classList.contains('tpl-drawing-region')).classList.contains('is-selected'), true)
})

test('directory leaves lock the region counts they bring', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [[
    { question_no: '2.1.4', question_type: 'solve', directory_node_id: 'n007', subquestion_count: 3, question_text: EXPANDING_TEXT, standard_answer: '略' },
    { question_no: '2.1.3', question_type: 'fill', directory_node_id: 'n006', blank_count: 3, question_text: EXPANDING_TEXT, standard_answer: 'a；b；c' },
    { question_no: '17', question_type: 'solve', question_text: EXPANDING_TEXT, standard_answer: '略' },
    { question_no: '18', question_type: 'fill', question_text: EXPANDING_TEXT, standard_answer: 'a；b；c；d' },
  ]] })
  const count = (number, label) => inputs(editor.field(number, label))[0]
  assert.equal(shown(count('2.1.4', '独立小问数量')), '1')
  assert.equal(count('2.1.4', '独立小问数量').hasAttribute('disabled'), true)
  assert.equal(shown(count('17', '独立小问数量')), '3')
  assert.equal(count('17', '独立小问数量').hasAttribute('disabled'), false)
  assert.equal(shown(count('2.1.3', '填空数量')), '3')
  assert.equal(count('2.1.3', '填空数量').hasAttribute('disabled'), true)
  assert.equal(shown(count('18', '填空数量')), '4')
  assert.equal(count('18', '填空数量').hasAttribute('disabled'), false)
  const note = '当前 Q 下的作答区域按目录排列，区域数不从题干重新推断。'
  assert.ok(editor.question('2.1.4').text.includes(note))
  assert.ok(!editor.question('17').text.includes(note))
})

test('a question edit refreshes every preview page from the server artifact', async (t) => {
  const editor = await openTemplateEditor(t, {
    pages: [[solve('1'), solve('2')]],
    responses: {
      [ROUTES.questions]: {
        success: true,
        action: 'delete',
        pages: [{ page_index: 0, questions: [solve('1')] }],
        page_artifacts: { p1: {
          question_anchors: [{ question_number: '1', question_number_bbox: [20, 40, 60, 70] }],
          question_schema: [{ question_number: '1', answer_kind: 'mixed', drawing_regions: [[600, 40, 900, 120]], label_anchor: [585, 80] }],
          // The answer boxes and the legacy handwriting regions name one box twice.
          template_answer_regions: [{ question_number: '1', regions: [[100, 40, 230, 100]] }],
          template_handwriting_regions: [{ question_number: '1', regions: [[100, 40, 230, 100], [300, 40, 400, 100]] }],
        } },
      },
    },
  })
  const { upload } = editor
  const manage = editor.preview(0).find(el => el.tagName === 'select' && el.attributes['aria-label'] === '管理题目')
  await upload.select(manage, '2')
  await editor.press(editor.preview(0).byText('button', '删除整道题'))
  assert.deepEqual(editor.questionEdits(), [{ page_index: 0, action: 'delete', question_number: '2' }])

  assert.equal(editor.counts(0), '1 个题号 · 2 个作答框 · 1 个作图框')
  assert.deepEqual(editor.legendItems(0), ['Q1 题号位置 1', 'Q1 作答框 1', 'Q1 作答框 2', 'Q1 画图区 1', 'Q1 作图锚点 1'])
  assert.ok(!editor.question('2'), 'the deleted question leaves the answer editor')
  // One collection of answer boxes, whichever name older code reads it by.
  const [preview] = editor.page.setup.templateCutPreviewPages
  assert.equal(preview.handwritingBoxes, preview.answerBoxes)
})

test('pure drawing suppresses duplicate answer boxes and mixed drawing retains only text boxes', async (t) => {
  const columns = { anchor_crop_plan: { parameters: { columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }], column_count: 1 } } }
  const anchors = numbers => numbers.map((question_number, row) => ({ question_number, question_number_bbox: [10, 20 + row * 130, 40, 50 + row * 130] }))
  // Page 1: legacy rows repeat the pure drawing's marker and area, and the
  // mixed question's drawing area beside its text box, as answer boxes.
  const shown = {
    page_index: 0,
    page_label: 'p1',
    ...columns,
    question_anchors: anchors(['1', '2.3', '3']),
    template_answer_regions: [
      { question_number: '1', geometry_kind: 'label_anchor', regions: [[100, 100, 101, 101]] },
      { question_number: '2.3', geometry_kind: 'label_anchor', regions: [[600, 400, 601, 401]] },
      { question_number: '2.3', regions: [[700, 350, 900, 500]] },
      { question_number: '3', regions: [[100, 800, 500, 950], [600, 800, 900, 950]] },
    ],
    question_schema: [
      { question_number: '1', answer_kind: 'non_graphic' },
      { question_number: '2.3', answer_kind: 'drawing', drawing_regions: [[700, 350, 900, 500]], label_anchor: [690, 370] },
      { question_number: '3', answer_kind: 'mixed', drawing_regions: [[100, 800, 500, 950]], label_anchor: [85, 875] },
    ],
  }
  // Page 2, saved: one row per question without slots, and slots.
  const saved = {
    page_index: 1,
    page_label: 'p2',
    ...columns,
    // The model's layout waits for the teacher's save.
    layout_review_pending: true,
    question_anchors: anchors(['6', '2.5', '7', '8', '9', '10']),
    template_answer_regions: [
      { question_number: '6', geometry_kind: 'label_anchor', regions: [[100, 100, 101, 101]] },
      { question_number: '2.5', regions: [[700, 150, 900, 250]] },
      { question_number: '7', regions: [[600, 300, 900, 400]] },
      { question_number: '8', regions: [[100, 500, 500, 600]] },
      { question_number: '9', regions: [[100, 650, 200, 700], [250, 650, 350, 700]], slot_ids: ['9[1]', '9[2]'] },
      { question_number: '10', regions: [[100, 750, 200, 800], [300, 720, 500, 840]], slot_ids: ['10[1]', '10[2]'] },
    ],
    question_schema: [
      { question_number: '6', answer_kind: 'non_graphic' },
      { question_number: '2.5', answer_kind: 'drawing', drawing_regions: [[700, 150, 900, 250]], label_anchor: [690, 200] },
      { question_number: '7', answer_kind: 'mixed', drawing_regions: [[100, 300, 500, 400]], label_anchor: [85, 350] },
      { question_number: '8', answer_kind: 'mixed', drawing_regions: [[100, 500, 500, 600]], label_anchor: [85, 550] },
      { question_number: '9', answer_kind: 'non_graphic', slot_ids: ['9[1]', '9[2]'] },
      { question_number: '10', answer_kind: 'mixed', slot_ids: ['10[1]', '10[2]'], region_kinds: ['answer', 'drawing'], drawing_regions: [[300, 720, 500, 840]], label_anchor: [285, 780] },
    ],
  }
  const editor = await openTemplateEditor(t, {
    pages: [
      [solve('1'), drawingQuestion('2.3'), solve('3', { answer_kind: 'mixed' })],
      [solve('6'), drawingQuestion('2.5'), solve('7', { answer_kind: 'mixed' }), solve('8', { answer_kind: 'mixed' }),
        { question_no: '9', question_type: 'fill', question_text: '填空', standard_answer: 'a; b', slot_ids: ['9[1]', '9[2]'], answer_slots: ['a', 'b'], blank_count: 2 },
        solve('10', { answer_kind: 'mixed', slot_ids: ['10[1]', '10[2]'], region_kinds: ['answer', 'drawing'], answer_slots: ['x', ''], blank_count: 2, standard_answer: 'x' })],
    ],
    cut: [shown, saved],
  })
  const unslotted = pageIndex => editor.canvas(pageIndex).findAll(el => el.classList.contains('tpl-handwriting-region') && !el.dataset.templateSlotId)
  const visible = unslotted(0)
  assert.equal(visible.length, 2)
  assert.match(visible[0].getAttribute('title'), /^Q1 作答框/)
  assert.equal(visible[0].classList.contains('is-label-anchor'), true)
  assert.match(visible[1].getAttribute('title'), /^Q3 作答框/)
  assert.deepEqual(visible[1].style, { left: '60%', top: '80%', width: '30%', height: '15%' })
  assert.equal(editor.page.setup.templateCutPreviewPages[0].answerBoxes.length, 5, 'hiding a box never drops it from the page')
  assert.deepEqual(unslotted(1).map(el => el.getAttribute('title').split(' ')[0]), ['Q6', 'Q7'])

  await editor.save()
  assert.deepEqual(editor.layoutSaves().map(save => save.page_index), [1])
  const [page] = editor.layoutSaves()
  const regions = list => Object.fromEntries(list.map(item => [item.question_number, item.regions]))
  // Text boxes: the visible boxes without a slot, then every slot in order.
  assert.deepEqual(regions(page.template_answer_regions), {
    6: [[100, 100, 101, 101]],
    2.5: [],
    7: [[600, 300, 900, 400]],
    8: [],
    9: [[100, 650, 200, 700], [250, 650, 350, 700]],
    10: [[100, 750, 200, 800], [300, 720, 500, 840]],
  })
  // Handwriting adds the drawing areas that are not slots already.
  assert.deepEqual(regions(page.template_handwriting_regions), {
    6: [[100, 100, 101, 101]],
    2.5: [[700, 150, 900, 250]],
    7: [[600, 300, 900, 400], [100, 300, 500, 400]],
    8: [[100, 500, 500, 600]],
    9: [[100, 650, 200, 700], [250, 650, 350, 700]],
    10: [[100, 750, 200, 800], [300, 720, 500, 840]],
  })
  assert.deepEqual(byNumber(page.template_answer_regions)['10'].slot_ids, ['10[1]', '10[2]'])
})

test('pending drawing candidates edit geometry only and cannot replace server-owned Q identity', async (t) => {
  const schema = [{ question_number: '2.3', directory_node_id: 'n003', printed_path: ['二', '3'], answer_kind: 'non_graphic', drawing_regions: [], label_anchor: null }]
  const candidates = [
    { question_number: '2.3', directory_node_id: 'forged', printed_path: ['wrong'], answer_kind: 'drawing', drawing_regions: [], label_anchor: null },
    { question_number: '99', answer_kind: 'drawing', drawing_regions: [] },
  ]
  assert.deepEqual(mergeTemplateDrawingCandidates(schema, candidates, false), schema)
  const draft = mergeTemplateDrawingCandidates(schema, candidates, true)
  assert.equal(draft.length, 1)
  assert.equal(draft[0].answer_kind, 'drawing')
  assert.equal(draft[0].directory_node_id, 'n003')
  assert.deepEqual(draft[0].printed_path, ['二', '3'])
  assert.equal(schema[0].answer_kind, 'non_graphic')

  // Loading the previews: only a page still under review takes the model's
  // drawing candidates, and only their geometry.
  const located = [{ ...candidates[0], drawing_regions: [[700, 350, 900, 500]], label_anchor: [690, 370] }, candidates[1]]
  const pending = locatedPage(0, [solve('2.3')], { layout_review_pending: true, question_schema: structuredClone(schema), question_schema_candidates: structuredClone(located) })
  const settled = locatedPage(1, [solve('2.4')], {
    question_schema: [{ ...structuredClone(schema[0]), question_number: '2.4' }],
    question_schema_candidates: [{ ...structuredClone(located[0]), question_number: '2.4' }],
  })
  const editor = await openTemplateEditor(t, {
    pages: [[solve('2.3', { directory_node_id: 'n003', printed_path: ['二', '3'] })], [solve('2.4')]],
    cut: [pending, settled],
  })
  // Q2.3 is drawn now: its answer box is the drawing area's.
  assert.equal(editor.counts(0), '1 个题号 · 0 个作答框 · 1 个作图框')
  const manage = editor.preview(0).find(el => el.tagName === 'select' && el.attributes['aria-label'] === '管理题目')
  assert.deepEqual(manage.options.map(option => option.text), ['Q2.3 · 原卷 二 / 3'])
  assert.doesNotMatch(editor.dialog().text, /Q99/)
  assert.equal(editor.counts(1), '1 个题号 · 1 个作答框 · 0 个作图框')

  await editor.save()
  const [saved] = editor.layoutSaves()
  assert.equal(saved.page_index, 0)
  assert.deepEqual(saved.question_schema, [{
    question_number: '2.3', directory_node_id: 'n003', printed_path: ['二', '3'],
    answer_kind: 'drawing', drawing_regions: [[700, 350, 900, 500]], label_anchor: [690, 370],
  }])
})

test('drawing subtype changes invalidate only the affected reference and never manufacture a drawing region', async (t) => {
  const reference = () => ({ comparison_mode: 'fixed', variants: [], teacher_confirmed: true })
  const editor = await openTemplateEditor(t, { pages: [[
    { question_no: '2.3', question_type: 'fill', answer_kind: 'non_graphic', question_text: '填空', standard_answer: '8', drawing_reference: reference() },
    drawingQuestion('3', { drawing_reference: reference() }),
    solve('4', { answer_kind: 'mixed', drawing_reference: reference() }),
  ]] })
  const { upload } = editor
  const summary = number => editor.question(number).byClass('drawing-reference-summary')?.find(el => el.tagName === 'span').text ?? null
  const tool = name => editor.preview(0).byText('button', name)
  const typeSelect = number => editor.question(number).find(el => el.tagName === 'select' && el.classList.contains('tpl-q-type'))
  assert.equal(summary('3'), '标准图 0 个完整方案 · 已随模板保存')
  assert.equal(editor.counts(0), '3 个题号 · 2 个作答框 · 2 个作图框')

  // The teacher has a tool armed and a marker selected when she changes Q2.3.
  await editor.press(tool('绿色画图区'))
  await editor.press(editor.legend(0).byText('button', 'Q2.3 作答框 1'))
  assert.equal(pressed(tool('绿色画图区')), true)
  assert.equal(pressed(editor.legend(0).byText('button', 'Q2.3 作答框 1')), true)
  await chooseValue(upload, editor.drawingKind('2.3'), 'mixed')

  assert.equal(editor.drawingKind('2.3').value, 'mixed')
  assert.equal(typeSelect('2.3').options[typeSelect('2.3').selectedIndex].text, '填空')
  assert.equal(editor.counts(0), '3 个题号 · 2 个作答框 · 2 个作图框', 'choosing a subtype cannot pretend a region was localized')
  assert.doesNotMatch(editor.legendItems(0).join(' '), /Q2\.3 画图区/)
  assert.match(editor.preview(0).text, /已调整 · 随模板保存/)
  assert.equal(pressed(tool('绿色画图区')), false, 'the armed tool is put down')
  assert.equal(pressed(editor.legend(0).byText('button', 'Q2.3 作答框 1')), false, 'the selection is cleared')
  assert.equal(summary('2.3'), '标准图 0 个完整方案 · 随保存模板一起确认')
  assert.match(editor.preview(0).text, /有尚未最终保存的作图参考修改/)
  assert.equal(summary('3'), '标准图 0 个完整方案 · 已随模板保存', 'another question keeps its approved figure')

  // Existing real geometry is removed when the teacher rules out drawing;
  // creation and resizing are covered by template-geometry-tools.test.js.
  const labelOf = number => editor.canvas(0).find(el => el.getAttribute('aria-label') === `拖动 Q${number} 到作图区域左侧`)
  assert.ok(labelOf('4'))
  await chooseValue(upload, editor.drawingKind('4'), 'non_graphic')
  assert.equal(editor.drawingKind('4').value, 'non_graphic')
  assert.equal(editor.counts(0), '3 个题号 · 2 个作答框 · 1 个作图框')
  assert.deepEqual(editor.legendItems(0).filter(item => /画图区|作图锚点/.test(item)), ['Q3 画图区 1', 'Q3 作图锚点 1'])
  assert.ok(!labelOf('4'), 'its Q marker is gone')
  assert.ok(labelOf('3'))
  assert.equal(summary('4'), null)
  // Its figure went with it: drawing again starts without one.
  await chooseValue(upload, editor.drawingKind('4'), 'mixed')
  assert.equal(summary('4'), '未设置标准图；保存后将按题干和作图区直接交给 Flash 判定。')
  assert.equal(editor.counts(0), '3 个题号 · 2 个作答框 · 1 个作图框')

  await editor.loadImages()
  await editor.save()
  const schema = byNumber(editor.layoutSaves()[0].question_schema)
  assert.equal(schema['2.3'].answer_kind, 'mixed')
  assert.deepEqual(schema['2.3'].drawing_regions, [])
  assert.equal(schema['4'].answer_kind, 'mixed')
  assert.deepEqual(schema['4'].drawing_regions, [])
  assert.equal(schema['4'].label_anchor, null)
  assert.equal(schema['3'].drawing_regions.length, 1)
  const sent = editor.sentQuestions(editor.confirms()[0])
  assert.equal(sent['2.3'].question_type, 'fill')
  assert.equal(sent['2.3'].answer_kind, 'mixed')
  assert.equal(sent['2.3'].drawing_reference.teacher_confirmed, false)
  assert.equal(sent['3'].drawing_reference.teacher_confirmed, true)
  assert.equal('drawing_reference' in sent['4'], false, 'a hidden drawing reference must not prevent final confirmation of a non-graphic question')
})

test('insert placeholders remain pending until an explicit successful layout save', async (t) => {
  const anchors = [
    { question_number: '1', question_number_bbox: [20, 40, 60, 70] },
    // The inserted number sits where the server put it, not where the teacher has checked.
    { question_number: '1.1', question_number_bbox: [20, 100, 60, 130], confidence: 0.3 },
    { question_number: '2', question_number_bbox: [20, 130, 60, 160] },
  ]
  const artifact = extra => ({
    question_anchors: structuredClone(anchors),
    question_schema: anchors.map(({ question_number }) => ({ question_number, answer_kind: 'non_graphic' })),
    template_answer_regions: anchors.map(({ question_number }, row) => ({ question_number, regions: [[100, 40 + row * 90, 230, 100 + row * 90]] })),
    ...extra,
  })
  const inserted = (extra = {}) => ({
    success: true,
    action: 'insert',
    pages: [{ page_index: 0, questions: [solve('1'), { question_no: '1.1', question_type: 'solve' }, solve('2')] }],
    ...extra,
  })
  const scenarios = [
    { name: 'the artifact is marked for review', response: inserted({ page_artifacts: { p1: artifact({ geometry_review_pending: true }) } }), pending: true },
    { name: 'the page is listed for review', response: inserted({ page_artifacts: { p1: artifact() }, geometry_review_pending_pages: [0] }), pending: true },
    { name: 'nothing is left to review', response: inserted({ page_artifacts: { p1: artifact() } }), pending: false },
  ]
  for (const { name, response, pending } of scenarios) {
    const editor = await openTemplateEditor(t, {
      pages: [[solve('1'), solve('2')]],
      responses: {
        [ROUTES.questions]: response,
        // A failed save, then a successful one; the confirmation after it is refused.
        [ROUTES.layout]: (() => {
          let calls = 0
          return config => (++calls === 1 ? { status: 500, data: { detail: '保存失败' } } : acceptLayout(config))
        })(),
        [ROUTES.confirm]: { success: false, detail: '模板确认暂不可用' },
      },
      messageBox: entry => (entry.kind === 'prompt' ? { value: '1.1' } : 'confirm'),
    })
    const { upload } = editor
    const placeholder = () => editor.preview(0).byClass('tpl-insert-placeholder')
    const manage = editor.preview(0).find(el => el.tagName === 'select' && el.attributes['aria-label'] === '管理题目')
    await upload.select(manage, '2')
    await editor.press(editor.preview(0).byText('button', '在前面插入题目'))
    assert.deepEqual(editor.questionEdits(), [{ page_index: 0, action: 'insert', before_question_number: '2', new_question_number: '1.1' }], name)
    assert.ok(editor.question('1.1'), name)
    if (!pending) {
      assert.ok(!placeholder(), name)
      assert.doesNotMatch(editor.preview(0).text, /已调整 · 随模板保存/, name)
      assert.ok(!editor.reviewAlert(), name)
      editor.upload.unmount()
      continue
    }
    assert.equal(placeholder().text, '新增题号为占位，请拖至正确位置；最后保存模板即可。', name)
    assert.match(editor.preview(0).text, /已调整 · 随模板保存/, name)
    assert.match(editor.reviewAlert().text, /栏边界 第1页/, name)
    assert.match(editor.reviewAlert().text, /低置信题目归属/, name)
    assert.equal(editor.bankEnabled(), false, name)

    // The teacher writes the new question, then saves.
    await upload.type(editor.field('1.1', '题干').find(el => el.tagName === 'textarea'), '新增题干')
    await upload.type(editor.field('1.1', '标准答案').find(el => el.tagName === 'textarea'), 'y=3')
    await editor.save()
    assert.equal(editor.layoutSaves().length, 1, name)
    assert.ok(placeholder(), `${name}: a failed save keeps the placeholder`)
    await editor.save()
    assert.equal(editor.layoutSaves().length, 2, name)
    assert.ok(!placeholder(), `${name}: a successful save places it`)
    assert.doesNotMatch(editor.preview(0).text, /已调整 · 随模板保存/, name)
    assert.ok(!editor.reviewAlert(), name)
    assert.equal(editor.confirms().length, 1, name)
    editor.upload.unmount()
  }
})

test('layout save retains marked writing areas but omits an absent legacy contract', async (t) => {
  // Each page saves once; its question has the number 2.<page>.
  const question = pageIndex => solve(`2.${pageIndex + 2}`)
  const page = (pageIndex, extra) => {
    const located = locatedPage(pageIndex, [question(pageIndex)], { layout_review_pending: true })
    delete located.template_answer_regions
    return { ...located, ...extra }
  }
  const region = (pageIndex, regions) => [{ question_number: `2.${pageIndex + 2}`, regions }]
  const box = [300, 150, 600, 300]
  const sentRegions = editor => Object.fromEntries(editor.layoutSaves().map(save => [save.page_index, save.template_answer_regions]))
  const confirmed = (pageIndex, regions) => region(pageIndex, regions).map(item => (regions.length
    ? { ...item, human_confirmed: true, confidence_source: 'teacher', confidence: 1 } : item))

  // A blank template always sends its writing areas, even none.
  const blank = await openTemplateEditor(t, { templateMode: 'blank', pages: [[question(0)]], cut: [page(0, { template_answer_regions: [] })] })
  assert.equal(blank.counts(0), '1 个题号 · 0 个作答框 · 0 个作图框')
  await blank.save()
  assert.deepEqual(sentRegions(blank), { 0: confirmed(0, []) })
  blank.upload.unmount()

  const mixed = solve('2.5', { answer_kind: 'mixed' })
  const pages = [
    // No writing areas were found: nothing to keep.
    page(0, { template_answer_regions: [] }),
    // A legacy artifact without the field at all.
    page(1),
    // A located writing area.
    page(2, { template_answer_regions: region(2, [box]) }),
    // A located writing area the teacher removes below (the question keeps its drawing area).
    { ...locatedPage(3, [mixed]), template_answer_regions: [{ question_number: '2.5', regions: [box] }] },
    // Legacy pages keep their writing areas as handwriting regions only.
    page(4, { template_handwriting_regions: region(4, [box]) }),
    // Under review, the model's candidate writing area is the draft.
    page(5, { template_answer_regions: [], template_answer_region_candidates: region(5, [box]) }),
    // A settled page ignores a stale candidate.
    { ...locatedPage(6, [question(6)]), template_answer_regions: [], template_answer_region_candidates: region(6, [box]) },
  ]
  const editor = await openTemplateEditor(t, {
    pages: [[question(0)], [question(1)], [question(2)], [mixed], [question(4)], [question(5)], [question(6)]],
    cut: pages,
  })
  assert.equal(editor.counts(5), '1 个题号 · 1 个作答框 · 0 个作图框')
  assert.equal(editor.counts(6), '1 个题号 · 0 个作答框 · 0 个作图框')
  // Removing the last writing area must be explicit, unlike a legacy absent contract.
  await editor.press(editor.legend(3).byText('button', 'Q2.5 作答框 1'))
  await editor.press(editor.preview(3).byText('button', '删除所选'))
  assert.equal(editor.counts(3), '1 个题号 · 0 个作答框 · 1 个作图框')

  await editor.save()
  const sent = sentRegions(editor)
  assert.deepEqual(Object.keys(sent), ['0', '1', '2', '3', '4', '5'], 'the settled page needs no save')
  assert.equal(sent[0], undefined)
  assert.equal(sent[1], undefined)
  assert.deepEqual(sent[2], confirmed(2, [box]))
  assert.deepEqual(sent[3], [{ question_number: '2.5', regions: [] }])
  assert.deepEqual(sent[4], confirmed(4, [box]))
  assert.deepEqual(sent[5], confirmed(5, [box]))
  for (const pageIndex of [0, 1]) {
    assert.equal(Object.hasOwn(editor.layoutSaves()[pageIndex], 'template_answer_regions'), false)
  }
  // The saved page result drops the model's candidate draft.
  const result = editor.page.setup.templateCutResult.pages[5]
  assert.equal(Object.hasOwn(result, 'template_answer_region_candidates'), false)
  assert.deepEqual(result.template_answer_regions, confirmed(5, [box]))
})
