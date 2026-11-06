import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { loadApiClient } from './helpers/apiClient.js'
import { createAppRouter, renderedFiles, signedInTeacherRouter } from './helpers/appRouter.js'

// The app's own router instance; under the test hooks its history is in memory.
const { default: appRouter } = await import('../src/router/index.js')
const { routes } = await import('../src/router/routes.js')

test('the app router serves the route table behind the navigation guard', async () => {
  assert.equal(appRouter.options.routes, routes)
  const client = await loadApiClient()
  setActivePinia(createPinia())
  await appRouter.push('/upload?batch=1')
  const route = appRouter.currentRoute.value
  assert.deepEqual([route.name, route.query.redirect], ['login', '/upload?batch=1'])
  assert.deepEqual(client.requests, [])
})

test('every page is loaded only when its route is first opened', () => {
  const pages = createAppRouter().getRoutes().filter(record => record.components)
  assert.ok(pages.length > 20)
  for (const record of pages) {
    assert.equal(typeof record.components.default, 'function', record.path)
  }
})

test('the phone capture page is public and never verifies a stale teacher login', async () => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'expired-teacher-token' } })
  client.respond(() => ({ status: 401, data: { detail: '登录已过期，请重新登录' } }))
  setActivePinia(createPinia())
  const router = createAppRouter()
  await router.push('/capture/session-1')
  const route = router.currentRoute.value
  assert.deepEqual([route.name, route.params.sessionId], ['phone-capture', 'session-1'])
  assert.deepEqual(route.meta, { public: true, standalone: true, title: '手机拍摄' })
  assert.deepEqual(renderedFiles(route), ['views/PhoneCapture.vue'])
  assert.deepEqual(client.requests, [])
})

test('an unknown address leads a signed-in teacher home', async () => {
  const router = await signedInTeacherRouter()
  await router.push('/no-such-page')
  assert.equal(router.currentRoute.value.name, 'home')
  assert.deepEqual(renderedFiles(router.currentRoute.value), ['views/Layout.vue', 'views/Home.vue'])
})
