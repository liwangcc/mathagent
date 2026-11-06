import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import axios from 'axios'
import { createPinia, setActivePinia } from 'pinia'
import { loadApiClient } from './helpers/apiClient.js'
import { signedInTeacher, teleportToBody } from './helpers/homeGuideLayout.js'
import { mountComponent } from './helpers/mountComponent.js'

const { useAuthStore } = await import('../src/stores/auth.js')

const CHAT_HISTORY_KEY = 'mathagent_chat_history'
const savedHistory = client => JSON.parse(client.localStorage.getItem(CHAT_HISTORY_KEY))

async function mountChatBar(t, props = {}) {
  const { default: ChatBar } = await import('../src/components/ChatBar.vue?sfc')
  return mountComponent(t, ChatBar, { props })
}

async function sendChat(page, text) {
  const input = page.root.find(el => el.tagName === 'el-input')
  input.handlers['update:modelValue'](text)
  await page.flush()
  await page.root.find(el => el.tagName === 'el-input').handlers.keyup({ key: 'Enter', ctrlKey: false, shiftKey: false, altKey: false, metaKey: false })
  await page.flush()
}

test('chat history items are clipped to the server limit before saving or sending', async (t) => {
  const client = await signedInTeacher({
    storage: {
      [CHAT_HISTORY_KEY]: JSON.stringify([
        { role: 'assistant', content: '长'.repeat(5000) },
        { role: 'user', content: '短' },
        null,
      ]),
    },
  })
  client.respond(({ url }) => (url === '/chat' ? { data: { response: '好的' } } : { data: {} }))
  const page = await mountChatBar(t, { variant: 'bar' })

  // The restored reply is the clipped one.
  const reply = page.root.byClass('chat-response-text').text
  assert.equal(reply.length, 4000)
  assert.ok(reply.endsWith('…'))

  await sendChat(page, '本周作业')
  const chat = client.requests.find(({ url }) => url === '/chat')
  const sent = JSON.parse(chat.data)
  assert.equal(sent.message, '本周作业')
  const [long, short, ...rest] = sent.history
  assert.equal(long.content.length, 4000, 'the long saved reply is clipped before sending')
  assert.equal(short.content, '短')
  assert.deepEqual(rest, [], 'invalid history items are dropped')

  // What is saved afterwards stays within the limit too.
  const saved = savedHistory(client)
  assert.deepEqual(saved.map(item => item.role), ['assistant', 'user', 'user', 'assistant'])
  assert.ok(saved.every(item => item.content.length <= 4000))
  assert.deepEqual(saved.slice(1).map(item => item.content), ['短', '本周作业', '好的'])
})

test('chat history the server returns is clipped and capped before it is saved', async (t) => {
  const client = await signedInTeacher()
  const history = Array.from({ length: 24 }, (_, index) => ({ role: index % 2 ? 'assistant' : 'user', content: `第${index}条` }))
  history[23] = { role: 'assistant', content: '答'.repeat(4500) }
  client.respond(({ url }) => (url === '/chat' ? { data: { response: '已整理', history } } : { data: {} }))
  const page = await mountChatBar(t, { variant: 'bar' })
  await sendChat(page, '整理一下')
  const saved = savedHistory(client)
  assert.equal(saved.length, 20, 'only the latest 20 items are kept')
  assert.equal(saved[0].content, '第4条')
  assert.equal(saved.at(-1).content.length, 4000)

  // The next message sends that history clipped as well.
  await sendChat(page, '继续')
  const [, second] = client.requests.filter(({ url }) => url === '/chat')
  const sent = JSON.parse(second.data).history
  assert.equal(sent.length, 24)
  assert.equal(sent.at(-1).content.length, 4000)
  assert.ok(sent.every(item => item.content.length <= 4000))
})

async function pickOfficeFile(page) {
  const input = page.root.find(el => el.tagName === 'input' && el.getAttribute('type') === 'file')
  const file = new File(['docx bytes'], '期末通知模板.docx', { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })
  await input.dispatch('change', { target: { files: [file], value: 'C:\\fakepath\\期末通知模板.docx' } })
  await page.flush()
  assert.equal(page.root.byClass('vo-file-name').text, '期末通知模板.docx')
}

async function submitVision(page) {
  await page.root.byText('el-button', '生成并下载').dispatch('click')
  await page.flush()
}

test('model-backed tools wait longer than the generic 60 s client timeout', async (t) => {
  const client = await signedInTeacher()
  client.respond((config) => {
    if (config.url === '/vision-office') {
      throw new axios.AxiosError(`timeout of ${config.timeout}ms exceeded`, 'ECONNABORTED', config)
    }
    return { data: {} }
  })
  const page = await mountChatBar(t, { variant: 'home' })
  await pickOfficeFile(page)
  await submitVision(page)
  const request = client.requests.find(({ url }) => url === '/vision-office')
  assert.equal(request.timeout, 660000)
  assert.equal(request.data.get('file').name, '期末通知模板.docx')
  // A timeout is not a failure: the server keeps generating.
  assert.deepEqual(client.messages.map(({ type }) => type), ['warning'])
  assert.match(client.messages[0].message, /最近生成文件/)
  assert.match(client.messages[0].message, /避免重复提交/)
  assert.equal(page.root.byText('el-button', '生成并下载').getAttribute('loading'), null)
})

