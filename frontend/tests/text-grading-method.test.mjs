import assert from 'node:assert/strict'
import test from 'node:test'
import { jsonResponse } from './helpers/memoryFetch.js'
import { mountAnswerSheet } from './helpers/sourcesReview.js'
import { mountUpload, paperFile } from './helpers/uploadPage.js'

// A question's grading method (grading_method) through the two editors that
// handle it: the 批改 page's template editor (Upload.vue) and the 答题卡批阅
// page's answer confirmation (AnswerSheet.vue), plus the 答题卡 review of the
// graded drafts.

const TEMPLATE_RECOGNIZE = 'POST /api/batch-grade/template/recognize'
const CORRECTION_CANDIDATES = 'POST /api/batch-grade/template/correction-candidates'

// The question as Upload's template editor sends it back to the server, after
// the server recognized `question` on the template's only page. Opening the
// editor asks the server for correction candidates with every question in
// the form the editor saves them.
async function throughUploadEditor(t, question) {
  const upload = await mountUpload(t, {
    streams: {
      [TEMPLATE_RECOGNIZE]: [['done', { template_id: 'tpl-1', pages: [{ page_index: 0, questions: [question] }] }]],
      'GET /api/batch-grade/template/tpl-1/cut-candidates': () => jsonResponse({ status: 'not_run' }),
      'POST /api/batch-grade/template/tpl-1/cut-candidates': () => jsonResponse({ status: 'failed', detail: '本测试不裁题' }),
    },
    responses: { [CORRECTION_CANDIDATES]: { candidates: [] } },
  })
  const input = upload.page.root.byClass('template-card').find(el => el.tagName === 'input' && el.type === 'file')
  input.files = [paperFile('template.png')]
  input.dispatch('change')
  await upload.settle()
  await upload.click('识别模板')
  const editor = upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板')
  assert.equal(editor?.attributes['data-open'], 'true', 'the template editor is open')
  const sent = upload.requests(CORRECTION_CANDIDATES)
  assert.equal(sent.length, 1)
  const payload = JSON.parse(sent[0].data)
  upload.unmount()
  return payload.pages[0].questions[0]
}

const SHEET = 's-answers'
const CONFIRM = `POST /api/answer-sheet/sessions/${SHEET}/answers/confirm`

// The 答题卡批阅 page with `questions` recognized from the original paper:
// the grading method each question card shows, and what 确认题目与答案 sends.
async function throughAnswerSheet(t, questions) {
  const session = { session_id: SHEET, status: 'source_recognized', answer_source: 'embedded', source_question_drafts: questions, answer_sheet_pages: [] }
  const sheet = await mountAnswerSheet(t, {
    session,
    responses: {
      [CONFIRM]: config => ({
        status: 200,
        data: { ...session, status: 'answers_confirmed', answer_spec_snapshot: JSON.parse(config.data).questions },
      }),
    },
  })
  const shown = questionCards(sheet).map(card => fieldSelect(card, '评分方式').props.modelValue)
  await sheet.page.root.byText('el-button', '确认题目与答案').dispatch('click')
  await sheet.settle()
  const sent = sheet.body(CONFIRM).questions
  sheet.page.unmount()
  return { shown, sent }
}

const questionCards = sheet => sheet.page.root.byClass('question-list').allByClass('question-card')
const fieldSelect = (card, label) => card
  .find(el => el.tagName === 'el-form-item' && el.attributes.label === label)
  ?.byTag('el-select')[0]

