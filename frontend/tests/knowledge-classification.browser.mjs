// Real Vue frontend; every API call is intercepted with synthetic records only.
// Start Vite, then: NODE_PATH=<playwright install> node tests/knowledge-classification.browser.mjs http://127.0.0.1:5193
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5193'
assert.equal(new URL(base).hostname, '127.0.0.1')
const output = resolve(process.argv[3] || '../outputs/knowledge-classification-browser')
mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const errors = [], calls = [], checks = []

const catalog = [
  { chapter: '函数的概念与性质', topics: ['函数的定义域', '函数的对称性'] },
  { chapter: '立体几何初步', topics: ['三视图', '球的切接问题'] },
]
const senior = { education_stage: 'senior' }
const pending = [
  { question: '示例沿用批改分类题', question_preview: '示例沿用批改分类题', student_answer: '1', ai_answer: '2', answer: '2',
    grading_id: 11, detail_index: 0, teaching_context: senior, chapter: '函数的概念与性质', topic: '函数的定义域',
    classification_status: 'classified', classification_source: 'grading' },
  { question: '示例分类失败题', question_preview: '示例分类失败题', student_answer: '3', ai_answer: '4', answer: '4',
    grading_id: 12, detail_index: 1, teaching_context: senior, chapter: '', topic: '', classification_status: 'failed' },
  { question: '示例分类中题', question_preview: '示例分类中题', student_answer: '5', ai_answer: '6', answer: '6',
    grading_id: 13, detail_index: 2, teaching_context: senior, chapter: '', topic: '', classification_status: 'pending' },
]
const bankRows = [
  { id: 31, teacher_id: 910002, is_public: false, question: '示例已分类题库题', answer: '1', chapter: '函数的概念与性质', topic: '函数的对称性', classification_status: 'suggested', source: 'teacher_confirmed' },
  { id: 32, teacher_id: 910002, is_public: false, question: '示例分类失败题库题', answer: '2', chapter: '', topic: '', classification_status: 'manual', classification_error: '模型多次未能从知识点目录中选出结果，请手动选择知识点', source: 'teacher_confirmed' },
  { id: 33, teacher_id: null, is_public: true, question: '示例公库分类中题', answer: '3', chapter: '', topic: '', classification_status: 'pending', source: 'admin_public' },
]

function respond(req, url) {
  const p = url.pathname
  if (p === '/api/auth/me') return { user: { id: 910002, name: '示例老师', role: 'teacher', education_stage: 'senior', profile_complete: true, current_class_id: 1 } }
  if (p === '/api/auth/classes') return { classes: [{ id: 1, class_name: '示例班', student_count: 0 }], current_class_id: 1 }
  if (p === '/api/bank/pending-answers') return { items: pending, total: pending.length, count: pending.length, page: 1, page_size: 50 }
  if (p === '/api/bank/confirm-answers') return { ok: true, imported: 1, skipped: 0 }
  if (p === '/api/teacher/chapters') return catalog
  if (p === '/api/teacher/bank' && req.method() === 'GET') return { items: bankRows, total: bankRows.length, page: 1, page_size: 20 }
  if (p.startsWith('/api/teacher/bank/') && req.method() === 'PUT') return { ok: true }
  if (p.includes('/availability')) return { status: 'available', hardware: { status: 'ready' } }
  if (p === '/api/recent-files') return { items: [] }
  return {}
}

