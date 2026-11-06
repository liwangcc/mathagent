// The answer-sheet page (src/views/AnswerSheet.vue) mounted for real against
// an in-memory backend, for the answer-sheet test files.
//
//   const sheet = await openAnswerSheet(t, { session: sessionFixture({ status: 'draft' }) })
//   await sheet.click('上传材料')
//   sheet.writes()            // [method, url] of every non-GET request
//   sheet.root                // the rendered tree (helpers/mountComponent.js)
//
// Every HTTP call goes through the page's real API client (helpers/apiClient.js)
// and every stream through an in-memory fetch (helpers/memoryFetch.js). The
// backend answers what the page asks for while it loads (availability,
// template history, paper templates, batches, sessions, the class list) from
// the options; `respond(config)` answers any request first, and returning
// undefined falls through to those defaults.
import './srcModules.js'
import { nextTick } from 'vue'
import { loadApiClient } from './apiClient.js'
import { installFetch } from './memoryFetch.js'
import { memoryDocument, nodeOps } from './memoryDom.js'
import { mountComponent } from './mountComponent.js'

const { default: AnswerSheet } = await import('../../src/views/AnswerSheet.vue?sfc')

export { AnswerSheet }

// Let pending promises and re-renders settle. The answer-sheet page, its
// components and the stand-ins schedule no zero-delay timers, so event-loop
// turns are enough; setTimeout(0) would add its timer granularity (about
// 15 ms a turn on Windows) to every wait.
export async function flush(rounds = 5) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise(resolve => setImmediate(resolve))
    await nextTick()
  }
}
export const SESSION_KEY = 'mathagent_answer_sheet_session_id'
export const PAPER = 'PT-AAAA-BBBB-CCCC-DDDD'
export const SHEET = 'AS-1111-2222-3333-4444'
export const CLASSES = { classes: [{ id: 10, class_name: '一班' }], current_class_id: 10 }
export const TEACHER = { id: 1, role: 'teacher', education_stage: 'senior', profile_complete: true }

export const body = config => (typeof config.data === 'string' ? JSON.parse(config.data) : config.data)

// A session as GET /answer-sheet/sessions/:id returns it.
export function sessionFixture(overrides = {}) {
  return {
    session_id: 's1',
    teacher_id: 1,
    class_id: 10,
    status: 'draft',
    answer_source: 'embedded',
    source_exam_pages: [],
    answer_pages: [],
    source_question_drafts: [],
    answer_spec_snapshot: [],
    answer_sheet_pages: [],
    answer_regions: [],
    identity_regions: [],
    student_pages: [],
    grouping_decisions: [],
    ...overrides,
  }
}

// A session of a template-code batch: answers come from a frozen paper template.
export function batchFixture(overrides = {}) {
  return sessionFixture({
    session_id: 's-new',
    status: 'answers_confirmed',
    batch_contract_version: 'answer-sheet-batch-v2',
    batch_label: '',
    answer_sheet_template_code: '',
    paper_template_code: PAPER,
    answer_spec_snapshot: [{ question_no: '1', question_type: 'choice', choice_mode: 'single', standard_answer: 'A' }],
    ...overrides,
  })
}

// URL.createObjectURL / revokeObjectURL, recorded once per test.
const objectUrls = new WeakMap()
function recordObjectUrls(t) {
  if (objectUrls.has(t)) return objectUrls.get(t)
  const record = { created: [], revoked: [], urls: () => record.created.map(item => item.url) }
  t.mock.method(URL, 'createObjectURL', (blob) => {
    const url = `blob:image-${record.created.length + 1}`
    record.created.push({ url, blob })
    return url
  })
  t.mock.method(URL, 'revokeObjectURL', (url) => { record.revoked.push(url) })
  objectUrls.set(t, record)
  return record
}

