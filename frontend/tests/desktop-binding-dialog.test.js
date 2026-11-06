import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { createPinia } from 'pinia'
import { createDesktopBindingManager, desktopProcessorReady, formatDesktopBindingError } from '../src/composables/useDesktopBindingDialog.js'
import { TEACHER, assertAbsent, cpuWorker, desktopBridge, dialogTitled, isOpen, signIn, teleportToBody } from './helpers/homeGuideLayout.js'
import { mountComponent } from './helpers/mountComponent.js'

const readyWorker = () => ({
  localProcessingAvailable: true, paused: false, computeBackend: 'windows_cpu_opencv',
  handoff: { configured: true, running: true, connected: true, lastErrorCode: null },
})
const binding = { configured: true, nodeId: 'synthetic-node' }

test('native busy, invalid service response and identity errors retain distinct actionable messages', () => {
  assert.match(formatDesktopBindingError('desktop binding operation is already in progress'), /稍候重试/)
  assert.match(formatDesktopBindingError('desktop credential rotation response was invalid'), /服务返回的数据不完整/)
  assert.doesNotMatch(formatDesktopBindingError('desktop credential rotation response was invalid'), /Windows/)
  assert.match(formatDesktopBindingError('desktop device status does not match the stored credential'), /已停止操作/)
  assert.match(formatDesktopBindingError('Windows could not store the desktop credential'), /Windows/)
})
function fixture(overrides = {}) {
  const account = { accountId: 910001, token: 'mock-only-token' }
  const calls = []
  const manager = createDesktopBindingManager({
    getSession: () => account,
    wait: async () => {},
    invoke: async (command, args) => {
      calls.push({ command, args })
      if (overrides[command]) return overrides[command](args)
      if (command === 'get_worker_status' || command === 'resume_worker') return readyWorker()
      if (command === 'unbind_teacher_desktop') return { configured: false }
      return { ...binding }
    },
  })
  return { manager, calls, account }
}
function deferred() {
  let resolve
  const promise = new Promise(yes => { resolve = yes })
  return { promise, resolve }
}

test('three entries share one manager request; opening/refreshing does not bind or verify', async () => {
  const { manager, calls } = fixture()
  const first = manager.open('guide')
  assert.equal(manager.open('settings'), first)
  assert.equal(manager.open('profile'), first)
  await manager.refresh()
  assert.equal(manager.state.binding.configured, true)
  assert.equal(manager.state.verified, false)
  assert.deepEqual(calls.map(call => call.command), ['desktop_binding_status', 'get_worker_status'])
  manager.close()
  const outcome = await first
  assert.equal(outcome.verified, false)
  assert.equal(outcome.attempted, false)
})

test('configured credentials still validate the current teacher exactly once; duplicate clicks do not mutate', async () => {
  const gate = deferred()
  const { manager, calls } = fixture({ bind_teacher_desktop: () => gate.promise })
  const result = manager.open('guide')
  await manager.refresh()
  const operation = manager.bind()
  assert.equal(await manager.bind(), false)
  assert.equal(await manager.unbind(), false)
  assert.equal(manager.close(), false)
  gate.resolve(binding)
  assert.equal(await operation, true)
  assert.equal(manager.state.verified, true)
  assert.equal(calls.filter(call => call.command === 'bind_teacher_desktop').length, 1)
  assert.deepEqual(calls.find(call => call.command === 'bind_teacher_desktop').args, { bearerToken: 'mock-only-token' })
  manager.close()
  assert.equal((await result).verified, true)
})

test('conflict never automatically unbinds or rotates and cannot complete setup', async () => {
  const { manager, calls } = fixture({ bind_teacher_desktop: () => { throw new Error('desktop device is already registered') } })
  const result = manager.open('guide')
  assert.equal(await manager.bind(), false)
  assert.match(manager.state.error, /无需先解除绑定/)
  assert.equal(manager.state.verified, false)
  assert.deepEqual(calls.map(call => call.command), ['bind_teacher_desktop'])
  manager.close()
  assert.equal((await result).attempted, true)
})

