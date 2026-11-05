import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { loadApiClient } from './helpers/apiClient.js'

const settingsSource = readFileSync(new URL('../src/views/Settings.vue', import.meta.url), 'utf8')
const { useAuthStore } = await import('../src/stores/auth.js')

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

test('expired desktop authorization exposes an in-place credential repair', () => {
  assert.match(settingsSource, /lastErrorCode === 'node_auth_failed'/)
  assert.match(settingsSource, /重新绑定/)
  assert.match(settingsSource, /@click="repairDesktopBinding"/)
  assert.match(settingsSource, /bindingManager\.open\('settings'\)/)
  assert.doesNotMatch(settingsSource, /desktopInvoke\('(bind|rotate|unbind)_teacher_desktop'/)
  assert.match(settingsSource, /desktopWorkerStatus\.value = await desktopInvoke\('get_worker_status'\)/)
  assert.match(settingsSource, /登录已过期，请重新登录/)
})