test('a failed or cancelled office generation is reported as failed or not at all', async (t) => {
  const client = await signedInTeacher()
  let failure = 'error'
  client.respond((config) => {
    if (config.url !== '/vision-office') return { data: {} }
    if (failure === 'cancel') throw new axios.CanceledError('canceled', 'ERR_CANCELED', config)
    return { status: 500, data: new Blob([JSON.stringify({ detail: '模板解析失败' })], { type: 'application/json' }) }
  })
  const page = await mountChatBar(t, { variant: 'home' })
  await pickOfficeFile(page)
  await submitVision(page)
  assert.deepEqual(client.messages.map(({ type }) => type), ['error'])
  assert.doesNotMatch(client.messages[0].message, /最近生成文件/)

  failure = 'cancel'
  client.messages.length = 0
  await submitVision(page)
  assert.deepEqual(client.messages, [], 'a cancelled request shows nothing')
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

async function mountLayoutWithPending(t, total) {
  teleportToBody(t)
  const client = await signedInTeacher({ storage: { disclaimer_agreed: 'true', 'mathagent_usage_guide_v3:7': 'true' } })
  client.respond(({ url }) => {
    if (url === '/bank/pending-answers') return { data: { items: [{ id: 'a1' }], total } }
    if (url === '/auth/classes') return { data: { classes: [], current_class_id: null } }
    return { data: {} }
  })
  const { default: Layout } = await import('../src/views/Layout.vue?sfc')
  const page = await mountComponent(t, Layout, { path: '/upload' })
  return { client, page }
}

test('sidebar badge counts pending bank answers from the paginated total', async (t) => {
  const { client, page } = await mountLayoutWithPending(t, 37)
  const pendingRequests = client.requests.filter(({ url }) => url === '/bank/pending-answers')
  assert.deepEqual(pendingRequests.map(({ method, params }) => [method, params]), [['get', { page: 1, page_size: 1 }]])
  assert.equal(client.requests.some(({ url }) => url === '/teacher/pending'), false, 'the old pending list is not read')
  const badge = page.root.byClass('nav-count')
  assert.equal(badge.text, '37', 'the server total, not the length of the one-row page')
  assert.equal(badge.getAttribute('aria-label'), '37 条待入库')

  // The badge follows changes announced by the pending-answers page.
  client.respond(({ url }) => (url === '/bank/pending-answers' ? { data: { items: [], total: 120 } } : { data: {} }))
  for (const listener of page.win.listeners.get('mathagent:pending-answers-changed')) await listener()
  await page.flush()
  assert.equal(page.root.byClass('nav-count').text, '99+')
})

// One roster of 150 students, served 100 per page as /student/list does.
function rosterBackend(client) {
  const roster = Array.from({ length: 150 }, (_, index) => ({ id: index + 1, name: `学生${index + 1}`, student_no: String(index + 1), class_id: 3 }))
  client.respond(({ url, params }) => {
    if (url === '/student/list') {
      const start = (params.page - 1) * params.page_size
      return { data: { items: roster.slice(start, start + params.page_size), total: roster.length } }
    }
    return { data: { items: [], total: 0 } }
  })
}

const pickers = {
  Stats: {
    path: '/stats',
    load: () => import('../src/views/Stats.vue?sfc'),
  },
  GradeRecords: {
    path: '/grade-records',
    load: () => import('../src/views/GradeRecords.vue?sfc'),
  },
  Exercise: {
    path: '/exercise',
    load: () => import('../src/views/Exercise.vue?sfc'),
    // The student picker belongs to personal weakness practice.
    open: page => page.root.find(el => el.tagName === 'el-radio-group').handlers['update:modelValue']('mistake'),
  },
  Reports: {
    path: '/reports',
    load: () => import('../src/views/Reports.vue?sfc'),
  },
}

test('student pickers page through the whole roster', async (t) => {
  for (const [view, { path, load, open }] of Object.entries(pickers)) {
    await t.test(view, async (t) => {
      const client = await signedInTeacher()
      rosterBackend(client)
      const pinia = createPinia()
      setActivePinia(pinia)
      useAuthStore().currentClassId = 3
      const { default: View } = await load()
      const page = await mountComponent(t, View, { path, pinia })
      await open?.(page)
      await page.flush()
      const pages = client.requests.filter(({ url }) => url === '/student/list').map(({ params }) => params)
      assert.deepEqual(pages, [{ page: 1, page_size: 100 }, { page: 2, page_size: 100 }], view)
      const options = page.root.findAll(el => el.tagName === 'el-option' && /学生\d+/.test(el.getAttribute('label') ?? ''))
      assert.equal(options.length, 150, `${view} offers every student of the roster`)
      assert.ok(options.some(option => option.getAttribute('label').includes('学生150')), view)
    })
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
