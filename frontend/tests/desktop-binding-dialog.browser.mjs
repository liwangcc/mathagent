// Real Vue/Element Plus pages, with synthetic HTTP and native IPC only.
// Start Vite, then run: node tests/desktop-binding-dialog.browser.mjs http://127.0.0.1:5196 <artifacts-dir>
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

let playwright
try {
  playwright = await import('playwright')
} catch {
  const entry = process.env.PLAYWRIGHT_ENTRY
  if (!entry) throw new Error('playwright is not installed; set PLAYWRIGHT_ENTRY to its index.mjs')
  playwright = await import(pathToFileURL(entry))
}
const base = process.argv[2] || 'http://127.0.0.1:5196'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only local fully mocked pages may be tested')
const artifacts = resolve(process.argv[3] || '../live_desktop_binding_fix_20260909/browser-mock')
mkdirSync(artifacts, { recursive: true })
const browser = await playwright.chromium.launch({ headless: true, channel: 'msedge' })
const results = []
const errors = []
const user = { id: 910001, username: 'mock-teacher', name: '模拟教师', role: 'admin', education_stage: 'senior', profile_complete: true, current_class_id: null }

async function fixture(firstSetup = false, viewport = { width: 1360, height: 960 }) {
  const context = await browser.newContext({ viewport })
  const apiCalls = []
  await context.addInitScript(({ user, firstSetup }) => {
    localStorage.setItem('mathagent_token', 'mock-only-token-not-a-real-account')
    localStorage.setItem('mathagent_user', JSON.stringify(user))
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem(`mathagent_usage_guide_v3:${user.id}`, 'true')
    if (!firstSetup) localStorage.setItem(`mathagent_desktop_setup_v1:${user.id}`, 'synthetic-node')
    window.__bindingMock = { calls: [], failure: '', paused: false, hardware: true, connected: true, configured: true, hold: false }
    window.__TAURI_INTERNALS__ = { invoke: async (command, args = {}) => {
      const mock = window.__bindingMock
      mock.calls.push({ command, args })
      const binding = () => ({ configured: mock.configured, nodeId: mock.configured ? 'synthetic-node' : null })
      const worker = () => ({
        paused: mock.paused, localProcessingAvailable: mock.hardware, computeBackend: 'windows_cpu_opencv',
        webFallbackRequired: !mock.hardware,
        handoff: { configured: mock.configured, running: true, connected: mock.connected, lastErrorCode: null },
        cpuAcceleration: { workerCount: 2, reasonCodes: [] },
      })
      if (command === 'runtime_info') return { runtime: 'desktop', webFallbackConfigured: true }
      if (command === 'desktop_binding_status') return binding()
      if (command === 'get_worker_status') return worker()
      if (command === 'bind_teacher_desktop') {
        if (mock.hold) await new Promise(resolve => { mock.release = resolve })
        if (mock.failure) throw new Error(mock.failure)
        mock.configured = true
        return binding()
      }
      if (command === 'resume_worker') { mock.paused = false; return worker() }
      if (command === 'unbind_teacher_desktop') { mock.configured = false; return binding() }
      if (command === 'open_web_fallback') return null
      throw new Error(`Unexpected native command: ${command}`)
    } }
  }, { user, firstSetup })
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    apiCalls.push(`${request.method()} ${url.pathname}`)
    let data = {}
    if (url.pathname === '/api/auth/me') data = { user }
    else if (url.pathname === '/api/auth/classes') data = { classes: [], current_class_id: null }
    else if (url.pathname.includes('/pending')) data = []
    else if (url.pathname.includes('/availability')) data = { status: 'available', hardware: { status: 'ready' } }
    else if (url.pathname === '/api/settings') data = { is_configured: false, llm_model: '', llm_base_url: '' }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  return { context, page, apiCalls }
}
const managerDialog = page => page.getByRole('dialog', { name: '本机绑定管理', exact: true })
const guideDialog = page => page.getByRole('dialog', { name: /使用引导/ })
async function waitLoaded(page) {
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
}
async function readyManager(page) {
  await managerDialog(page).waitFor()
  await page.waitForFunction(() => window.__bindingMock.calls.some(call => call.command === 'desktop_binding_status'))
  await managerDialog(page).getByRole('button', { name: '重新检测状态', exact: true }).waitFor()
  await page.waitForFunction(() => ![...document.querySelectorAll('.el-dialog')].find(el => el.textContent.includes('账号校验'))?.querySelector('.is-loading'))
}

