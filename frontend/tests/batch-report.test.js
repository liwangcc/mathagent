import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { textOf } from './helpers/availabilityQuota.js'
import { mountUpload, TEACHER } from './helpers/uploadPage.js'

// The 本批批阅报告 panel of the real Upload page, for a graded batch restored
// from the teacher's saved review pointer: GET /batch-grade/review/<id> gives
// the per-paper results, GET /batch-grade/report/<id> the batch report.

const uploadUrl = new URL('../src/views/Upload.vue', import.meta.url)

const BATCH = 'b-report'
const REVIEW = `GET /api/batch-grade/review/${BATCH}`
const REPORT = `GET /api/batch-grade/report/${BATCH}`

const ROWS = [
  { paper_id: 'p1', student_name: '甲', disposition: 'active', correct: 3, partial: 1, wrong: 1, needs_review: 0, total: 5, details: [] },
  { paper_id: 'p2', student_name: '乙', disposition: 'active', correct: 4, partial: 0, wrong: 1, needs_review: 0, total: 5, details: [] },
]
const errorItem = (no, count) => ({ question_no: String(no), error_count: count, error_rate: count / 2, judged_count: 2, attempt_count: 2, question: `第${no}题题干` })
const STATS = {
  paper_count: 2,
  question_count: 10,
  correct_count: 6,
  partial_count: 1,
  wrong_count: 2,
  needs_review_count: 1,
  error_rate: 0.3333,
  top_errors: [errorItem(3, 2), errorItem(5, 2), errorItem(1, 1), errorItem(2, 1), errorItem(4, 1)],
  all_errors: [
    errorItem(3, 2), errorItem(5, 2), errorItem(1, 1), errorItem(2, 1), errorItem(4, 1),
    // Answered twice, judged once (the other answer awaits review).
    { ...errorItem(6, 1), error_rate: 1, judged_count: 1, attempt_count: 2 },
    // An older report without judged counts: every answer counts as judged.
    { question_no: '7', error_count: 1, error_rate: 1 / 3, attempt_count: 3, question: '第7题题干' },
  ],
  knowledge_points: [
    { chapter: '一元一次方程', topic: '移项', error_count: 3, attempt_count: 4, error_rate: 0.75 },
    { chapter: '整式', topic: '合并同类项', error_count: 1, attempt_count: 4, error_rate: 0.25 },
  ],
  // An error-cause breakdown the server may still send; the panel has none.
  error_type_distribution: { 计算错误: 2, 审题错误: 1 },
}

async function restoredReport(t, { anonymous = false, report = { status: 'ready', text: '先讲第3题的移项。', stats: STATS } } = {}) {
  const upload = await mountUpload(t, {
    props: anonymous ? { anonymous: true } : {},
    localStorage: { [`mathagent_review_batch_id${anonymous ? ':anonymous' : ''}:${TEACHER.id}`]: BATCH },
    responses: {
      [REVIEW]: {
        batch_id: BATCH,
        status: 'review_ready',
        identity_mode: anonymous ? 'anonymous' : 'identified',
        results: ROWS,
        summary: { total: ROWS.length },
      },
      [REPORT]: report,
    },
  })
  const panel = upload.page.root.byClass('batch-report-panel')
  assert.ok(panel, 'the restored review shows its batch report')
  return { upload, panel }
}

const headings = panel => panel.byTag('h3').map(el => el.text)
// The outcome ring's legend: [label, count] per entry.
const ringLegend = panel => panel.byClass('batch-report-ring-card').byClass('outcome-donut__legend').byTag('li')
  .map(li => [li.byTag('span').at(-1).text, li.byTag('strong')[0].text])