test('binding resumes a paused worker, waits for server connection, and honors hardware safety', async () => {
  let reads = 0
  const { manager, calls } = fixture({ get_worker_status: () => {
    reads += 1
    return reads === 1 ? { ...readyWorker(), paused: true } : readyWorker()
  }, resume_worker: () => ({ ...readyWorker(), handoff: { configured: true, running: true, connected: false } }) })
  manager.open('guide')
  await manager.bind()
  assert.ok(calls.some(call => call.command === 'resume_worker'))
  assert.ok(reads >= 2)
  assert.equal(desktopProcessorReady(manager.state.binding, manager.state.worker), true)
  for (const worker of [
    { ...readyWorker(), localProcessingAvailable: false },
    { ...readyWorker(), paused: true },
    { ...readyWorker(), handoff: { configured: true, running: true, connected: false } },
    { ...readyWorker(), handoff: { configured: true, running: true, connected: true, lastErrorCode: 'node_auth_failed' } },
  ]) assert.equal(desktopProcessorReady(binding, worker), false)
  manager.close()
})

test('unavailable hardware keeps successful account validation distinct from processor readiness', async () => {
  const { manager, calls } = fixture({ get_worker_status: () => ({ ...readyWorker(), localProcessingAvailable: false }) })
  manager.open('guide')
  await manager.bind()
  assert.equal(manager.state.verified, true)
  assert.equal(desktopProcessorReady(manager.state.binding, manager.state.worker), false)
  assert.ok(calls.filter(call => call.command === 'get_worker_status').length <= 7)
  manager.close()
})

test('missing login never reaches native commands; account switch discards an in-flight result', async () => {
  const first = fixture()
  first.account.token = ''
  first.manager.open('guide')
  assert.equal(await first.manager.bind(), false)
  assert.equal(first.calls.length, 0)
  first.manager.close()
  const gate = deferred()
  const { manager, account, calls } = fixture({ bind_teacher_desktop: () => gate.promise })
  const outcome = manager.open('guide')
  const operation = manager.bind()
  account.token = 'mock-second-token'
  account.accountId = 910002
  manager.invalidateSession()
  gate.resolve(binding)
  assert.equal(await operation, false)
  assert.equal((await outcome).verified, false)
  assert.equal(manager.state.binding, null)
  assert.equal(manager.state.verified, false)
  assert.equal(calls.length, 1, 'No resume, polling, or unbind after account changed')
})

test('failed live worker reads discard previously ready status after bind and refresh', async () => {
  let fail = false
  const { manager } = fixture({ get_worker_status: () => {
    if (fail) throw new Error('worker status unavailable')
    return readyWorker()
  } })
  const outcome = manager.open('guide')
  await manager.refresh()
  assert.equal(desktopProcessorReady(manager.state.binding, manager.state.worker), true)
  fail = true
  assert.equal(await manager.bind(), false)
  assert.equal(manager.state.worker, null)
  assert.equal(desktopProcessorReady(manager.state.binding, manager.state.worker), false)
  assert.ok(manager.state.error)
  fail = false
  await manager.refresh()
  assert.equal(desktopProcessorReady(manager.state.binding, manager.state.worker), true)
  fail = true
  assert.equal(await manager.refresh(), false)
  assert.equal(manager.state.worker, null)
  manager.close()
  const result = await outcome
  assert.equal(Boolean(result.verified && !result.error && desktopProcessorReady(result.binding, result.worker)), false)
})

test('explicit unbind clears verification without issuing another registration', async () => {
  const { manager, calls } = fixture()
  manager.open('profile')
  await manager.bind()
  await manager.unbind()
  assert.equal(manager.state.binding.configured, false)
  assert.equal(manager.state.verified, false)
  assert.equal(calls.at(-1).command, 'unbind_teacher_desktop')
  assert.equal(calls.filter(call => call.command === 'bind_teacher_desktop').length, 1)
  manager.close()
})

// Mounted pages share one Pinia (see helpers/homeGuideLayout.js): the binding
// manager is one instance for the whole app.
const pinia = createPinia()
const { default: Layout } = await import('../src/views/Layout.vue?sfc')
const { default: Settings } = await import('../src/views/Settings.vue?sfc')
const { default: Profile } = await import('../src/views/Profile.vue?sfc')
const { default: UsageGuideDialog } = await import('../src/components/UsageGuideDialog.vue?sfc')
const { default: DesktopBindingDialog } = await import('../src/components/DesktopBindingDialog.vue?sfc')

