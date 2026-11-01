import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const chatBar = source('../src/components/ChatBar.vue')
const api = source('../src/api/index.js')
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
  assert.match(api, /importStudentsImage: .*timeout: MODEL_TOOL_TIMEOUT_MS/)
  assert.match(api, /photoRecognize: .*timeout: GRADING_OPERATION_TIMEOUT_MS/)
  assert.match(api, /send: \(message, history = \[\], signal\) => .*timeout: MODEL_TOOL_TIMEOUT_MS/)
  assert.match(extract(api, "url: '/vision-office'", 'transformResponse'), /timeout: GRADING_OPERATION_TIMEOUT_MS/)
  assert.match(chatBar, /e\?\.code === 'ECONNABORTED'[\s\S]*最近生成文件/)
})

test('sidebar badge counts pending bank answers from the paginated total', () => {
  assert.doesNotMatch(layout, /api\.pending\.list\(\)/)
  assert.match(layout, /api\.bank\.pendingAnswers\(\{ page: 1, page_size: 1 \}/)
  assert.match(layout, /data\?\.total/)
})

test('student pickers page through the whole roster', () => {
  const body = extract(api, 'listAll: async', 'classDetail:')
  assert.match(body, /for \(let page = 1; page <= STUDENT_PAGE_LIMIT; page \+= 1\)/)
  assert.match(body, /page_size: STUDENT_PAGE_SIZE/)
  for (const view of ['Stats', 'GradeRecords', 'Exercise', 'Reports']) {
    assert.match(source(`../src/views/${view}.vue`), /api\.student\.listAll\(\)/, view)
  }
})