test('batch report keeps a compact top-five view and an expandable full distribution', async (t) => {
  const { upload, panel } = await restoredReport(t)
  assert.equal(upload.requests(REPORT).length, 1)
  assert.deepEqual(headings(panel), ['高频错题（前五道）', '知识点分布', '教学建议', '推荐课件'])
  assert.equal(panel.byClass('batch-report-subtitle').text, '确认后可写入学生档案与班级统计')

  // The ring draws the judged outcomes; pending answers are listed beside it.
  assert.deepEqual(ringLegend(panel), [['正确', '6'], ['部分正确', '1'], ['错误', '2'], ['待复核', '1']])

  const top = panel.byClass('batch-report-top-errors').byTag('li')
  assert.deepEqual(top.map(li => li.byTag('strong')[0].text), ['第3题', '第5题', '第1题', '第2题', '第4题'])

  // One knowledge distribution, no error-cause distribution beside it.
  const distributions = panel.byClass('batch-report-distributions')
  assert.equal(distributions.children.length, 1)
  assert.deepEqual(distributions.allByClass('batch-report-bar-label').map(el => el.text), ['移项3 次错', '合并同类项1 次错'])
  assert.ok(!panel.text.includes('辅助错因分布'))
  assert.ok(!panel.text.includes('计算错误'))

  const details = panel.byClass('batch-report-details')
  assert.equal(details.byTag('summary')[0].text, '查看全部错题分布（7 道）')
  assert.deepEqual(details.byTag('th').map(el => el.text), ['题号', '错题次数', '已判 / 作答', '错误率', '题干摘要'])
  const rows = details.byTag('tbody')[0].byTag('tr').map(tr => tr.byTag('td').map(el => el.text))
  assert.equal(rows.length, 7)
  assert.deepEqual(rows[0], ['第3题', '2', '2 / 2', '100.0%', '第3题题干'])
  // 已判 / 作答 shows the judged count, falling back to the answers when the report has none.
  assert.deepEqual(rows[5], ['第6题', '1', '1 / 2', '100.0%', '第6题题干'])
  assert.deepEqual(rows[6], ['第7题', '1', '3 / 3', '33.3%', '第7题题干'])

  assert.equal(panel.byClass('batch-report-teaching-card').byClass('batch-report-text').text, '先讲第3题的移项。')
  upload.unmount()

  const anonymous = await restoredReport(t, { anonymous: true })
  assert.equal(anonymous.panel.byClass('batch-report-subtitle').text, '无需绑定学生，按页面查看整批学情')
})

test('the full error distribution appears only when the report lists every error', async (t) => {
  for (const allErrors of [[], undefined]) {
    const { upload, panel } = await restoredReport(t, { report: { status: 'ready', text: '', stats: { ...STATS, all_errors: allErrors } } })
    assert.equal(textOf(panel.byClass('batch-report-details')), null, String(allErrors))
    assert.ok(!panel.text.includes('查看全部错题分布'))
    // The compact top-five view stays.
    assert.equal(panel.byClass('batch-report-top-errors').byTag('li').length, 5)
    upload.unmount()
  }
})

test('without server statistics the report counts the graded rows and says knowledge labels are missing', async (t) => {
  const { panel } = await restoredReport(t, { report: { status: 'ready', text: '', stats: {} } })
  // Two papers of five questions: 7 correct, 1 partial, 2 wrong, none pending.
  assert.deepEqual(ringLegend(panel), [['正确', '7'], ['部分正确', '1'], ['错误', '2'], ['待复核', '0']])
  assert.ok(panel.byClass('batch-report-stats').text.includes('共 10 道题'))
  assert.equal(panel.byClass('batch-report-distribution-card').byClass('batch-report-empty').text, '当前没有知识点标签；上方题目判定统计仍然有效')
  assert.equal(panel.byClass('batch-report-top-errors-card').byClass('batch-report-empty').text, '暂未形成可用的错题分布')
  assert.equal(textOf(panel.byClass('batch-report-details')), null)
  assert.equal(textOf(panel.byClass('batch-report-empty-state')), null)
})

test('knowledge distribution fills the card after error-type distribution removal', async () => {
  const source = await readFile(uploadUrl, 'utf8')
  assert.match(source, /\.batch-report-distributions\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/)
  assert.doesNotMatch(source, /\.batch-report-distributions\s*\{[^}]*repeat\(2,/)
  assert.match(source, /\.batch-report-bar-label span\s*\{[^}]*overflow-wrap:\s*anywhere/)
  assert.doesNotMatch(source, /\.batch-report-bar-label span\s*\{[^}]*(?:text-overflow:\s*ellipsis|white-space:\s*nowrap)/)
  assert.match(source, /\.batch-report-bar-label strong\s*\{[^}]*flex:\s*0 0 auto[^}]*white-space:\s*nowrap/)
})