for (const grading_method of [undefined, null, '', 'process', 'equivalent', 'exact']) {
  test(`missing method is not converted to process; explicit ${grading_method} survives both editors`, async (t) => {
    const original = { question_no: '2', directory_node_id: 'n002', question_type: 'solve',
      question_text: 'Complete shared question', standard_answer: '6', blank_count: 1,
      slot_ids: ['2[8]'], answer_slots: ['6'], solution_steps: ['Full reference one', 'Full reference two'],
      ...(grading_method === undefined ? {} : { grading_method }) }
    const snapshot = structuredClone(original)
    const expected = grading_method || ''
    // Two rounds: what the template editor saves comes back from the server
    // the next time the template is opened.
    const rounds = []
    let stored = structuredClone(original)
    for (let round = 0; round < 2; round++) {
      stored = await throughUploadEditor(t, stored)
      assert.equal(stored.grading_method, expected)
      assert.deepEqual(stored.slot_ids, original.slot_ids)
      assert.deepEqual(stored.answer_slots, original.answer_slots)
      assert.deepEqual(stored.solution_steps, original.solution_steps)
      assert.equal(stored.question_text, original.question_text)
      rounds.push(stored)
    }
    // The 答题卡 answer confirmation keeps it too, for each saved round and for
    // the question as first recognized, which it leaves untouched.
    const { shown, sent } = await throughAnswerSheet(t, [...rounds, original])
    assert.deepEqual(shown, [expected, expected, expected])
    assert.deepEqual(sent.map(question => question.grading_method), [expected, expected, expected])
    assert.deepEqual(sent.map(question => question.solution_steps), [original.solution_steps, original.solution_steps, original.solution_steps])
    assert.deepEqual(original, snapshot)
  })
}

for (const [question_type, expected] of [['choice', 'exact'], ['fill', 'equivalent'], ['solve', ''], ['unknown', '']]) {
  test(`${question_type} default matches the backend grading contract`, async (t) => {
    const question = { question_no: '1', question_type, question_text: '题目', standard_answer: 'A' }
    assert.equal((await throughUploadEditor(t, question)).grading_method, expected)
    const { shown, sent } = await throughAnswerSheet(t, [{ question_no: '1', question_type }])
    assert.deepEqual(shown, [expected])
    assert.equal(sent[0].grading_method, expected)
  })
}

// A graded 答题卡 session waiting for the teacher's review.
const REVIEW_SESSION = 'detail-id-test'
const DECISION = `POST /api/answer-sheet/sessions/${REVIEW_SESSION}/grading/decision`
const SESSION = `GET /api/answer-sheet/sessions/${REVIEW_SESSION}`

function reviewSession(details, extra = {}) {
  return {
    session_id: REVIEW_SESSION,
    status: 'review',
    review_revision: 3,
    answer_spec_snapshot: [{ question_no: '2', question_type: 'solve' }],
    answer_sheet_pages: [],
    student_pages: [{ paper_id: 'student_1', filename: 'front.jpg' }, { paper_id: 'student_2', filename: 'back.jpg' }],
    grouping_decisions: [{ student_id: 101, paper_ids: ['student_1', 'student_2'], result: { student_name: '甲', details } }],
    ...extra,
  }
}

const detailCards = sheet => sheet.page.root.allByClass('review-detail-card')
const detailHeading = card => card.byClass('review-detail-top').byTag('strong')[0].text

test('review detail identity prefers historical IDs and never substitutes model slot IDs', async (t) => {
  const sheet = await mountAnswerSheet(t, {
    session: reviewSession([
      { question_no: '2(1)', question_number: 'alias', parent_question_no: '2', slot_id: '2[1]', result: 'correct' },
      { question_number: '2(8)', parent_question_no: '2', slot_id: '2[8]', result: 'correct' },
      { parent_question_no: '7', slot_id: '7[3]', result: 'correct' },
      { slot_id: '9[8]', result: 'correct' },
    ]),
  })
  assert.deepEqual(detailCards(sheet).map(detailHeading), ['第 2(1) 题', '第 2(8) 题', '第 7 题', '第 4 题'])
  assert.doesNotMatch(sheet.page.root.byClass('review-result-scroll').text, /\[\d\]|alias/)
})

