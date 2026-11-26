import assert from 'node:assert/strict'
import test from 'node:test'
import { decodeEntitiesThroughTextareas } from './helpers/decodeEntities.js'
import { mountUpload, TEACHER } from './helpers/uploadPage.js'
import { renderAnswerLatex } from '../src/utils/latex.js'

// The real Upload page counts answer boxes: a synthetic fill question with
// eight answer boxes, seven judged correct and one wrong, reads 7 correct ·
// 1 wrong · 87.5% in the 判定构成 column, the accuracy column, the summary,
// the hero and the batch report ring — never one "partial" question. The
// manual review dialog shows and decides the same eight boxes, so a teacher's
// box verdicts are the ones every statistic counts. Exam rows keep their scores.

const BATCH = 'b-boxes'
const REVIEW_POINTER = `mathagent_review_batch_id:${TEACHER.id}`
const REVIEW = `GET /api/batch-grade/review/${BATCH}`
const DECISION = 'POST /api/batch-grade/review/decision'
const PREVIEW = 'POST /api/batch-grade/review/image-preview'
const SEVEN_RIGHT_ONE_WRONG = [...Array(7).fill('correct'), 'wrong']
const STUDENT = ['11', '12', '13', '14', '15', '16', '17', '99']
const REFERENCE = ['11', '12', '13', '14', '15', '16', '17', '18']
const BOX_IDS = STUDENT.map((_, index) => `1[${index + 1}]`)

// One reviewed paper with question 1 judged box by box. The server counts the
// boxes; a review saved before it did still holds the whole-question fold.
function paperRow(boxes, { category = 'daily', counts = 'boxes', ...extra } = {}) {
  const whole = boxes.every(v => v === 'correct') ? 'correct' : boxes.every(v => v === 'wrong') ? 'wrong' : 'partial'
  const boxCount = verdict => boxes.filter(v => v === verdict).length
  const saved = counts === 'boxes'
    ? { correct: boxCount('correct'), partial: boxCount('partial'), wrong: boxCount('wrong'), total: boxes.length }
    : { correct: Number(whole === 'correct'), partial: Number(whole === 'partial'), wrong: Number(whole === 'wrong'), total: 1 }
  return {
    paper_id: 'p1', review_id: 'p1', student_name: '甲', filename: '甲.png', disposition: 'active', category,
    ...saved, needs_review: 0, correct_rate: saved.correct / saved.total,
    details: [{
      question_no: '1', question_type: 'fill', result: whole, student_answer: STUDENT.join('；'),
      correct_answer: REFERENCE.join('；'),
      grading_units: boxes.map((result, index) => ({ question_no: BOX_IDS[index], result, blank_index: index + 1 })),
      blank_results: SEVEN_RIGHT_ONE_WRONG.map((result, index) => ({
        blank_index: index + 1, result, student_answer: STUDENT[index], standard_answer: REFERENCE[index],
      })),
    }],
    ...extra,
  }
}

function reviewPayload(row, summary = {}) {
  return { batch_id: BATCH, status: 'review_ready', results: [row], summary: { total: 1, ...summary }, review_revision: 3 }
}

async function restoredReview(t, payload, responses = {}) {
  return mountUpload(t, {
    localStorage: { [REVIEW_POINTER]: BATCH },
    responses: {
      [REVIEW]: payload,
      [`GET /api/batch-grade/report/${BATCH}`]: { status: 'ready', text: '', stats: {} },
      [`GET /api/batch-grade/${BATCH}/paper/p1/image`]: new Blob(['review image'], { type: 'image/png' }),
      [PREVIEW]: { status: 200, data: new Blob(['preview'], { type: 'image/png' }) },
      ...responses,
    },
  })
}

