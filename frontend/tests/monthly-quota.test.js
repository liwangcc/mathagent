import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { loadApiClient } from './helpers/apiClient.js'

const upload = readFileSync(
  fileURLToPath(new URL('../src/views/Upload.vue', import.meta.url)),
  'utf8',
)

test('批改页显示每日400张原始学生图片额度并在创建批次后刷新', () => {
  assert.match(upload, /今日学生图片/)
  assert.match(upload, /dailyUsage\.current/)
  assert.match(upload, /dailyUsage\.remaining/)
  assert.match(upload, /const response = await api\.grade\.recognize[\s\S]*void loadDailyUsage\(\)/)
  assert.match(upload, /Promise\.all\(\[refreshAvailability\(\), loadDailyUsage\(\)\]\)/)
})

test('额度统计接口 GET /usage/stats 并把后端统计原样交给批改页', async () => {
  const client = await loadApiClient()
  const stats = { daily_images: { current: 12, limit: 400, remaining: 388, unlimited: false } }
  client.respond(() => ({ data: stats }))

  assert.deepEqual(await client.api.usage.stats(), stats)
  assert.equal(client.requests.length, 1)
  assert.equal(client.requests[0].method, 'get')
  assert.equal(client.requests[0].url, '/usage/stats')
})
