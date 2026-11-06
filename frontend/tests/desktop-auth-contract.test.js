import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { loadApiClient } from './helpers/apiClient.js'
import { mountComponent } from './helpers/mountComponent.js'

const { useAuthStore } = await import('../src/stores/auth.js')
const { default: Settings } = await import('../src/views/Settings.vue?sfc')
const { useDesktopBindingDialog } = await import('../src/composables/useDesktopBindingDialog.js')

async function authStoreWith(localStorage) {
  const client = await loadApiClient({ localStorage })
  setActivePinia(createPinia())
  return { client, auth: useAuthStore() }
}

// The axios base URL and the URL native fetch receives, for one client.
async function apiTargets(client) {
  await client.api.auth.me()
  const fetched = []
  const realFetch = globalThis.fetch
  globalThis.fetch = async (url) => {
    fetched.push(url)
    return new Response('xlsx')
  }
  try {
    await client.api.bank.exportPrivate({ education_stage: 'junior' })
  } finally {
    globalThis.fetch = realFetch
  }
  return { axios: client.requests.map(({ baseURL, url }) => [baseURL, url]), fetch: fetched }
}

test('desktop API origin covers axios and native fetch without changing the web default', async () => {
  const desktop = await loadApiClient({ env: { VITE_API_ORIGIN: ' https://desktop.example:8443/ ' } })
  assert.deepEqual(await apiTargets(desktop), {
    axios: [['https://desktop.example:8443/api', '/auth/me']],
    fetch: ['https://desktop.example:8443/api/bank/export?education_stage=junior'],
  })
})

test('web build keeps axios and native fetch on the same-origin /api path', async () => {
  const web = await loadApiClient({ localStorage: { mathagent_token: 'web-token' } })
  assert.deepEqual(await apiTargets(web), {
    axios: [['/api', '/auth/me']],
    fetch: ['/api/bank/export?education_stage=junior'],
  })
})

test('invalid serialized tokens never create a logged-in session', async () => {
  for (const stored of ['undefined', 'null', ' NULL ', 'Undefined', '   ', '']) {
    const { client, auth } = await authStoreWith({ mathagent_token: stored, mathagent_user: '{"id":"t1"}' })
    assert.equal(auth.token, '', JSON.stringify(stored))
    assert.equal(auth.isLoggedIn, false, JSON.stringify(stored))
    assert.equal(auth.user, null, JSON.stringify(stored))
    assert.equal(client.localStorage.getItem('mathagent_token'), null, JSON.stringify(stored))
    assert.equal(client.localStorage.getItem('mathagent_user'), null, JSON.stringify(stored))
  }

  const { client, auth } = await authStoreWith({ mathagent_token: ' saved-token ', mathagent_user: '{"id":"t1"}' })
  assert.equal(auth.token, 'saved-token')
  assert.equal(auth.isLoggedIn, true)
  const snapshot = () => ['mathagent_token', 'mathagent_user'].map(key => client.localStorage.getItem(key))
  const before = snapshot()
  for (const token of [undefined, null, 42, 'undefined', 'Null', '  ']) {
    assert.throws(() => auth.login(token, { id: 't2' }), { message: '登录响应缺少有效凭据' }, String(token))
  }
  assert.equal(auth.token, 'saved-token')
  assert.deepEqual(auth.user, { id: 't1' })
  assert.deepEqual(snapshot(), before)
})

const ADMIN = { id: 1, role: 'admin', education_stage: 'senior', profile_complete: true }
const EXPIRED = {
  localProcessingAvailable: true,
  computeBackend: 'windows_cpu_opencv',
  handoff: { configured: true, running: true, connected: false, lastErrorCode: 'node_auth_failed', completed: 0, failed: 0, remaining: 2 },
}
const REBOUND = {
  localProcessingAvailable: true,
  computeBackend: 'windows_cpu_opencv',
  handoff: { configured: true, running: true, connected: true, lastErrorCode: null, completed: 0, failed: 0, remaining: 2 },
}

// The settings page inside the Windows client: the desktop runtime answers
// the page's commands (recorded in `commands`), reporting `workerStatus()`.
async function openDesktopSettings(t, { localStorage, workerStatus }) {
  const client = await loadApiClient({ localStorage })
  const commands = []
  const desktopRuntime = {
    install() {
      window.__TAURI_INTERNALS__ = {
        invoke: async (command) => {
          commands.push(command)
          if (command === 'runtime_info') return { runtime: 'desktop' }
          if (command === 'get_worker_status') return workerStatus()
          return {}
        },
      }
    },
  }
  const page = await mountComponent(t, Settings, { path: '/settings', plugins: [desktopRuntime] })
  // The binding dialog is one for the whole app: whatever a test leaves open
  // (and any open() still waiting on it) is closed before the next test.
  const bindingDialog = useDesktopBindingDialog()
  t.after(() => bindingDialog.close(true))
  const card = () => page.root.byClass('desktop-acceleration-card')
  return { client, page, commands, card, repairButton: () => card().find(el => el.tagName === 'el-button' && el.attributes.type === 'primary') }
}

const BINDING_WRITES = ['bind_teacher_desktop', 'rotate_teacher_desktop', 'unbind_teacher_desktop']

// A repair that waits on the dialog forever would otherwise hang the run.
const SETTINGS_TIMEOUT = { timeout: 10_000 }

test('expired desktop authorization exposes an in-place credential repair', SETTINGS_TIMEOUT, async (t) => {
  let status = EXPIRED
  const { page, commands, card, repairButton } = await openDesktopSettings(t, {
    localStorage: { mathagent_token: 'admin-token', mathagent_user: JSON.stringify(ADMIN) },
    workerStatus: () => status,
  })
  assert.deepEqual(commands, ['runtime_info', 'get_worker_status'])
  assert.equal(card().find(el => el.tagName === 'el-tag').text, '需重新绑定')
  assert.equal(card().byClass('desktop-status__primary').text, '本地处理器可用，但设备授权已失效；重新绑定前将由云端接管')
  assert.equal(repairButton().text, '重新绑定')

  // Repair opens the shared binding dialog for this page; the page itself
  // never binds, rotates or unbinds the device.
  const bindingDialog = useDesktopBindingDialog()
  const repaired = repairButton().dispatch('click')
  await page.flush()
  assert.equal(bindingDialog.state.visible, true)
  assert.equal(bindingDialog.state.source, 'settings')
  assert.equal(repairButton().getAttribute('loading'), '')

  // Once the teacher is done with the dialog, the page reads the worker again.
  status = REBOUND
  bindingDialog.close()
  await repaired
  await page.flush()
  assert.deepEqual(commands, ['runtime_info', 'get_worker_status', 'get_worker_status'])
  assert.equal(commands.some(command => BINDING_WRITES.includes(command)), false)
  assert.equal(card().find(el => el.tagName === 'el-tag').text, 'CPU')
  assert.equal(repairButton().text, '本机绑定管理')
})

test('a signed-out teacher is told to log in again instead of repairing the desktop binding', SETTINGS_TIMEOUT, async (t) => {
  const { client, page, commands, repairButton } = await openDesktopSettings(t, {
    localStorage: {},
    workerStatus: () => EXPIRED,
  })
  assert.equal(repairButton().text, '重新绑定')
  // Not awaited: a repair that went ahead would wait on the dialog.
  repairButton().dispatch('click')
  await page.flush()
  assert.deepEqual(client.messages, [{ type: 'error', message: '登录已过期，请重新登录' }])
  assert.equal(useDesktopBindingDialog().state.visible, false)
  assert.deepEqual(commands, ['runtime_info', 'get_worker_status'])
})
