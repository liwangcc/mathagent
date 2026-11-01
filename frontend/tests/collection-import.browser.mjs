// Real Vue/browser File APIs, synthetic data, and exclusively intercepted APIs.
// NODE_PATH=<bundled node_modules> node tests/collection-import.browser.mjs
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import JSZip from 'jszip'

const { chromium } = createRequire(import.meta.url)('playwright')
const output = resolve('../outputs/collection-import-browser-20260909')
mkdirSync(output, { recursive: true })
// No project dotenv or backend proxy is loaded by this isolated test server.
const server = await createServer({
  configFile: false, envFile: false, plugins: [vue()],
  resolve: { alias: { '@': resolve('src') } },
  server: { host: '127.0.0.1', port: 0 },
})
await server.listen()
const base = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const errors = [], submissions = [], results = []
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII=', 'base64')
const candidates = [{ student_id: 1, name: '张三', student_no: '08' }, { student_id: 2, name: '张三', student_no: '09' }]
const papers = [1, 2].map(index => ({
  paper_id: `paper-${index}`, filename: `张三_${index}.jpg`, questions_count: 1, orientation_status: 'auto_passed',
  identity: {
    status: 'needs_confirmation', reason: 'name_ambiguous', candidates,
    confirmation_group: 'synthetic-consistent-directory', source_relative_path: `收集.zip/随机学生目录/张三_${index}.jpg`,
    clue_source: 'filename', name_sources: [{ source: 'filename', names: ['张三'] }],
  },
}))

async function contextFor(recover) {
  const context = await browser.newContext({ viewport: { width: 1366, height: 900 } })
  await context.addInitScript(recover => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
    if (recover) localStorage.setItem('mathagent_orientation_batch_id:910001', 'mock-collection')
  }, recover)
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') {
      submissions.push({ path: url.pathname, body: request.postDataJSON() })
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'event: error\ndata: {"message":"本地模拟：验证到身份提交为止"}\n\n' })
    }
    let data = {}
    if (url.pathname === '/api/auth/me') data = { user: { id: 910001, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true, current_class_id: 1 } }
    else if (url.pathname === '/api/auth/classes') data = { classes: [{ id: 1, name: '模拟班级', class_name: '模拟班级' }], current_class_id: 1 }
    else if (url.pathname.endsWith('/availability')) data = { status: 'available', service_status: 'available', registration_hardware: { status: 'ready', ready: true }, hardware: { status: 'ready' } }
    else if (url.pathname.endsWith('/identity-options')) data = { students: candidates }
    else if (url.pathname === '/api/batch-grade/mock-collection/orientation') data = {
      batch_id: 'mock-collection', status: 'ocr_done', source_mode: 'template', template_id: 'mock-template',
      category: 'daily', template_mode: 'blank', total_papers: 2, uploaded_papers: 2, succeeded_papers: 2,
      can_grade: true, papers, questions: [{ question_no: '1', question_type: 'fill', content: '1+1=?', answer: '2' }],
    }
    else if (url.pathname.endsWith('/image')) return route.fulfill({ status: 200, contentType: 'image/png', body: png })
    else if (url.pathname.includes('/pending')) data = []
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  return context
}

try {
  const importContext = await contextFor(false)
  const importPage = await importContext.newPage()
  importPage.on('pageerror', error => errors.push(error.message))
  await importPage.goto(base + '/upload')
  await importPage.locator('#mathagent-startup').waitFor({ state: 'hidden' })
  const input = importPage.locator('input[type="file"][accept*=".zip"]')
  await input.waitFor({ state: 'attached' })
  await importPage.waitForFunction(() => !document.querySelector('input[accept*=".zip"]').disabled)
  const zip = new JSZip()
  const paths = ['张三/abc.png', '李四/abc.png', '随机目录1/张三_2.png', '随机目录2/李四_2.png', '张三_3.png']
  paths.forEach(path => zip.file(path, png))
  await input.setInputFiles({ name: '收集.zip', mimeType: 'application/zip', buffer: await zip.generateAsync({ type: 'nodebuffer' }) })
  await importPage.getByText('已选 5 个文件', { exact: false }).waitFor()
  assert.equal(await input.isDisabled(), false)
  await importPage.screenshot({ path: resolve(output, 'zip-import.png'), fullPage: true })
  results.push({ scenario: 'ZIP import through actual Vue input', selected: 5 })
  assert.equal(submissions.length, 0)
  await importContext.close()

  const reviewContext = await contextFor(true)
  const reviewPage = await reviewContext.newPage()
  reviewPage.on('pageerror', error => errors.push(error.message))
  await reviewPage.goto(base + '/upload')
  await reviewPage.locator('#mathagent-startup').waitFor({ state: 'hidden' })
  const dialog = reviewPage.getByRole('dialog', { name: '确认学生身份' })
  await dialog.waitFor()
  assert.equal(await dialog.locator('.identity-card').count(), 1)
  await dialog.getByText('此次选择应用于同一目录内的 2 张图片。', { exact: false }).waitFor()
  assert.equal(await dialog.getByRole('button', { name: /查看第 \d 张/ }).count(), 2)
  assert.equal(await dialog.locator('input[value="create"]').isDisabled(), true)
  await dialog.locator('select').selectOption('2')
  const submit = dialog.getByRole('button', { name: '确认身份并开始批改' })
  assert.equal(await submit.isEnabled(), true)
  for (const width of [1366, 768, 390]) {
    await reviewPage.setViewportSize({ width, height: 900 })
    const overflow = await dialog.locator('.identity-controls').evaluate(element => element.scrollWidth - element.clientWidth)
    assert.ok(overflow <= 2, `identity source path overflows by ${overflow}px at ${width}`)
    await dialog.screenshot({ path: resolve(output, `identity-group-${width}.png`) })
  }
  await submit.click()
  await reviewPage.waitForTimeout(100)
  assert.equal(submissions.length, 1)
  assert.deepEqual(submissions[0].body.identity_resolutions.map(item => [item.paper_id, item.student_id]), [['paper-1', 2], ['paper-2', 2]])
  results.push({ scenario: 'one teacher choice expands to two page confirmations', widths: [1366, 768, 390], submittedPages: 2 })
  await reviewContext.close()
  assert.deepEqual(errors, [])
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ mockOnly: true, realAccountRequests: 0, results, errors, submissions }, null, 2))
  console.log(JSON.stringify({ ok: true, results, output }))
} finally {
  await browser.close()
  await server.close()
}