const NODE = 'synthetic-node'
const ADMIN = { ...TEACHER, role: 'admin' }

// The desktop client, bound and ready, whose first-run check and tour are done.
function desktopClient() {
  return desktopBridge({
    runtime_info: { runtime: 'desktop', webFallbackConfigured: false },
    desktop_binding_status: { configured: true, nodeId: NODE },
    get_worker_status: cpuWorker(),
    bind_teacher_desktop: { configured: true, nodeId: NODE },
    unbind_teacher_desktop: { configured: false },
  })
}

async function signedInDesktop(t) {
  teleportToBody(t)
  const { client } = await signIn(pinia, {
    user: ADMIN,
    storage: { disclaimer_agreed: 'true', [`mathagent_usage_guide_v3:${ADMIN.id}`]: 'true', [`mathagent_desktop_setup_v1:${ADMIN.id}`]: NODE },
  })
  client.respond(() => ({ data: {} }))
  return client
}

const bindingDialogs = root => root.findAll(el => el.tagName === 'el-dialog' && el.getAttribute('title') === '本机绑定管理')
const footerOf = dialog => dialog.find(el => el.tagName === 'el-slot' && el.getAttribute('name') === 'footer')
const writes = bridge => bridge.commands().filter(command => /_teacher_desktop$/.test(command))

// A page's 本机绑定管理 entry; its handler waits until the dialog closes.
async function openFrom(page, root = page.root) {
  root.byText('el-button', '本机绑定管理').dispatch('click')
  await page.flush()
  const dialogs = bindingDialogs(page.root)
  assert.equal(dialogs.length, 1, 'one binding dialog for the whole app')
  assert.equal(isOpen(dialogs[0]), true)
  return dialogs[0]
}

for (const [entry, View, path] of [['settings', Settings, '/settings'], ['profile', Profile, '/profile']]) {
  test(`the ${entry} page opens the global binding dialog and never writes itself`, async (t) => {
    const bridge = desktopClient()
    await signedInDesktop(t)
    const page = await mountComponent(t, View, { path, layout: Layout, pinia, plugins: [bridge.plugin] })
    assert.deepEqual(page.errors, [])
    assert.equal(bindingDialogs(page.root).length, 1, 'the layout renders the dialog, closed')
    assert.equal(isOpen(bindingDialogs(page.root)[0]), false)
    const before = bridge.calls.length
    const dialog = await openFrom(page, page.root.byClass(entry === 'settings' ? 'desktop-acceleration-card' : 'desktop-binding'))
    assert.equal(footerOf(dialog).text, '完成')
    assert.deepEqual(bridge.commands().slice(before), ['desktop_binding_status', 'get_worker_status'], 'opening only reads status')
    assert.deepEqual(writes(bridge), [])
    await dialog.byText('el-button', '完成').dispatch('click')
    await page.flush()
    assert.equal(isOpen(bindingDialogs(page.root)[0]), false)
    assert.deepEqual(writes(bridge), [])
  })
}

