import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const upload = readFileSync(
  fileURLToPath(new URL('../src/views/Upload.vue', import.meta.url)),
  'utf8',
)
const api = readFileSync(
  fileURLToPath(new URL('../src/api/index.js', import.meta.url)),
  'utf8',
)

test('批改页显示每日400张原始学生图片额度并在创建批次后刷新', () => {
  assert.match(api, /stats: \(\) => http\.get\('\/usage\/stats'\)/)
  assert.match(upload, /今日学生图片/)
  assert.match(upload, /dailyUsage\.current/)
  assert.match(upload, /dailyUsage\.remaining/)
  assert.match(upload, /const response = await api\.grade\.recognize[\s\S]*void loadDailyUsage\(\)/)
  assert.match(upload, /Promise\.all\(\[refreshAvailability\(\), loadDailyUsage\(\)\]\)/)
})