try {
  {
    const { context, page, apiCalls } = await fixture()
    await page.goto(`${base}/settings`)
    await waitLoaded(page)
    await page.getByRole('button', { name: '本机绑定管理', exact: true }).click()
    await readyManager(page)
    assert.equal(await managerDialog(page).count(), 1)
    assert.equal(await page.evaluate(() => window.__bindingMock.calls.filter(call => /^(bind|unbind|rotate)_teacher_desktop$/.test(call.command)).length), 0)
    await managerDialog(page).getByRole('button', { name: '重新绑定并检测', exact: true }).click()
    await managerDialog(page).getByText('本机绑定已完成，处理器自检和服务器连接均已就绪。').waitFor()
    await page.screenshot({ path: resolve(artifacts, 'settings-shared-binding.png'), fullPage: true })
    await managerDialog(page).getByRole('button', { name: '完成', exact: true }).click()
    await managerDialog(page).waitFor({ state: 'hidden' })
    let calls = await page.evaluate(() => window.__bindingMock.calls)
    assert.equal(calls.filter(call => call.command === 'bind_teacher_desktop').length, 1)
    assert.equal(calls.find(call => call.command === 'bind_teacher_desktop').args.bearerToken, 'mock-only-token-not-a-real-account')
    results.push({ case: 'settings', commands: calls.map(call => call.command), apiCalls: [...apiCalls] })
    await page.goto(`${base}/profile`)
    await waitLoaded(page)
    await page.getByRole('button', { name: '本机绑定管理', exact: true }).click()
    await readyManager(page)
    await managerDialog(page).getByRole('button', { name: '解除绑定', exact: true }).click()
    assert.equal(await page.evaluate(() => window.__bindingMock.calls.filter(call => call.command === 'unbind_teacher_desktop').length), 0)
    await page.getByRole('button', { name: /^(Yes|确定)$/ }).click()
    await managerDialog(page).getByText('未登记', { exact: true }).waitFor()
    await page.screenshot({ path: resolve(artifacts, 'profile-shared-binding.png'), fullPage: true })
    calls = await page.evaluate(() => window.__bindingMock.calls)
    assert.equal(calls.filter(call => call.command === 'unbind_teacher_desktop').length, 1)
    assert.equal(calls.filter(call => call.command === 'rotate_teacher_desktop').length, 0)
    results.push({ case: 'profile-explicit-unbind', commands: calls.map(call => call.command) })
    await context.close()
  }
  {
    const { context, page, apiCalls } = await fixture(true)
    await page.goto(`${base}/profile`)
    await waitLoaded(page)
    await guideDialog(page).waitFor()
    assert.equal(await guideDialog(page).getByRole('button', { name: '完成引导', exact: true }).isDisabled(), true)
    await guideDialog(page).getByRole('button', { name: '本机绑定管理', exact: true }).click()
    await readyManager(page)
    await managerDialog(page).getByRole('button', { name: '返回使用引导', exact: true }).click()
    await managerDialog(page).waitFor({ state: 'hidden' })
    assert.equal(await guideDialog(page).getByRole('button', { name: '完成引导', exact: true }).isDisabled(), true)
    assert.equal(await page.evaluate(() => localStorage.getItem('mathagent_desktop_setup_v1:910001')), null)
    await guideDialog(page).getByRole('button', { name: '本机绑定管理', exact: true }).click()
    await readyManager(page)
    await page.evaluate(() => { window.__bindingMock.failure = 'desktop device is already registered' })
    await managerDialog(page).getByRole('button', { name: '重新绑定并检测', exact: true }).click()
    await managerDialog(page).getByText(/无需先解除绑定/).waitFor()
    await page.screenshot({ path: resolve(artifacts, 'guide-conflict-safe.png'), fullPage: true })
    await managerDialog(page).getByRole('button', { name: '返回使用引导', exact: true }).click()
    await managerDialog(page).waitFor({ state: 'hidden' })
    assert.equal(await guideDialog(page).getByRole('button', { name: '完成引导', exact: true }).isDisabled(), true)
    await guideDialog(page).getByRole('button', { name: '本机绑定管理', exact: true }).click()
    await readyManager(page)
    await page.evaluate(() => { window.__bindingMock.failure = ''; window.__bindingMock.paused = true })
    await managerDialog(page).getByRole('button', { name: '重新绑定并检测', exact: true }).click()
    await managerDialog(page).getByText('本机绑定已完成，处理器自检和服务器连接均已就绪。').waitFor()
    await managerDialog(page).getByRole('button', { name: '返回使用引导', exact: true }).click()
    await managerDialog(page).waitFor({ state: 'hidden' })
    assert.equal(await guideDialog(page).getByRole('button', { name: '完成引导', exact: true }).isEnabled(), true)
    await page.screenshot({ path: resolve(artifacts, 'guide-verified-return.png'), fullPage: true })
    await guideDialog(page).getByRole('button', { name: '完成引导', exact: true }).click()
    await guideDialog(page).waitFor({ state: 'hidden' })
    assert.equal(await page.evaluate(() => localStorage.getItem('mathagent_desktop_setup_v1:910001')), 'synthetic-node')
    const calls = await page.evaluate(() => window.__bindingMock.calls)
    assert.equal(calls.filter(call => call.command === 'bind_teacher_desktop').length, 2)
    assert.equal(calls.filter(call => call.command === 'resume_worker').length, 1)
    assert.equal(calls.filter(call => /^(unbind|rotate)_teacher_desktop$/.test(call.command)).length, 0)
    results.push({ case: 'first-guide-cancel-conflict-repair', commands: calls.map(call => call.command), apiCalls })
    await context.close()
  }
  {
    const { context, page } = await fixture()
    await page.goto(`${base}/profile`)
    await waitLoaded(page)
    await page.getByRole('button', { name: '本机绑定管理', exact: true }).click()
    await readyManager(page)
    await page.evaluate(() => { window.__bindingMock.hold = true })
    await managerDialog(page).getByRole('button', { name: '重新绑定并检测', exact: true }).click()
    await page.waitForFunction(() => typeof window.__bindingMock.release === 'function')
    assert.equal(await managerDialog(page).getByRole('button', { name: '完成', exact: true }).isDisabled(), true)
    await page.evaluate(async () => {
      const { useAuthStore } = await import('/src/stores/auth.js')
      const auth = useAuthStore()
      auth.token = 'mock-second-token-not-a-real-account'
      auth.user = { ...auth.user, id: 910002, name: '第二位模拟教师' }
      window.__bindingMock.release()
    })
    await managerDialog(page).waitFor({ state: 'hidden' })
    const calls = await page.evaluate(() => window.__bindingMock.calls)
    const afterBind = calls.slice(calls.findIndex(call => call.command === 'bind_teacher_desktop') + 1)
    assert.equal(afterBind.some(call => ['resume_worker', 'unbind_teacher_desktop'].includes(call.command)), false)
    assert.equal(await page.evaluate(() => localStorage.getItem('mathagent_desktop_setup_v1:910002')), null)
    results.push({ case: 'in-flight-account-change', commands: calls.map(call => call.command), newAccountNotVerified: true })
    await context.close()
  }
  {
    const { context, page } = await fixture(true, { width: 390, height: 844 })
    await page.goto(`${base}/profile`)
    await waitLoaded(page)
    await guideDialog(page).waitFor()
    await guideDialog(page).getByRole('button', { name: '本机绑定管理', exact: true }).click()
    await readyManager(page)
    await page.evaluate(() => { window.__bindingMock.hardware = false })
    await managerDialog(page).getByRole('button', { name: '重新绑定并检测', exact: true }).click()
    await page.waitForFunction(() => ![...document.querySelectorAll('.el-dialog')].find(el => el.textContent.includes('账号校验'))?.querySelector('.is-loading'))
    await managerDialog(page).getByText(/本机处理尚未就绪/).waitFor()
    await page.screenshot({ path: resolve(artifacts, 'mobile-hardware-cloud-fallback.png'), fullPage: true })
    await managerDialog(page).getByRole('button', { name: '返回使用引导', exact: true }).click()
    await managerDialog(page).waitFor({ state: 'hidden' })
    assert.equal(await guideDialog(page).getByRole('button', { name: '完成引导', exact: true }).isDisabled(), true)
    await guideDialog(page).getByRole('button', { name: '先由云端接管，稍后重试', exact: true }).click()
    assert.equal(await guideDialog(page).getByRole('button', { name: '完成引导', exact: true }).isEnabled(), true)
    results.push({ case: 'mobile-hardware-gate-explicit-cloud-fallback', viewport: { width: 390, height: 844 } })
    await context.close()
  }
  assert.deepEqual(errors, [], 'Actual Vue runtime must not emit errors')
  writeFileSync(resolve(artifacts, 'audit.json'), JSON.stringify({ passed: true, mockedApiOnly: true, mockedNativeOnly: true, realAccountRequests: 0, results, errors }, null, 2))
  console.log(JSON.stringify({ passed: true, cases: results.length, artifacts, mockedApiOnly: true, mockedNativeOnly: true, realAccountRequests: 0 }))
} catch (error) {
  writeFileSync(resolve(artifacts, 'audit-failed.json'), JSON.stringify({ passed: false, error: String(error.stack), results, errors }, null, 2))
  throw error
} finally {
  await browser.close()
}