test('real Vue scripts and templates compile and all entries defer writes to one global dialog', async (t) => {
  // The guide's entry: the binding dialog is the shared one, beside (not
  // inside) the guide's own dialog, and returns to the guide.
  const bridge = desktopClient()
  await signedInDesktop(t)
  const page = await mountComponent(t, UsageGuideDialog, { path: '/upload', pinia, plugins: [bridge.plugin] })
  await page.vm.exposed.open()
  await page.flush()
  const guide = dialogTitled(page.root, '使用引导 · 本机处理检查')
  assert.equal(isOpen(guide), true)
  const dialog = await openFrom(page, guide)
  assert.equal(guide.contains(dialog), false, 'the binding dialog is not nested in the guide dialog')
  assert.equal(footerOf(dialog).text, '返回使用引导')
  assert.deepEqual(writes(bridge), [], 'the guide never binds, unbinds or rotates by itself')

  // Writes happen only from the dialog's own buttons.
  await dialog.byText('el-button', '重新绑定并检测').dispatch('click')
  await page.flush()
  assert.deepEqual(bridge.calls.filter(call => call.command === 'bind_teacher_desktop').map(call => call.args), [{ bearerToken: 'teacher-token' }])
  assert.match(dialog.find(el => el.tagName === 'el-alert').getAttribute('title'), /本机绑定已完成/)
  assert.match(dialog.text, /已校验当前账号/)
  const unbind = dialog.find(el => el.tagName === 'el-popconfirm')
  // Clicking 解除绑定 only asks for confirmation; nothing is unbound yet.
  await unbind.byText('el-button', '解除绑定').dispatch('click')
  await page.flush()
  assert.deepEqual(writes(bridge), ['bind_teacher_desktop'], 'unbinding asks for confirmation first')
  await unbind.handlers.confirm()
  await page.flush()
  assert.deepEqual(writes(bridge), ['bind_teacher_desktop', 'unbind_teacher_desktop'])
  assert.match(dialog.text, /未登记/)
  assertAbsent(dialog.find(el => el.tagName === 'el-popconfirm'), 'nothing left to unbind')

  await dialog.byText('el-button', '返回使用引导').dispatch('click')
  await page.flush()
  assert.equal(isOpen(dialog), false)
  assert.equal(isOpen(guide), true, 'back in the guide')
  assert.match(guide.text, /待绑定/)
  page.unmount()

  // The dialog compiles and mounts on its own too; it stays closed until a
  // page opens the manager.
  const alone = await mountComponent(t, DesktopBindingDialog, { pinia })
  assert.deepEqual(alone.errors, [])
  assert.equal(isOpen(bindingDialogs(alone.root)[0]), false)
})

test('the guide counts the check as resolved only after a verified, error-free, ready binding', async (t) => {
  const bridge = desktopClient()
  await signedInDesktop(t)
  const page = await mountComponent(t, UsageGuideDialog, { path: '/upload', pinia, plugins: [bridge.plugin] })
  bridge.replies.desktop_binding_status = { configured: true, nodeId: 'another-node' }
  await page.vm.exposed.open()
  await page.flush()
  const guide = dialogTitled(page.root, '使用引导 · 本机处理检查')
  const complete = () => guide.byClass('guide-footer').byTag('el-button')[0]
  assert.equal(complete().hasAttribute('disabled'), true, 'a new binding must be checked')

  // Verified, but the processor status could not be read.
  let dialog = await openFrom(page, guide)
  bridge.replies.bind_teacher_desktop = { configured: true, nodeId: 'another-node' }
  bridge.replies.get_worker_status = () => { throw new Error('worker status unavailable') }
  await dialog.byText('el-button', '重新绑定并检测').dispatch('click')
  await page.flush()
  await dialog.byText('el-button', '返回使用引导').dispatch('click')
  await page.flush()
  assert.equal(complete().hasAttribute('disabled'), true)

  // Verified, no error, processor ready.
  bridge.replies.get_worker_status = cpuWorker()
  dialog = await openFrom(page, guide)
  await dialog.byText('el-button', '重新绑定并检测').dispatch('click')
  await page.flush()
  await dialog.byText('el-button', '返回使用引导').dispatch('click')
  await page.flush()
  assert.equal(complete().hasAttribute('disabled'), false)

  // Closing without binding again does not keep an old verification.
  dialog = await openFrom(page, guide)
  await dialog.byText('el-button', '返回使用引导').dispatch('click')
  await page.flush()
  assert.equal(complete().hasAttribute('disabled'), true)

  // Verified and ready, but the teacher's next action in the dialog failed:
  // the check is not resolved while that error stands.
  bridge.replies.unbind_teacher_desktop = () => { throw new Error('Windows credential storage failed') }
  dialog = await openFrom(page, guide)
  await dialog.byText('el-button', '重新绑定并检测').dispatch('click')
  await page.flush()
  await dialog.find(el => el.tagName === 'el-popconfirm').handlers.confirm()
  await page.flush()
  assert.match(dialog.find(el => el.tagName === 'el-alert').getAttribute('title'), /Windows/)
  assert.match(dialog.text, /已校验当前账号/, 'the failed unbind leaves the verified binding')
  await dialog.byText('el-button', '返回使用引导').dispatch('click')
  await page.flush()
  assert.equal(complete().hasAttribute('disabled'), true)
})
