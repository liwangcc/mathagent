import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import {
  EDUCATION_STAGES,
  educationProfileRedirect,
  hasCompleteEducationProfile,
  normalizeEducationStage,
  safeProfileRedirect,
} from '../src/utils/educationStage.js'
import { loadApiClient } from './helpers/apiClient.js'

const { useAuthStore } = await import('../src/stores/auth.js')
const routerSource = readFileSync(new URL('../src/router/index.js', import.meta.url), 'utf8')

// A fresh auth store with empty storage whose api calls reach the recorded backend.
async function signedOutStore() {
  const client = await loadApiClient()
  setActivePinia(createPinia())
  return { store: useAuthStore(), client }
}

const storedUser = client => JSON.parse(client.localStorage.getItem('mathagent_user'))
const sent = client => client.requests.map(request => `${request.method} ${request.url}`)

function createGuard(auth) {
  let guard
  new Function('createRouter', 'createWebHistory', 'useAuthStore', 'educationProfileRedirect',
    routerSource.replace(/^import .*$/gm, '').replace('export default router', ''))(
    () => ({ beforeEach: callback => { guard = callback } }), () => ({}), () => auth, educationProfileRedirect)
  return guard
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('only school stage is required; never infer missing or invalid stages', () => {
  assert.deepEqual(EDUCATION_STAGES.map(item => item.value), ['primary', 'junior', 'senior'])
  for (const value of [null, undefined, '', '小学', '高中', 'Junior', 'Primary', 'university', 1]) assert.equal(normalizeEducationStage(value), '')
  assert.equal(hasCompleteEducationProfile({ education_stage: 'primary', profile_complete: true }), true)
  assert.equal(hasCompleteEducationProfile({ education_stage: 'junior', profile_complete: true }), true)
  assert.equal(hasCompleteEducationProfile({ education_stage: 'senior', profile_complete: false }), false)
  assert.equal(hasCompleteEducationProfile({ education_stage: '', profile_complete: true }), false)
})

test('profile continuation only accepts local non-looping paths', () => {
  assert.equal(safeProfileRedirect('/upload?batch=123#review'), '/upload?batch=123#review')
  for (const value of ['https://example.org', '//example.org', '/\\example.org', '/%2fexample.org', '/%5cfoo', '/%0afoo', '/login?x=1', '/profile/', ['/', '//x']]) {
    assert.equal(safeProfileRedirect(value), '/', String(value))
  }
})

test('missing profile redirects all teaching pages but permits profile and admin settings', () => {
  const auth = { token: 'test', user: { role: 'teacher', education_stage: null, profile_complete: false } }
  for (const name of ['home', 'upload', 'anonymous-grade', 'answer-sheet', 'bank', 'exercise', 'stats']) {
    const result = educationProfileRedirect({ name, fullPath: `/${name}`, meta: {} }, auth)
    assert.equal(result.name, 'profile')
    assert.equal(result.query.redirect, `/${name}`)
  }
  assert.equal(educationProfileRedirect({ name: 'profile', meta: {} }, auth), null)
  assert.equal(educationProfileRedirect({ name: 'login', meta: { public: true } }, auth), null)
  auth.user.role = 'admin'
  assert.equal(educationProfileRedirect({ name: 'settings', meta: { admin: true } }, auth), null)
  assert.equal(educationProfileRedirect({ name: 'upload', fullPath: '/upload', meta: {} }, auth).name, 'profile')
})

test('route guard verifies cached profile from /me before allowing direct entry', async () => {
  const auth = {
    token: 'test', userVerified: false,
    user: { education_stage: 'senior', profile_complete: true },
    async fetchMe() { this.user = { education_stage: null, profile_complete: false }; this.userVerified = true },
  }
  const result = await createGuard(auth)({ name: 'upload', fullPath: '/upload?batch=1', meta: {} })
  assert.equal(result.name, 'profile')
  assert.equal(result.query.redirect, '/upload?batch=1')
})

test('profile-complete relogin passes; profile save and logout remain reachable', async () => {
  const auth = {
    token: 'test', userVerified: false,
    async fetchMe() { this.user = { education_stage: 'junior', profile_complete: true }; this.userVerified = true },
  }
  const guard = createGuard(auth)
  assert.equal(await guard({ name: 'upload', fullPath: '/upload', meta: {} }), undefined)
  auth.user.profile_complete = false
  assert.equal(await guard({ name: 'profile', fullPath: '/profile', meta: {} }), undefined)
  auth.token = ''
  assert.equal(await guard({ name: 'login', fullPath: '/login', meta: { public: true } }), undefined)
})

test('logout while a restored session is being verified cannot enter a teaching route', async () => {
  const auth = { token: 'test', userVerified: false, async fetchMe() { this.token = ''; this.user = null } }
  const result = await createGuard(auth)({ name: 'upload', fullPath: '/upload', meta: {} })
  assert.equal(result.name, 'login')
})

test('login never trusts cached metadata and clears previous class list', async () => {
  const { store, client } = await signedOutStore()
  client.respond(() => ({ data: { user: { id: 2, education_stage: null, profile_complete: false } } }))
  // State left behind by the previous, already verified account.
  store.userVerified = true
  store.classes = [{ id: 1 }]
  store.login('new-token', { id: 2, education_stage: 'senior', profile_complete: true })
  assert.equal(store.userVerified, false)
  assert.deepEqual(store.classes, [])
  await store.fetchMe()
  assert.deepEqual(sent(client), ['get /auth/me'])
  assert.equal(client.requests[0].headers.Authorization, 'Bearer new-token')
  assert.equal(store.user.profile_complete, false)
  assert.equal(store.userVerified, true)
  assert.equal(storedUser(client).profile_complete, false)
})

test('late /me from another account cannot overwrite the current teacher', async () => {
  const old = deferred()
  const current = deferred()
  const replies = [old.promise, current.promise]
  const { store, client } = await signedOutStore()
  client.respond(() => replies.shift())
  store.login('old', { id: 1 })
  const oldRequest = store.fetchMe()
  store.login('new', { id: 2 })
  const newRequest = store.fetchMe()
  current.resolve({ data: { user: { id: 2, education_stage: 'junior', profile_complete: true } } })
  await newRequest
  old.resolve({ data: { user: { id: 1, education_stage: 'senior', profile_complete: true } } })
  assert.equal(await oldRequest, null)
  assert.deepEqual(sent(client), ['get /auth/me', 'get /auth/me'])
  assert.equal(store.user.id, 2)
  assert.equal(storedUser(client).education_stage, 'junior')
})

test('profile update loads persisted /me and never reuses an earlier in-flight snapshot', async () => {
  const old = deferred()
  const meReplies = [old.promise, { data: { user: { id: 1, education_stage: 'junior', profile_complete: true } } }]
  const { store, client } = await signedOutStore()
  client.respond(config => (config.method === 'get'
    ? meReplies.shift()
    : { data: { user: { education_stage: 'senior' } } }))
  store.login('same', { id: 1 })
  const stale = store.fetchMe()
  const saved = await store.updateProfile({ education_stage: 'junior' })
  old.resolve({ data: { user: { id: 1, education_stage: null, profile_complete: false } } })
  await stale
  assert.deepEqual(sent(client), ['get /auth/me', 'put /auth/me', 'get /auth/me'])
  assert.deepEqual(JSON.parse(client.requests[1].data), { education_stage: 'junior' })
  assert.equal(saved.education_stage, 'junior')
  assert.equal(store.user.education_stage, 'junior')
  assert.equal(storedUser(client).education_stage, 'junior')
})

test('late profile save and logout do not replace or clear a newly signed-in account', async () => {
  const save = deferred()
  const logout = deferred()
  const { store, client } = await signedOutStore()
  const replies = { 'put /auth/me': save.promise, 'post /auth/logout': logout.promise }
  client.respond(config => replies[`${config.method} ${config.url}`])
  store.login('old', { id: 1 })
  const oldSave = store.updateProfile({ education_stage: 'senior' })
  const oldLogout = store.logout()
  store.login('new', { id: 2 })
  save.resolve({ data: { user: { id: 1 } } })
  logout.resolve({ data: { ok: true } })
  assert.equal(await oldSave, null)
  await oldLogout
  assert.deepEqual(sent(client), ['put /auth/me', 'post /auth/logout'])
  assert.equal(store.token, 'new')
  assert.equal(store.user.id, 2)
  assert.equal(client.localStorage.getItem('mathagent_token'), 'new')
  assert.equal(storedUser(client).id, 2)
})

test('late class selection cannot contaminate a new account and its profile scope', async () => {
  const request = deferred()
  const { store, client } = await signedOutStore()
  client.respond(() => request.promise)
  store.login('old', { id: 1, education_stage: 'senior', current_class_id: 11 })
  const changing = store.switchClass(12)
  store.login('new', { id: 2, education_stage: 'junior', current_class_id: 21 })
  request.resolve({ data: { ok: true } })
  await changing
  assert.deepEqual(sent(client), ['post /auth/class/switch'])
  assert.deepEqual(JSON.parse(client.requests[0].data), { class_id: 12 })
  assert.equal(store.currentClassId, 21)
  assert.equal(store.user.current_class_id, 21)
  assert.deepEqual(storedUser(client), {
    id: 2, education_stage: 'junior', current_class_id: 21,
  })
})