const summaryText = upload => upload.page.root.byClass('summary-bar').text.replace(/\s+/g, ' ')
const rowMetric = upload => upload.page.root.byClass('review-row__metric').text.replace(/\s+/g, '')
const rowOutcome = upload => upload.page.root.byClass('review-row__outcome').text.replace(/\s+/g, ' ').trim()
const heroAccuracy = upload => upload.page.root.allByClass('hero-metric')
  .find(el => el.text.includes('作答框正确率'))?.byClass('hero-metric__value').text.replace(/\s+/g, '')
function reportRing(upload) {
  const ring = upload.page.root.byClass('batch-report-ring-card')
  return {
    center: ['strong', 'span'].map(tag => ring.byClass('outcome-donut__center').byTag(tag)[0].text),
    legend: ring.byClass('outcome-donut__legend').byTag('li').map(li => [li.byTag('span').at(-1).text, li.byTag('strong')[0].text]),
    spoken: ring.find(el => el.tagName === 'svg').getAttribute('aria-label'),
  }
}

// The manual review dialog's compact sheet: one column per answer box.
function reviewSheet(upload) {
  const sheet = upload.page.root.byClass('manual-review-sheet-compact')
  const answers = sheet.allByClass('manual-review-sheet-answer').map(cell => cell.byTag('span')[0].innerHTML)
  return {
    numbers: sheet.allByClass('manual-review-sheet-number').map(cell => cell.text),
    student: answers.slice(0, answers.length / 2),
    reference: answers.slice(answers.length / 2),
    verdicts: sheet.allByClass('manual-review-sheet-verdict')
      .map(cell => ['correct', 'partial', 'wrong'].find(verdict => cell.classList.contains(`is-verdict-${verdict}`)) ?? ''),
  }
}

async function chooseVerdict(upload, label, verdict) {
  const select = upload.page.root.find(el => el.tagName === 'select' && el.attributes['aria-label'] === `第${label}题最终判定`)
  assert.ok(select, `answer box ${label} has a verdict select`)
  for (const option of select.options) option.selected = String(option._value ?? option.value) === verdict
  const event = { type: 'change', target: select, currentTarget: select, stopPropagation() {}, preventDefault() {} }
  for (const listener of select.listeners.get('change') ?? []) listener(event)
  await select.handlers.change?.(event)
  await upload.settle()
}

function assertSevenOfEight(upload, when) {
  assert.match(summaryText(upload), /作答框正确率 87\.5%（正确 7 · 错误 1）/, `summary ${when}`)
  assert.equal(rowMetric(upload), '87.5%', `row ${when}`)
  assert.equal(heroAccuracy(upload), '87.5%', `hero ${when}`)
  assert.equal(rowOutcome(upload), '正确 7 / 8 · 错 1', `判定构成 ${when}`)
  assert.deepEqual(reportRing(upload), {
    center: ['88%', '作答框正确率'],
    legend: [['正确', '7'], ['部分正确', '0'], ['错误', '1'], ['待复核', '0']],
    spoken: '判定结果分布：正确 7 个作答框，部分正确 0 个作答框，错误 1 个作答框',
  }, `report ${when}`)
}