export async function openAnswerSheet(t, {
  session = null,
  sessions = [],
  saved = session?.session_id,
  path = '/answer-sheet',
  storage = {},
  availability = { status: 'available' },
  history = [],
  paperTemplates = [],
  paperSources = [],
  batches = [],
  classes = CLASSES,
  respond = () => undefined,
  stream = () => new Response('', { status: 200 }),
  allowErrors = false,
} = {}) {
  const client = await loadApiClient({
    localStorage: {
      mathagent_token: 'teacher-token',
      mathagent_user: JSON.stringify(TEACHER),
      ...(saved ? { [SESSION_KEY]: saved } : {}),
      ...storage,
    },
  })
  const known = new Map([session, ...sessions].filter(Boolean).map(item => [item.session_id, item]))
  const images = recordObjectUrls(t)
  const answer = (value, config) => (typeof value === 'function' ? value(config) : value)
  client.respond(async (config) => {
    const custom = await respond(config)
    if (custom !== undefined) return custom
    const { method, url } = config
    if (method === 'get') {
      if (url === '/answer-sheet/availability') return { data: await answer(availability, config) }
      if (url === '/answer-sheet/templates/history') return { data: { templates: answer(history, config) } }
      if (url === '/answer-sheet/paper-templates') return { data: { templates: answer(paperTemplates, config) } }
      if (url === '/answer-sheet/paper-templates/sources') return { data: { sources: answer(paperSources, config) } }
      if (url === '/answer-sheet/sessions') return { data: { batches: answer(batches, config) } }
      if (url === '/auth/classes') return { data: classes }
      if (/^\/class\/[^/]+\/students$/.test(url)) return { data: [] }
      if (/\/image$/.test(url)) return { data: new Blob(['png'], { type: 'image/png' }) }
      const match = /^\/answer-sheet\/sessions\/([^/]+)$/.exec(url)
      if (match) {
        const found = known.get(decodeURIComponent(match[1]))
        return found ? { data: found } : { status: 404, data: { detail: '会话不存在' } }
      }
    }
    return { data: {} }
  })
  const backend = installFetch(t, stream)
  const page = await mountComponent(t, AnswerSheet, { path, allowErrors })

  const elButtons = () => page.root.findAll(el => el.tagName === 'el-button')
  const sheet = {
    page,
    client,
    backend,
    sessions: known,
    images,
    get root() { return page.root },
    get setup() { return page.setup },
    flush: () => flush(),
    // Unmount now (a test that mounts the page again first closes it).
    close() {
      page.unmount()
      backend.restore()
    },
    requests: () => client.requests.map(config => [config.method, config.url]),
    writes: () => client.requests.filter(config => config.method !== 'get').map(config => [config.method, config.url]),
    request: (method, url) => client.requests.find(config => config.method === method && config.url === url),
    count: (method, url) => client.requests.filter(config => config.method === method && config.url === url).length,
    streams: () => backend.requests.map(request => [request.init.method, request.url]),
    messages: () => client.messages.map(item => [item.type, item.message]),
    button: text => elButtons().find(el => el.text === text) ?? null,
    // Click an el-button and wait for what its handler does.
    async click(text) {
      const { done } = await sheet.press(text)
      await done
      await flush()
    },
    // Click an el-button without waiting for its handler (a stream it reads
    // stays open until the test ends it): gives { done }, settling with the handler.
    press(text) {
      const button = sheet.button(text)
      if (!button) throw new Error(`no el-button "${text}" in ${page.html()}`)
      // What the button's own handler returns (dispatch also runs ancestors').
      const done = button.dispatch('click').then(results => results[0])
      done.catch(() => {}) // the test awaits it; a rejection is not unhandled meanwhile
      return flush().then(() => ({ done }))
    },
    serviceTag: () => page.root.byClass('service-state'),
    serviceNotice: () => page.root.byClass('service-notice'),
    alerts: () => page.root.byTag('el-alert').map(el => el.getAttribute('title')),
    stageButtons: () => page.root.findAll(el => el.tagName === 'button' && el.classList.contains('stage-item')),
    activeStage: () => sheet.stageButtons().findIndex(el => el.getAttribute('aria-current') === 'step'),
    section: heading => page.root.find(el => el.tagName === 'section' && el.byText('h2', heading)),
    picker: title => page.root.find(el => el.classList.contains('file-picker') && el.byText('strong', title)),
    pickers: () => page.root.allByClass('file-picker'),
    // The component whose rendered root element is `el` (a file picker, ...).
    owner: el => componentRendering(page.vm, el),
  }
  return sheet
}

