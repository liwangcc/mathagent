import assert from 'node:assert/strict'
import test from 'node:test'
import { liveSse, mountUpload, paperFile, TEACHER } from './helpers/uploadPage.js'

// What the real Upload page says about a paper's OCR state while it is being
// recognized, and what the 原卷与批阅结果 dialog says about a question the
// grader could not read.

const RECOGNIZE = 'POST /api/batch-grade/recognize'
const REVIEW_POINTER = `mathagent_review_batch_id:${TEACHER.id}`

// The recognition table's 匹配/题数 cell for a paper event, while the
// recognition stream is still open.
async function pendingLabel(t, paper) {
  const live = liveSse(t)
  const upload = await mountUpload(t, { streams: { [RECOGNIZE]: live.respond } })
  upload.page.setup.confirmedTemplateId = 'tpl-1'
  await upload.choosePapers([paperFile('a.png')])
  live.send('progress', { batch_id: 'b1', current: 0, total: 1 })
  live.send('paper', { batch_id: 'b1', paper_id: 'p1', filename: 'a.png', ...paper })
  await upload.settle()
  assert.equal(live.open, true)
  const cell = upload.page.root.byClass('result-table').byTag('tr')[1].byTag('td')[3]
  return cell.text
}

// The diagnostic under 学生答案 for one graded question, in the dialog the
// teacher opens from a restored review.
async function diagnostic(t, detail) {
  const upload = await mountUpload(t, {
    localStorage: { [REVIEW_POINTER]: 'b-review' },
    responses: {
      'GET /api/batch-grade/review/b-review': {
        batch_id: 'b-review',
        status: 'review_ready',
        results: [{
          paper_id: 'synthetic-paper',
          student_name: '甲',
          disposition: 'active',
          correct: 0,
          total: 1,
          needs_review: 1,
          details: [{ question_no: '3', student_answer: '', result: 'needs_review', ...detail }],
        }],
        summary: { total: 1 },
      },
      'GET /api/batch-grade/report/b-review': { status: 'ready', text: '', stats: {} },
      'GET /api/batch-grade/b-review/paper/synthetic-paper/image': new Blob(['png'], { type: 'image/png' }),
    },
  })
  await upload.click('原卷与批阅结果')
  const dialog = upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes['data-open'] === 'true')
  assert.ok(dialog, 'the 原卷与批阅结果 dialog is open')
  assert.deepEqual(upload.client.messages.filter(message => message.type === 'error'), [])
  const row = dialog.byClass('manual-review-solve-row')
  assert.match(row.text, /第 3 题/)
  return row.allByClass('manual-review-diagnostic').map(el => el.text).join('')
}

for (const [status, label] of [['queued', '等待识别处理'], ['running', '正在识别处理']]) {
  test(`${status} describes the actual OCR stage rather than assuming accelerator work`, async (t) => {
    assert.equal(await pendingLabel(t, { ocr_status: status }), label)
  })
}

test('an explicit accelerator diagnostic is preserved without inferring it from queued', async (t) => {
  assert.equal(await pendingLabel(t, { ocr_status: 'queued', error: '  等待教师客户端加速节点完成图片处理 ' }), '等待教师客户端加速节点完成图片处理')
})

for (const detail of [
  { error_type: 'ocr_slot_identity_invalid' },
  { error_type: 'ocr_failed', slot_identity_error: 'slot_contract_missing' },
  { error_type: 'ocr_unreadable', slot_identity_error: 'slot_contract_missing' },
]) {
  test(`slot evidence failure takes precedence over unreadable: ${detail.error_type}`, async (t) => {
    const text = await diagnostic(t, { ...detail, answer_kind: 'fill', answer_state: 'unreadable' })
    assert.match(text, /模板.*作答位/)
    assert.doesNotMatch(text, /笔迹未能可靠识别/)
  })
}

test('an OCR processing failure never masquerades as unreadable handwriting', async (t) => {
  const text = await diagnostic(t, { error_type: 'ocr_failed', answer_state: 'unreadable', answer_kind: 'fill' })
  assert.match(text, /识别处理未完成/)
  assert.doesNotMatch(text, /笔迹未能可靠识别/)
})

test('genuine unreadable handwriting still describes the original-image review path', async (t) => {
  assert.match(await diagnostic(t, { error_type: 'ocr_unreadable', answer_state: 'unreadable', answer_kind: 'fill' }), /原卷笔迹未能可靠识别/)
})
