import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref, computed } from 'vue'
import {
  EDUCATION_STAGES,
  educationProfileRedirect,
  hasCompleteEducationProfile,
  normalizeEducationStage,
  safeProfileRedirect,
} from '../src/utils/educationStage.js'

const authSource = readFileSync(new URL('../src/stores/auth.js', import.meta.url), 'utf8')
const routerSource = readFileSync(new URL('../src/router/index.js', import.meta.url), 'utf8')

function createStore(apiAuth = {}, stored = {}) {
  const data = new Map(Object.entries(stored))
  const storage = {
    getItem: key => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
    removeItem: key => data.delete(key),
  }
  const run = new Function('defineStore', 'ref', 'computed', 'api', 'localStorage',
    authSource.replace(/^import .*$/gm, '').replace('export const useAuthStore', 'const useAuthStore') + '\nreturn useAuthStore();')
  const store = run((_id, setup) => setup, ref, computed, { auth: apiAuth }, storage)
  return { store, data }
}

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
  const { store } = createStore({ me: async () => ({ user: { id: 2, education_stage: null, profile_complete: false } }) })
  store.classes.value = [{ id: 1 }]
  store.login('new-token', { id: 2, education_stage: 'senior', profile_complete: true })
  assert.equal(store.userVerified.value, false)
  assert.deepEqual(store.classes.value, [])
  await store.fetchMe()
  assert.equal(store.user.value.profile_complete, false)
  assert.equal(store.userVerified.value, true)
})

test('late /me from another account cannot overwrite the current teacher', async () => {
  const old = deferred()
  const current = deferred()
  let calls = 0
  const { store, data } = createStore({ me: () => (++calls === 1 ? old.promise : current.promise) })
  store.login('old', { id: 1 })
  const oldRequest = store.fetchMe()
  store.login('new', { id: 2 })
  const newRequest = store.fetchMe()
  current.resolve({ user: { id: 2, education_stage: 'junior', profile_complete: true } })
  await newRequest
  old.resolve({ user: { id: 1, education_stage: 'senior', profile_complete: true } })
  await oldRequest
  assert.equal(store.user.value.id, 2)
  assert.equal(JSON.parse(data.get('mathagent_user')).education_stage, 'junior')
})

test('profile update loads persisted /me and never reuses an earlier in-flight snapshot', async () => {
  const old = deferred()
  let calls = 0
  let submitted
  const { store } = createStore({
    me: () => ++calls === 1 ? old.promise : Promise.resolve({ user: { id: 1, education_stage: 'junior', profile_complete: true } }),
    updateMe: async payload => { submitted = payload; return { user: { education_stage: 'senior' } } },
  })
  store.login('same', { id: 1 })
  const stale = store.fetchMe()
  const saved = await store.updateProfile({ education_stage: 'junior' })
  old.resolve({ user: { id: 1, education_stage: null, profile_complete: false } })
  await stale
  assert.deepEqual(submitted, { education_stage: 'junior' })
  assert.equal(saved.education_stage, 'junior')
  assert.equal(store.user.value.education_stage, 'junior')
})

test('late profile save and logout do not replace or clear a newly signed-in account', async () => {
  const save = deferred()
  const logout = deferred()
  const { store } = createStore({ updateMe: () => save.promise, logout: () => logout.promise })
  store.login('old', { id: 1 })
  const oldSave = store.updateProfile({ education_stage: 'senior' })
  const oldLogout = store.logout()
  store.login('new', { id: 2 })
  save.resolve({ user: { id: 1 } })
  logout.resolve({ ok: true })
  assert.equal(await oldSave, null)
  await oldLogout
  assert.equal(store.token.value, 'new')
  assert.equal(store.user.value.id, 2)
})

test('late class selection cannot contaminate a new account and its profile scope', async () => {
  const request = deferred()
  const { store, data } = createStore({ switchClass: () => request.promise })
  store.login('old', { id: 1, education_stage: 'senior', current_class_id: 11 })
  const changing = store.switchClass(12)
  store.login('new', { id: 2, education_stage: 'junior', current_class_id: 21 })
  request.resolve({ ok: true })
  await changing
  assert.equal(store.currentClassId.value, 21)
  assert.deepEqual(JSON.parse(data.get('mathagent_user')), {
    id: 2, education_stage: 'junior', current_class_id: 21,
  })
})