function componentRendering(instance, el) {
  let found = instance.subTree?.el === el ? instance : null
  const visit = (vnode) => {
    if (!vnode || typeof vnode !== 'object') return
    if (vnode.component) {
      if (vnode.component.subTree?.el === el) found = vnode.component
      visit(vnode.component.subTree)
      return
    }
    if (Array.isArray(vnode.children)) vnode.children.forEach(visit)
  }
  visit(instance.subTree)
  return found
}

// A picker emitting the processed files it hands the page once a teacher
// picked them (what its own preprocessing emits).
export async function pickFiles(sheet, title, files) {
  const picker = sheet.picker(title)
  if (!picker) throw new Error(`no file picker "${title}"`)
  sheet.owner(picker).emit('update:modelValue', files)
  await flush()
}

// Choose files in a picker's file input, as the browser's file dialog does.
export async function chooseFiles(input, files) {
  input.files = files
  input.value = 'C:\\fakepath\\chosen'
  await input.dispatch('change')
  await flush(10)
}

// A small PNG whose header names its size (40×30), as a scanner writes it.
export function pngFile(name, { path, lastModified = 1 } = {}) {
  const bytes = new Uint8Array(33)
  bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52, 0, 0, 0, 40, 0, 0, 0, 30])
  const file = new File([bytes], name, { type: 'image/png', lastModified })
  if (path) Object.defineProperty(file, 'webkitRelativePath', { value: path, configurable: true })
  return file
}

// The browser's image decoder and canvas, which the page's upload
// preprocessing (utils/uploadValidation.js) draws every picked image through:
// decoding yields a bitmap of the requested size, the canvas encodes a JPEG.
export function installImageDecoding(t) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'createImageBitmap')
  globalThis.createImageBitmap = async (_file, options = {}) => ({
    width: options.resizeWidth ?? 40,
    height: options.resizeHeight ?? 30,
    close() {},
  })
  const createElement = memoryDocument.createElement
  memoryDocument.createElement = tag => (tag === 'canvas' ? canvas() : createElement(tag))
  t.after(() => {
    memoryDocument.createElement = createElement
    if (previous) Object.defineProperty(globalThis, 'createImageBitmap', previous)
    else delete globalThis.createImageBitmap
  })
}

function canvas() {
  const context = {
    fillStyle: '',
    fillRect() {},
    drawImage() {},
    translate() {},
    rotate() {},
    setTransform() {},
    getImageData: (_x, _y, width, height) => ({ data: new Uint8ClampedArray(width * height * 4) }),
    putImageData() {},
  }
  return {
    width: 0,
    height: 0,
    getContext: () => context,
    toBlob: (callback, type) => callback(new Blob(['jpeg'], { type })),
  }
}

// A server-sent event stream the test writes to while the page reads it.
// Aborting the request errors the body, as a browser's fetch does.
export function liveStreams() {
  const encoder = new TextEncoder()
  const opened = []
  return {
    opened,
    get last() { return opened.at(-1) },
    open(request) {
      let controller
      const responseBody = new ReadableStream({ start(c) { controller = c } })
      let finished = false
      const finish = (fn) => {
        if (finished) return
        finished = true
        fn()
      }
      request.init.signal?.addEventListener('abort', () => {
        finish(() => controller.error(new DOMException('The operation was aborted.', 'AbortError')))
      })
      opened.push({
        request,
        async send(event, data) {
          controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
          await flush()
        },
        async end() {
          finish(() => controller.close())
          await flush()
        },
      })
      return new Response(responseBody, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
    },
  }
}

// Teleport to="body" (components/GuidedTour.vue, inside the layout) finds the
// in-memory document body while a test runs.
export function teleportToBody(t) {
  const select = nodeOps.querySelector
  nodeOps.querySelector = selector => (selector === 'body' ? memoryDocument.body : select(selector))
  t.after(() => { nodeOps.querySelector = select })
}