test('the teacher decides each answer box and every statistic follows', async (t) => {
  decodeEntitiesThroughTextareas(t)
  assert.equal(renderAnswerLatex('99'), '99', 'answers render as text, so the sheet comparison below is not vacuous')
  // The server answers each decision as the backend does: every box takes
  // the verdict the teacher gave it.
  let revision = 3
  let latest = paperRow(SEVEN_RIGHT_ONE_WRONG)
  const upload = await restoredReview(t, reviewPayload(latest), {
    [DECISION]: (config) => {
      const row = paperRow(JSON.parse(config.data).decisions.map(item => item.result))
      latest = row
      revision += 1
      return {
        status: 200,
        data: {
          ok: true, review_revision: revision,
          review: { paper_id: 'p1', disposition: 'active', category: 'daily', result: {
            correct_count: row.correct, partial_count: row.partial, wrong_count: row.wrong, needs_review_count: 0,
            total: row.total, correct_rate: row.correct_rate, score: null, max_score: null, details: row.details,
          } },
        },
      }
    },
  })
  assertSevenOfEight(upload, 'when the review is restored')

  await upload.click('原卷与批阅结果')
  assert.ok(upload.text().includes('8 个作答框 · 答题卡式核对'))
  assert.deepEqual(reviewSheet(upload), {
    numbers: ['1.1', '1.2', '1.3', '1.4', '1.5', '1.6', '1.7', '1.8'],
    student: STUDENT.map(answer => renderAnswerLatex(answer)),
    reference: REFERENCE.map(answer => renderAnswerLatex(answer)),
    verdicts: SEVEN_RIGHT_ONE_WRONG,
  }, 'one column per answer box, never one partial question')
  await chooseVerdict(upload, '1.8', 'correct')
  assert.deepEqual(JSON.parse(upload.requests(PREVIEW).at(-1).data).decisions, [{ question_no: '1[8]', result: 'correct' }])
  await upload.click('保存该学生判定')
  assert.match(summaryText(upload), /作答框正确率 100%（正确 8 · 错误 0）/)
  assert.equal(rowOutcome(upload), '正确 8 / 8')

  await upload.click('原卷与批阅结果')
  await chooseVerdict(upload, '1.8', 'wrong')
  await upload.click('保存该学生判定')
  assert.deepEqual(upload.requests(DECISION).map(config => JSON.parse(config.data).decisions), [
    BOX_IDS.map(question_no => ({ question_no, result: 'correct', note: '' })),
    BOX_IDS.map((question_no, index) => ({ question_no, result: SEVEN_RIGHT_ONE_WRONG[index], note: '' })),
  ])
  assertSevenOfEight(upload, 'after the teacher edits the boxes')

  // A refresh reads the saved review back from the server.
  const storage = upload.storage()
  upload.unmount()
  const reopened = await mountUpload(t, {
    localStorage: storage,
    responses: {
      [REVIEW]: { ...reviewPayload(latest), review_revision: revision },
      [`GET /api/batch-grade/report/${BATCH}`]: { status: 'ready', text: '', stats: {} },
    },
  })
  assertSevenOfEight(reopened, 'after reopening the result')
})

test('box columns carry the numbers the review image prints', async (t) => {
  // A template whose second slot was deleted keeps slots 1 and 3: the boxes read 1.1 and 1.3.
  const row = paperRow(['correct', 'wrong'])
  row.details[0].grading_units = [
    { question_no: '1[1]', result: 'correct', blank_index: 1 },
    { question_no: '1[3]', result: 'wrong', blank_index: 2 },
  ]
  const upload = await restoredReview(t, reviewPayload(row))
  await upload.click('原卷与批阅结果')
  assert.deepEqual(reviewSheet(upload).numbers, ['1.1', '1.3'])
})

test('a review saved before the server counted boxes still reads box by box', async (t) => {
  const upload = await restoredReview(t, reviewPayload(paperRow(SEVEN_RIGHT_ONE_WRONG, { counts: 'whole question' })))
  assert.equal(rowOutcome(upload), '正确 7 / 8 · 错 1')
  assert.equal(rowMetric(upload), '87.5%')
  assert.match(summaryText(upload), /作答框正确率 87\.5%（正确 7 · 错误 1）/)
})

test('exam rows keep their scores while their counts are box by box', async (t) => {
  const exam = paperRow(SEVEN_RIGHT_ONE_WRONG, { category: 'exam', exam_name: '合成考试', score: 7, max_score: 8 })
  const upload = await restoredReview(t, reviewPayload(exam, { avg_score: 7 }))
  assert.equal(rowMetric(upload), '7/8分')
  assert.equal(rowOutcome(upload), '正确 7 / 8 · 错 1')
  assert.match(summaryText(upload), /已判平均分 7\.0/)
  assert.ok(!upload.text().includes('作答框正确率'), 'no answer-box accuracy on an exam batch')
})