async function pick(page, row, chapter, topic) {
  await row.locator('.el-cascader').click()
  const dropdown = page.locator('.el-cascader__dropdown:visible')
  await dropdown.locator('.el-cascader-node', { hasText: chapter }).click()
  await dropdown.locator('.el-cascader-node', { hasText: topic }).click()
}

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await context.addInitScript(() => {
    if (window !== window.top) return
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('mathagent_usage_guide_v3:910002', 'true')
    localStorage.setItem('disclaimer_agreed', 'true')
  })
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    let body = null
    try { body = req.postDataJSON() } catch { body = null }
    calls.push({ path: url.pathname, method: req.method(), query: url.search, body })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(respond(req, url)) })
  })
  const page = await context.newPage()
  page.setDefaultTimeout(12000)
  page.on('pageerror', error => errors.push(error.message))

  // 待入库: reused label, failed and in-progress states, manual pick sent on confirm.
  await page.goto(`${base}/pending-answers`)
  await page.locator('#mathagent-startup').waitFor({ state: 'detached' })
  const rows = page.locator('.pending-answers-page .el-table__body-wrapper tbody tr')
  await rows.first().waitFor()
  const reusedRow = rows.filter({ hasText: '示例沿用批改分类题' })
  const failedRow = rows.filter({ hasText: '示例分类失败题' })
  const pendingRow = rows.filter({ hasText: '示例分类中题' })
  assert.match(await reusedRow.innerText(), /沿用批改分类/)
  assert.match(await failedRow.innerText(), /分类失败/)
  assert.match(await pendingRow.innerText(), /分类中/)
  assert.doesNotMatch(await page.locator('.pending-answers-page').innerText(), /未分类/)
  assert.equal(await reusedRow.locator('.el-cascader input').inputValue(), '函数的概念与性质 / 函数的定义域')
  checks.push('pending: reused/failed/in-progress tags, reused label prefilled')
  await page.screenshot({ path: resolve(output, 'pending-before.png'), fullPage: true })

  await pick(page, failedRow, '立体几何初步', '球的切接问题')
  await failedRow.getByText('手动选择').waitFor()
  await failedRow.getByRole('button', { name: '确认入库' }).click()
  await page.getByText(/^已入库/).first().waitFor()
  const manualConfirm = calls.filter(call => call.path === '/api/bank/confirm-answers').at(-1).body.answers[0]
  assert.equal(manualConfirm.question, '示例分类失败题')
  assert.equal(manualConfirm.chapter, '立体几何初步')
  assert.equal(manualConfirm.topic, '球的切接问题')
  assert.equal(manualConfirm.answer, '4')
  checks.push('pending: manual pick for failed row is sent on confirm')

  await rows.filter({ hasText: '示例沿用批改分类题' }).getByRole('button', { name: '确认入库' }).click()
  await page.waitForTimeout(300)
  const reusedConfirm = calls.filter(call => call.path === '/api/bank/confirm-answers').at(-1).body.answers[0]
  assert.equal(reusedConfirm.question, '示例沿用批改分类题')
  assert.deepEqual([reusedConfirm.chapter, reusedConfirm.topic], ['函数的概念与性质', '函数的定义域'])
  checks.push('pending: reused grading label is sent on confirm')
  await page.screenshot({ path: resolve(output, 'pending-after.png'), fullPage: true })

  // 题库: failed row shows 分类失败 + owner picker, public pending row has no picker.
  await page.goto(`${base}/bank`)
  await page.locator('#mathagent-startup').waitFor({ state: 'detached' })
  const bankTable = page.locator('.bank-page .el-table__body-wrapper tbody tr')
  await bankTable.first().waitFor()
  const classifiedBank = bankTable.filter({ hasText: '示例已分类题库题' })
  const failedBank = bankTable.filter({ hasText: '示例分类失败题库题' })
  const publicBank = bankTable.filter({ hasText: '示例公库分类中题' })
  assert.match(await classifiedBank.innerText(), /函数的对称性/)
  assert.match(await failedBank.innerText(), /分类失败/)
  assert.match(await publicBank.innerText(), /分类中/)
  assert.equal(await publicBank.locator('.el-cascader').count(), 0)
  assert.doesNotMatch(await page.locator('.bank-page').innerText(), /未分类/)
  checks.push('bank: classified tag, 分类失败 with picker, public row read-only')
  await page.screenshot({ path: resolve(output, 'bank-before.png'), fullPage: true })

  await pick(page, failedBank, '函数的概念与性质', '函数的定义域')
  await page.getByText('已设置知识点').first().waitFor()
  const update = calls.filter(call => call.path === '/api/teacher/bank/32' && call.method === 'PUT').at(-1)
  assert.deepEqual(update.body, { chapter: '函数的概念与性质', topic: '函数的定义域' })
  checks.push('bank: manual pick sends PUT /teacher/bank/32 with chapter/topic')

  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ ok: true, checks, calls: calls.length, page_errors: errors.length, output }, null, 2))
} finally {
  await browser.close()
}
