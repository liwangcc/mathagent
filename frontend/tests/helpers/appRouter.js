// A fresh router with the app's real routes and navigation guard, on an
// in-memory history, for each test (src/router/index.js is one shared
// instance; router-wiring.test.js checks it uses these same parts). Pages load
// as empty components naming their file (see helpers/srcModuleHooks.js), and
// the guard's auth store is whatever Pinia store is active.
//
//   setActivePinia(createPinia())
//   const router = createAppRouter()
//   await router.push('/upload')
//   router.currentRoute.value.name
import './srcModules.js'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { loadApiClient } from './apiClient.js'

const { routes } = await import('../../src/router/routes.js')
const { authorizeNavigation } = await import('../../src/router/guard.js')

// A guard that redirects back and forth would otherwise loop in microtasks
// forever, starving every timer, so the test would hang instead of failing.
const MAX_REDIRECTS = 20

export function createAppRouter() {
  const router = createRouter({ history: createMemoryHistory(), routes })
  let hops = 0
  router.beforeEach((to) => {
    if (++hops > MAX_REDIRECTS) throw new Error(`redirect loop at ${to.fullPath}`)
  })
  router.beforeEach(authorizeNavigation)
  router.afterEach(() => { hops = 0 })
  return router
}

const TEACHER = { id: 1, role: 'teacher', education_stage: 'senior', profile_complete: true }

// The router of a teacher whose login, restored from storage as auth.login()
// leaves it, /me verifies with a complete teaching profile: every teacher page
// is open to it.
export async function signedInTeacherRouter() {
  const client = await loadApiClient({
    localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(TEACHER) },
  })
  client.respond(() => ({ data: { user: TEACHER } }))
  setActivePinia(createPinia())
  return createAppRouter()
}

// The page file (relative to src/) each matched route record renders, once a
// navigation has resolved it.
export const renderedFiles = route => route.matched.map(record => record.components?.default?.__file)
