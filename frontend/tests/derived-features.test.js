import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'

const source = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const chatBar = source('../src/components/ChatBar.vue')
const layout = source('../src/views/Layout.vue')

function extract(text, startMarker, endMarker) {
  const start = text.indexOf(startMarker)
  const end = text.indexOf(endMarker, start)
  assert.ok(start >= 0 && end > start, `missing ${startMarker}`)
  return text.slice(start, end)
}

test('chat history items are clipped to the server limit before saving or sending', () => {
  const body = extract(chatBar, 'const MAX_HISTORY_CONTENT', 'function loadChatHistory')
  const clipHistory = new Function(`${body}\nreturn clipHistory`)()
  const [long, short] = clipHistory([
    { role: 'assistant', content: '长'.repeat(5000) },
    { role: 'user', content: '短' },
    null,
  ])
  assert.equal(long.content.length, 4000)
  assert.equal(short.content, '短')
  assert.match(chatBar, /localStorage\.setItem\(CHAT_HISTORY_KEY, JSON\.stringify\(trimmed\)\)/)
  assert.match(chatBar, /api\.chat\.send\(userMsg, clipHistory\(/)
})

test('model-backed tools wait longer than the generic 60 s client timeout', () => {
  assert.match(chatBar, /e\?\.code === 'ECONNABORTED'[\s\S]*最近生成文件/)
})

test('model-backed API calls override the generic 60 s client timeout', async () => {
  const client = await loadApiClient()
  const docx = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  client.respond(({ url }) => ({ data: url === '/vision-office' ? new Blob(['docx'], { type: docx }) : {} }))
  const { signal } = new AbortController()
  await client.api.classroom.importStudentsExcel('c1', new FormData())
  await client.api.classroom.importStudentsImage('c1', new FormData())
  await client.api.bank.photoRecognize(new FormData())
  await client.api.chat.send('本周作业', undefined, signal)
  const generated = await client.api.visionOffice.submit(new Blob(['png']), '做成表格', 'auto', signal)

  assert.deepEqual(client.requests.map(({ url, timeout }) => [url, timeout]), [
    ['/class/c1/students/import-excel', 60000],
    ['/class/c1/students/import-image', 300000],
    ['/bank/photo-import/recognize', 660000],
    ['/chat', 300000],
    ['/vision-office', 660000],
  ])
  const chat = client.requests[3]
  assert.deepEqual(JSON.parse(chat.data), { message: '本周作业', history: [] })
  assert.equal(chat.signal, signal)
  assert.equal(generated.blob.type, docx)
})

test('sidebar badge counts pending bank answers from the paginated total', () => {
  assert.doesNotMatch(layout, /api\.pending\.list\(\)/)
  assert.match(layout, /api\.bank\.pendingAnswers\(\{ page: 1, page_size: 1 \}/)
  assert.match(layout, /data\?\.total/)
})

test('student pickers page through the whole roster', () => {
  for (const view of ['Stats', 'GradeRecords', 'Exercise', 'Reports']) {
    assert.match(source(`../src/views/${view}.vue`), /api\.student\.listAll\(\)/, view)
  }
})

test('student roster listing walks pages of 100 from page 1 up to 30 pages', async () => {
  const rows = (page, count) => Array.from({ length: count }, (_, index) => ({ id: `${page}-${index}` }))
  const client = await loadApiClient()
  client.respond(({ params }) => ({ data: { items: rows(params.page, params.page === 3 ? 50 : 100), total: 250 } }))
  const roster = await client.api.student.listAll({ class_id: 'c1' })
  assert.deepEqual(client.requests.map(({ method, url, params }) => [method, url, params]), [
    ['get', '/student/list', { class_id: 'c1', page: 1, page_size: 100 }],
    ['get', '/student/list', { class_id: 'c1', page: 2, page_size: 100 }],
    ['get', '/student/list', { class_id: 'c1', page: 3, page_size: 100 }],
  ])
  assert.equal(roster.total, 250)
  assert.deepEqual(roster.students, roster.items)
  assert.equal(new Set(roster.items.map(({ id }) => id)).size, 250)

  const huge = await loadApiClient()
  huge.respond(({ params }) => ({ data: { items: rows(params.page, 100), total: 100000 } }))
  const capped = await huge.api.student.listAll()
  assert.deepEqual(huge.requests.map(({ params }) => params.page), Array.from({ length: 30 }, (_, index) => index + 1))
  assert.equal(capped.total, 3000)
})
