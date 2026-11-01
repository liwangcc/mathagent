// Real Vue frontend; every API call is intercepted with synthetic records only.
// Start Vite, then: NODE_PATH=<playwright install> node tests/derived-features.browser.mjs http://127.0.0.1:5189
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5189'
assert.equal(new URL(base).hostname, '127.0.0.1')
const output = resolve(process.argv[3] || '../outputs/derived-features-browser')
mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const errors = [], calls = [], checks = []

const students = [
  { id: 1, name: '示例甲', student_no: '001', class_id: 1, wrong_rate: 0.5, correction_rate: 0.2, weakness_count: 2, risk_level: 'watch', risk_reason: '存在2个薄弱点', has_records: true, has_mistakes: true, latest_exam_score: 135, latest_exam_name: '期中' },
  { id: 2, name: '示例全对', student_no: '002', class_id: 1, wrong_rate: 0, correction_rate: 0, weakness_count: 0, risk_level: 'stable', risk_reason: '各项指标正常', has_records: true, has_mistakes: false, latest_exam_score: null },
  { id: 3, name: '示例新生', student_no: '003', class_id: 1, wrong_rate: 0, correction_rate: 0, weakness_count: 0, risk_level: 'no_data', risk_reason: '暂无已归档的批阅记录', has_records: false, has_mistakes: false, latest_exam_score: null },
]
const isDryRun = req => (req.postDataBuffer() || Buffer.alloc(0)).toString('utf8').includes('name="dry_run"')

