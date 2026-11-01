import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const apiSource = readFileSync(new URL('../src/api/index.js', import.meta.url), 'utf8')
const authSource = readFileSync(new URL('../src/stores/auth.js', import.meta.url), 'utf8')
const settingsSource = readFileSync(new URL('../src/views/Settings.vue', import.meta.url), 'utf8')

test('desktop API origin covers axios and native fetch without changing the web default', () => {
  assert.match(apiSource, /import\.meta\.env\.VITE_API_ORIGIN/)
  assert.match(apiSource, /baseURL:\s*apiBaseUrl/)
  assert.match(apiSource, /fetch\(absoluteApiUrl\(url\)/)
  assert.match(apiSource, /const apiBaseUrl = apiOrigin \? `\$\{apiOrigin\}\/api` : '\/api'/)
})

test('invalid serialized tokens never create a logged-in session', () => {
  assert.match(authSource, /function normalizeToken\(/)
  assert.match(authSource, /\['undefined', 'null'\]\.includes/)
  assert.match(authSource, /const token = ref\(loadStoredToken\(\)\)/)
  assert.match(authSource, /if \(!normalizedToken\) throw new Error\('登录响应缺少有效凭据'\)/)
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
