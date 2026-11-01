import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { createDesktopBindingManager, desktopProcessorReady, formatDesktopBindingError } from '../src/composables/useDesktopBindingDialog.js'

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

test('real Vue scripts and templates compile and all entries defer writes to one global dialog', () => {
  for (const path of ['views/Settings.vue', 'views/Profile.vue', 'components/UsageGuideDialog.vue', 'components/DesktopBindingDialog.vue']) {
    const source = readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
    const parsed = parse(source, { filename: path })
    assert.deepEqual(parsed.errors, [])
    const script = compileScript(parsed.descriptor, { id: path })
    const template = compileTemplate({ source: parsed.descriptor.template.content, filename: path, id: path, compilerOptions: { bindingMetadata: script.bindings } })
    assert.deepEqual(template.errors, [])
    if (!path.endsWith('DesktopBindingDialog.vue')) {
      assert.doesNotMatch(source, /desktopInvoke\('(bind|unbind|rotate)_teacher_desktop'/)
    }
  }
  const guide = readFileSync(new URL('../src/components/UsageGuideDialog.vue', import.meta.url), 'utf8')
  assert.equal((guide.match(/<DesktopBindingDialog\s*\/>/g) || []).length, 1)
  assert.match(guide, /<\/el-dialog>\s*<DesktopBindingDialog \/>/)
  assert.match(guide, /desktopSetupResolved\.value = outcome\.verified && !outcome\.error && processorReady\.value/)
  const dialog = readFileSync(new URL('../src/components/DesktopBindingDialog.vue', import.meta.url), 'utf8')
  assert.match(dialog, /@confirm="manager\.unbind\(\)"/)
  assert.match(dialog, /返回使用引导/)
})