test('saveDraftDecision serializes independent historical detail IDs and reloads the saved session', async (t) => {
  const details = [
    { question_no: '2(1)', question_number: 'alias', parent_question_no: '2', slot_id: '2[1]',
      result: 'correct', teacher_note: 'first correction', paper_ids: ['student_1'] },
    { question_number: '2(8)', parent_question_no: '2', slot_id: '2[8]',
      result: 'wrong', teacher_note: 'eighth correction', paper_ids: ['student_2'] },
  ]
  const session = reviewSession(details)
  const sheet = await mountAnswerSheet(t, { session, responses: { [DECISION]: { ok: true } } })
  const save = () => sheet.page.root.byText('el-button', '保存人工判定')
  const stage = () => sheet.page.root.byClass('stage-strip').byTag('button').findIndex(item => item.getAttribute('aria-current') === 'step')
  assert.equal(stage(), 4)
  const draft = sheet.page.setup.gradingDrafts[0]
  const draftBefore = JSON.parse(JSON.stringify(draft))
  // The server's latest version of the session: the second verdict was
  // reopened for review elsewhere.
  sheet.answer(SESSION, reviewSession([details[0], { ...details[1], result: 'needs_review' }], { review_revision: 4 }))

  await save().dispatch('click')
  await sheet.settle()
  assert.deepEqual(sheet.requests(DECISION).map(config => JSON.parse(config.data)), [{
    student_id: 101, review_revision: 3,
    decisions: [
      { question_no: '2(1)', result: 'correct', note: 'first correction' },
      { question_no: '2(8)', result: 'wrong', note: 'eighth correction' },
    ],
  }])
  assert.deepEqual(sheet.client.messages, [{ type: 'success', message: '人工判定已保存' }])
  assert.deepEqual(JSON.parse(JSON.stringify(draft)), draftBefore, 'saving leaves the draft as it was')
  // The saved session is read again and shown.
  assert.equal(sheet.requests(SESSION).length, 2)
  assert.deepEqual(detailCards(sheet).map(card => card.byClass('review-detail-top').byTag('i')[0].text), ['正确', '待复核'])
  assert.equal(stage(), 4)

  // An unresolved verdict still blocks saving.
  await save().dispatch('click')
  await sheet.settle()
  assert.equal(sheet.requests(DECISION).length, 1, 'unresolved verdicts still block saving')
  assert.deepEqual(sheet.client.messages.at(-1), { type: 'warning', message: '请先为全部题目选择正确、部分正确或错误' })

  // Deciding it saves against the reloaded revision. Reloading never moves the
  // teacher off the review, even when the server's latest session reports an
  // earlier stage.
  sheet.answer(SESSION, reviewSession(details, { review_revision: 5, status: 'failed', error: '批阅草稿需要重新生成' }))
  await detailCards(sheet)[1].byTag('el-radio-group')[0].handlers['update:modelValue']('wrong')
  await save().dispatch('click')
  await sheet.settle()
  assert.equal(sheet.requests(DECISION).length, 2)
  assert.equal(JSON.parse(sheet.requests(DECISION)[1].data).review_revision, 4)
  assert.equal(sheet.requests(SESSION).length, 3)
  assert.ok(sheet.page.root.find(el => el.tagName === 'el-alert' && el.attributes.title === '批阅草稿需要重新生成'), 'the reloaded session is shown')
  assert.equal(stage(), 4)
})

for (const [name] of [['AnswerSheet']]) {
  test(`${name} exposes the unspecified scoring option only for solve questions`, async (t) => {
    const types = ['solve', 'choice', 'fill', 'unknown', undefined]
    const sheet = await mountAnswerSheet(t, {
      session: {
        session_id: SHEET,
        status: 'source_recognized',
        answer_source: 'embedded',
        answer_sheet_pages: [],
        source_question_drafts: types.map((question_type, index) => ({ question_no: String(index + 1), ...(question_type ? { question_type } : {}) })),
      },
    })
    const unspecified = card => fieldSelect(card, '评分方式').byTag('el-option').find(option => option.attributes.label === '按题意判分') ?? null
    const typeOf = card => fieldSelect(card, '题型').props.modelValue
    const cards = questionCards(sheet)
    // A question the recognizer typed as unknown, or not at all, is shown and
    // graded as a 解答题.
    assert.deepEqual(cards.map(typeOf), ['solve', 'choice', 'fill', 'solve', 'solve'])
    for (const [index, card] of cards.entries()) {
      const option = unspecified(card)
      assert.equal(Boolean(option), typeOf(card) === 'solve', `${name}: ${types[index]}`)
      if (option) assert.equal(option.props.value, '', 'the unspecified scoring option remains available')
    }
    // Changing the type moves the option with it.
    await fieldSelect(cards[0], '题型').handlers['update:modelValue']('choice')
    await sheet.settle()
    assert.equal(Boolean(unspecified(questionCards(sheet)[0])), false, 'a 选择题 has no unspecified scoring option')
    await fieldSelect(questionCards(sheet)[1], '题型').handlers['update:modelValue']('solve')
    await sheet.settle()
    assert.equal(unspecified(questionCards(sheet)[1])?.props.value, '')
  })
}