function respond(req, url) {
  const p = url.pathname
  if (p === '/api/auth/me') return { user: { id: 910002, name: '示例老师', role: 'teacher', education_stage: 'senior', profile_complete: true, current_class_id: 1 } }
  if (p === '/api/auth/classes') return { classes: [{ id: 1, class_name: '示例班', student_count: 3 }], current_class_id: 1 }
  if (p === '/api/class/1/students' && req.method() === 'GET') return { class_name: '示例班', students, coverage: { class_size: 3, effective_students: 3, observation_students: 0 } }
  if (p === '/api/class/1/students' && req.method() === 'POST') return { added: req.postDataJSON().students.length, skipped: 0, errors: [] }
  if (p === '/api/class/1/students/import-image') return { recognized: [{ student_no: '010', name: '识别错字', phone: '' }], need_confirm: true }
  if (p === '/api/class/1/students/import-excel') {
    return isDryRun(req)
      ? { dry_run: true, added: 1, skipped: 1, errors: ['第3行学号 010 与第2行重复'], warnings: [], columns: { no: '学号', name: '姓名', phone: '家长电话' },
          preview: [{ row: 2, student_no: '010', name: '示例乙', phone: '13800000000', status: 'add', reason: '' }, { row: 3, student_no: '010', name: '示例丙', phone: '', status: 'skip', reason: '第3行学号 010 与第2行重复' }] }
      : { dry_run: false, added: 1, skipped: 1, errors: ['第3行学号 010 与第2行重复'], warnings: [] }
  }
  if (p === '/api/class/1/exam-scores/import') {
    const result = { matched: 1, unmatched: ['第3行 示例甲：班级有 2 名同名学生，请在表中填写学号'], skipped: ['第4行 示例全对：成绩“缺考”无法识别，已跳过'], duplicates: [], exam_name: '期中', exam_names: ['期中'], columns: { name: '姓名', score: '成绩' } }
    return isDryRun(req) ? { ...result, dry_run: true, preview: [{ row: 2, student_no: '002', name: '示例全对', score: 120, exam_name: '期中', status: 'match', reason: '' }] } : { ...result, dry_run: false }
  }
  if (p === '/api/student/list') return { items: students, students, total: students.length }
  if (p === '/api/student/1/detail') return {
    student: { ...students[0], phone: '13800000001', class_name: '示例班' },
    weaknesses: [{ chapter: '函数', topic: '定义域', topic_key: 'senior||函数||定义域', education_stage: 'senior', status: 'corrected', manual: false, auto_status: 'corrected', occurrences: 2 }],
    wrong_questions: [], exam_scores: [{ exam_name: '月考', score: 120, date: '2026-09-01', education_stage: 'senior' }, { exam_name: '期中', score: 135, date: '2026-10-01', education_stage: 'senior' }],
    wrong_rate: 0.5, correction_rate: 1, latest_exam_delta: 15, latest_exam_rank_delta: null, has_records: true, graded_count: 3,
  }
  if (p === '/api/student/2/detail') return { student: { ...students[1], class_name: '示例班' }, weaknesses: [], wrong_questions: [], exam_scores: [], wrong_rate: 0, correction_rate: 0, has_records: true, graded_count: 4 }
  if (p === '/api/student/1/weakness/correct') return { ok: true, status: req.postDataJSON().status, manual: true }
  if (p === '/api/bank/pending-answers') return { items: [{ question: '示例待入库题', question_preview: '示例待入库题', student_answer: '1', ai_answer: '2', grading_id: 9, detail_index: 0, teaching_context: { education_stage: 'senior' }, classification_status: 'failed' }], total: 7, count: 7 }
  if (p === '/api/bank/pending-answers/delete') return { ok: true, removed: 1, dismissed: 1 }
  if (p === '/api/teacher/chapters') return []
  if (p === '/api/grade/results') return { items: [{ id: 501, student_name: '示例甲', category: 'daily', correct_rate: 0.8, correct_count: 4, total_questions: 5, created_at: '2026-10-01T08:00:00' }], total: 120, page: Number(url.searchParams.get('page') || 1), page_size: 50 }
  if (p === '/api/reports/summary') return { summary: '近7天共完成 1 份正式批改', total: 1, total_questions: 5, accuracy: 80, top_students: [], weak_students: [], knowledge_distribution: [] }
  if (p === '/api/class/1/letter-shares') return { shares: [] }
  if (p === '/api/class/1/report/batch') return { ok: true, mode: 'share', shares: [], skipped: [], empty_content: ['示例新生'], message: '已准备 0 个家长专属链接' }
  if (p === '/api/courseware/recommendations') return { matched: true, items: [{ id: 5, stage: 'senior', education_stage: 'senior', title: '函数：函数的单调性', chapter: '函数', knowledge_point: '函数的单调性', knowledgePoint: '函数的单调性', objective: '示例目标', steps: [], checkpoints: [], recommendation_reason: '命中章节：函数（知识点：函数的单调性）' }] }
  if (p === '/api/courseware') return { items: [] }
  if (p.includes('/availability')) return { status: 'available', hardware: { status: 'ready' } }
  if (p === '/api/recent-files') return { items: [] }
  return {}
}

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await context.addInitScript(() => {
    // Courseware covers render in sandboxed iframes without storage access.
    if (window !== window.top) return
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('mathagent_usage_guide_v3:910002', 'true')
    localStorage.setItem('disclaimer_agreed', 'true')
  })
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    calls.push({ path: url.pathname, method: req.method(), query: url.search, dryRun: isDryRun(req) })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(respond(req, url)) })
  })
  const page = await context.newPage()
  page.setDefaultTimeout(12000)
  page.on('pageerror', error => errors.push(error.message))
  const messageBox = () => page.locator('.el-message-box')
  const shot = name => page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true })

  // Class table: no-data risk group, "no mistakes" correction cell, sidebar badge.
  await page.goto(`${base}/class/1/students`)
  const classPage = page.locator('.class-table-page')
  await classPage.locator('.risk-panel').filter({ hasText: '暂无数据' }).waitFor()
  assert.match(await classPage.locator('.risk-panel').filter({ hasText: '暂无数据' }).innerText(), /示例新生/)
  await classPage.getByText('无错题', { exact: true }).first().waitFor()
  await classPage.getByText('暂无批阅', { exact: true }).first().waitFor()
  await page.locator('.nav-count').filter({ hasText: '7' }).waitFor()
  checks.push('class-table risk + badge')

  // Score import: dry-run preview first, then the real import.
  await classPage.getByRole('button', { name: /更多/ }).click()
  await page.getByRole('menuitem', { name: /导入月考成绩/ }).click()
  const examDialog = page.locator('.el-dialog').filter({ hasText: '导入月考成绩' })
  await examDialog.locator('input[type=file]').setInputFiles({ name: 'scores.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('synthetic') })
  await examDialog.getByRole('button', { name: '预览导入' }).click()
  await examDialog.getByText(/同名学生，请在表中填写学号/).waitFor()
  await examDialog.getByText(/成绩“缺考”无法识别/).waitFor()
  await shot('exam-import-preview')
  await examDialog.getByRole('button', { name: '确认导入 1 人' }).click()
  await messageBox().filter({ hasText: '请核对以下行' }).waitFor()
  await messageBox().getByRole('button', { name: '知道了' }).click()
  const examCalls = calls.filter(call => call.path === '/api/class/1/exam-scores/import')
  assert.deepEqual(examCalls.map(call => call.dryRun), [true, false])
  checks.push('exam import preview then commit')

  // Roster Excel: preview shows skipped rows with reasons before writing.
  await classPage.getByRole('button', { name: '添加学生' }).first().click()
  const studentDialog = page.locator('.el-dialog').filter({ hasText: '如果班级中有同名学生' })
  await studentDialog.getByRole('tab', { name: 'Excel 导入' }).click()
  await studentDialog.locator('.el-tab-pane').filter({ hasText: '选择学生名单 Excel' }).locator('input[type=file]')
    .setInputFiles({ name: 'roster.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('synthetic') })
  await studentDialog.getByRole('button', { name: '预览导入' }).click()
  await studentDialog.getByText('第3行学号 010 与第2行重复').first().waitFor()
  await studentDialog.getByText(/家长手机←“家长电话”/).waitFor()
  await shot('roster-import-preview')
  await studentDialog.getByRole('button', { name: '确认导入 1 人' }).click()
  await messageBox().getByRole('button', { name: '知道了' }).click()
  assert.deepEqual(calls.filter(call => call.path === '/api/class/1/students/import-excel').map(call => call.dryRun), [true, false])
  checks.push('roster import preview then commit')

  // Recognized roster rows are editable before saving.
  await classPage.getByRole('button', { name: '添加学生' }).first().click()
  await studentDialog.getByRole('tab', { name: '图片识别' }).click()
  await studentDialog.locator('.el-tab-pane').filter({ hasText: '上传名单图片' }).locator('input[type=file]')
    .setInputFiles({ name: 'roster.png', mimeType: 'image/png', buffer: Buffer.from('synthetic') })
  await studentDialog.getByRole('button', { name: '识别名单' }).click()
  const nameInput = studentDialog.locator('.recognized-box input').nth(2)
  await nameInput.waitFor()
  await nameInput.fill('示例改正')
  const saved = page.waitForRequest(req => new URL(req.url()).pathname === '/api/class/1/students' && req.method() === 'POST')
  await studentDialog.getByRole('button', { name: '保存学生' }).click()
  assert.equal((await saved).postDataJSON().students[0].name, '示例改正')
  checks.push('recognized roster editable')

  // Student detail: 150-point axis, masked phone, reopen auto-corrected topic.
  await page.goto(`${base}/student/1/detail`)
  const detailPage = page.locator('.student-detail-page')
  await detailPage.locator('.tick-label').filter({ hasText: /^150$/ }).waitFor()
  await detailPage.getByText(/家长手机 138\*\*\*\*1234/).waitFor()
  const reopen = page.waitForRequest(req => new URL(req.url()).pathname === '/api/student/1/weakness/correct')
  await detailPage.getByRole('button', { name: '标记未纠正' }).click()
  assert.equal((await reopen).postDataJSON().status, 'pending')
  await shot('student-detail')
  await page.goto(`${base}/student/2/detail`)
  await page.locator('.student-detail-page .status-pill').filter({ hasText: '表现良好' }).waitFor()
  await page.locator('.student-detail-page').getByText('暂无错题，无需纠错').waitFor()
  checks.push('student detail scale + status + reopen')

  // Pending answers: "忽略" explains that grade records stay untouched.
  await page.goto(`${base}/pending-answers`)
  await page.getByText('共 7 条').waitFor()
  await page.getByRole('button', { name: '忽略', exact: true }).first().click()
  await messageBox().filter({ hasText: '学生的批改记录和成绩不会改变' }).waitFor()
  const dismissed = page.waitForRequest(req => new URL(req.url()).pathname === '/api/bank/pending-answers/delete')
  await messageBox().getByRole('button', { name: '忽略' }).click()
  await dismissed
  checks.push('pending dismissal wording')

  // Grade records: real total and pagination.
  await page.goto(`${base}/grade-records`)
  await page.locator('.head-subtitle').filter({ hasText: '共 120 条' }).waitFor()
  await page.locator('.grade-records-page .el-pagination').waitFor()
  assert.ok(calls.some(call => call.path === '/api/grade/results' && call.query.includes('page=1')))
  checks.push('grade records paging')

  // Courseware: a chapter-level recommendation is shown, with a way back to the full list.
  await page.goto(`${base}/courseware?chapter=${encodeURIComponent('函数')}&knowledge_point=${encodeURIComponent('二次函数的图像')}`)
  await page.getByRole('heading', { name: '函数：函数的单调性' }).waitFor()
  await page.getByRole('button', { name: '查看全部课件' }).first().waitFor()
  checks.push('courseware recommendation not re-filtered')

  // Reports: rolling-window labels and empty-letter warning.
  await page.goto(`${base}/reports`)
  await page.getByText('近7天', { exact: true }).first().waitFor()
  await page.getByRole('button', { name: '准备并复制链接', exact: true }).click()
  await messageBox().filter({ hasText: '示例新生' }).waitFor()
  await shot('reports-letter-warning')
  await messageBox().getByRole('button', { name: '知道了' }).click()
  checks.push('reports labels + letter warning')

  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ ok: true, output, apiCalls: calls.length, checks }))
} finally {
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ mockedApiOnly: true, realAccountRequests: 0, errors, checks, calls }, null, 2))
  await browser.close()
}
