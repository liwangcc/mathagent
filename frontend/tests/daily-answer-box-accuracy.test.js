import assert from 'node:assert/strict'
import test from 'node:test'
import { mountUpload, TEACHER } from './helpers/uploadPage.js'

// The real Upload page's daily accuracy is answer-box accuracy: a synthetic
// fill question with eight answer boxes, seven judged correct and one wrong,
// reads 7 correct · 1 wrong · 87.5% — while the whole question stays one
// "partial" (正确 0 / 1 · 半对 1) and exam rows keep their scores.

const BATCH = 'b-boxes'
const REVIEW_POINTER = `mathagent_review_batch_id:${TEACHER.id}`
const REVIEW = `GET /api/batch-grade/review/${BATCH}`
const DECISION = 'POST /api/batch-grade/review/decision'
const SEVEN_RIGHT_ONE_WRONG = [...Array(7).fill('correct'), 'wrong']

// One reviewed paper with question 1 judged box by box; the whole-question
// counts are the server's fold of those boxes.
function paperRow(boxes, { category = 'daily', ...extra } = {}) {
  const whole = boxes.every(v => v === 'correct') ? 'correct' : boxes.every(v => v === 'wrong') ? 'wrong' : 'partial'
  return {
    paper_id: 'p1', review_id: 'p1', student_name: '甲', filename: '甲.png', disposition: 'active', category,
    correct: Number(whole === 'correct'), partial: Number(whole === 'partial'), wrong: Number(whole === 'wrong'),
    needs_review: 0, total: 1, correct_rate: whole === 'correct' ? 1 : 0,
    details: [{
      question_no: '1', question_type: 'fill', result: whole, student_answer: '11；12；13；14；15；16；17；99',
      correct_answer: '11；12；13；14；15；16；17；18',
      grading_units: boxes.map((result, index) => ({ question_no: `1[${index + 1}]`, result, blank_index: index + 1 })),
      blank_results: SEVEN_RIGHT_ONE_WRONG.map((result, index) => ({ blank_index: index + 1, result })),
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
      'POST /api/batch-grade/review/image-preview': { status: 200, data: new Blob(['preview'], { type: 'image/png' }) },
      ...responses,
    },
  })
}

const summaryText = upload => upload.page.root.byClass('summary-bar').text.replace(/\s+/g, ' ')
const rowMetric = upload => upload.page.root.byClass('review-row__metric').text.replace(/\s+/g, '')
const rowOutcome = upload => upload.page.root.byClass('review-row__outcome').text.replace(/\s+/g, ' ').trim()
const heroAccuracy = upload => upload.page.root.allByClass('hero-metric')
  .find(el => el.text.includes('答题框正确率'))?.byClass('hero-metric__value').text.replace(/\s+/g, '')

async function chooseVerdict(upload, questionNo, verdict) {
  const select = upload.page.root.find(el => el.tagName === 'select' && el.attributes['aria-label'] === `第${questionNo}题最终判定`)
  for (const option of select.options) option.selected = String(option._value ?? option.value) === verdict
  const event = { type: 'change', target: select, currentTarget: select, stopPropagation() {}, preventDefault() {} }
  for (const listener of select.listeners.get('change') ?? []) listener(event)
  await select.handlers.change?.(event)
  await upload.settle()
}

function assertSevenOfEight(upload, when) {
  assert.match(summaryText(upload), /答题框正确率 87\.5%（正确 7 · 错误 1）/, `summary ${when}`)
  assert.equal(rowMetric(upload), '87.5%', `row ${when}`)
  assert.equal(heroAccuracy(upload), '87.5%', `hero ${when}`)
  assert.match(rowOutcome(upload), /正确 0 \/ 1 · 半对 1/, `whole-question status ${when}`)
}

test('answer-box accuracy survives a teacher edit and a reopen; the whole question stays partial', async (t) => {
  // The server answers each decision as the backend does: wrong reaches every
  // box; partial gives the boxes back the grader's own verdicts.
  const decided = { wrong: Array(8).fill('wrong'), partial: SEVEN_RIGHT_ONE_WRONG }
  let revision = 3
  let latest = paperRow(SEVEN_RIGHT_ONE_WRONG)
  const upload = await restoredReview(t, reviewPayload(latest), {
    [DECISION]: (config) => {
      const body = JSON.parse(config.data)
      const row = paperRow(decided[body.decisions[0].result])
      latest = row
      revision += 1
      return {
        status: 200,
        data: {
          ok: true, review_revision: revision,
          review: { paper_id: 'p1', disposition: 'active', category: 'daily', result: {
            correct_count: row.correct, partial_count: row.partial, wrong_count: row.wrong, needs_review_count: 0,
            total: 1, correct_rate: row.correct_rate, score: null, max_score: null, details: row.details,
          } },
        },
      }
    },
  })
  assertSevenOfEight(upload, 'when the review is restored')
  assert.ok(upload.text().includes('整题正确率'), 'the report names its whole-question rate')

  for (const verdict of ['wrong', 'partial']) {
    await upload.click('原卷与批阅结果')
    await chooseVerdict(upload, '1', verdict)
    await upload.click('保存该学生判定')
    if (verdict === 'wrong') {
      assert.match(summaryText(upload), /答题框正确率 0%（正确 0 · 错误 8）/)
      assert.equal(rowMetric(upload), '0%')
    }
  }
  assert.deepEqual(upload.requests(DECISION).map(config => JSON.parse(config.data).decisions), [
    [{ question_no: '1', result: 'wrong', note: '' }],
    [{ question_no: '1', result: 'partial', note: '' }],
  ])
  assertSevenOfEight(upload, 'after the teacher edits the verdict')

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

test('exam rows keep their scores, not answer-box accuracy', async (t) => {
  const exam = paperRow(SEVEN_RIGHT_ONE_WRONG, { category: 'exam', exam_name: '合成考试', score: 7, max_score: 8 })
  const upload = await restoredReview(t, reviewPayload(exam, { avg_score: 7 }))
  assert.equal(rowMetric(upload), '7/8分')
  assert.match(summaryText(upload), /已判平均分 7\.0/)
  assert.ok(!upload.text().includes('答题框正确率'), 'no answer-box accuracy on an exam batch')
})
