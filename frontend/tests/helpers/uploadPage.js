// The real 批改 page (frontend/src/views/Upload.vue) mounted for a test: a
// signed-in teacher with a class, talking to a healthy grading service.
//
//   import { liveSse, mountUpload, paperFile } from './helpers/uploadPage.js'
//   const live = liveSse(t)
//   const upload = await mountUpload(t, {
//     localStorage: { 'mathagent_orientation_batch_id:7': 'b1' },        // extra entries
//     responses: { 'GET /api/batch-grade/b1/orientation': { batch_id: 'b1', ... } },
//     streams: { 'POST /api/batch-grade/recognize': live.respond },
//   })
//   upload.page.setup.confirmedTemplateId = 'tpl-1'
//   await upload.choosePapers([paperFile('a.png')])
//   live.send('progress', { batch_id: 'b1' })
//   await upload.settle()
//   upload.fetches('POST /api/batch-grade/recognize')[0].init.body   // the FormData sent
//
// Routes are 'METHOD /api/path' (no query string), for the JSON API (axios)
// and for the streaming calls (fetch) alike.
//
// Options:
//   user, token     the teacher in localStorage (default: id 7, junior stage)
//   classId         auth.currentClassId as the top navigation would set it
//                   (default 1; null = no class selected)
//   localStorage    more storage entries, e.g. a saved batch pointer
//   props, path     Upload's props ({ anonymous: true }) and route ('/upload')
//   responses       JSON API answers by route: the data (status 200), or
//                   config => ({ status, data }) like client.respond. Upload's
//                   mount requests (availability, template availability, usage
//                   stats) get a healthy answer unless overridden; any other
//                   route answers 200 {}.
//   streams         fetch answers by route (recognize, resume, confirm,
//                   template recognize, ...): request => Response, or a list of
//                   [event, data] pairs served as a finished event stream.
//                   A route without an answer is a 404.
//   timers          true: node:test mock timers (setTimeout, setInterval) for
//                   the whole page, including what it schedules while mounting;
//                   advance them with upload.tick(ms). page.flush() waits on a
//                   real zero-delay timer, so use upload.settle() instead.
//   images          false: no canvas / createImageBitmap stand-in. By default
//                   the page can prepare chosen images the way a browser does
//                   (each one becomes `<name>.ocr-ready-v1.jpg`).
//   The global requestAnimationFrame its components call is always there.
//   ...the rest goes to mountComponent (messageBox, confirm, allowErrors).
//
// Returns { page, client, fetch, auth, user, ... }:
//   page              helpers/mountComponent.js's page (root, setup, win, router, ...)
//   client            helpers/apiClient.js's client (requests, messages, localStorage)
//   fetch             helpers/memoryFetch.js's backend (requests: { url, init })
//   auth              the page's auth store
//   requests(route)   JSON API requests sent to a route (axios configs), in order
//   fetches(route)    fetch requests sent to a route ({ url, init }), in order
//   answer(route, v)  change a JSON API answer from now on
//   stream(route, v)  change a fetch answer from now on
//   settle()          let requests, streams, timers due now and re-renders finish
//   tick(ms)          advance mocked timers by ms, then settle (needs `timers`)
//   button(text)      the innermost <button>/<el-button> whose text includes text
//   click(text)       click it, as a user would (a disabled button cannot be
//                     clicked), then settle; it does not wait for what the click
//                     started to finish (a handler's failure lands in page.errors)
//   choosePapers(files)  pick student files the way 选择文件 does, then settle
//   select(el, value), type(el, text)  change a v-model <select> / <input>
//   text()            the page's visible text, whitespace collapsed
//   storage()         localStorage entries, for mounting again after a refresh
//   unmount()         unmount and put fetch and the browser stand-ins back; call
//                     it before mounting again in the same test (a page refresh)
import './srcModules.js'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'
import { loadApiClient } from './apiClient.js'
import { memoryDocument, MElement } from './memoryDom.js'
import { installFetch, jsonResponse, sseResponse } from './memoryFetch.js'
import { mountComponent } from './mountComponent.js'

const { default: Upload } = await import('../../src/views/Upload.vue?sfc')
const { useAuthStore } = await import('../../src/stores/auth.js')

export const TEACHER = Object.freeze({ id: 7, name: '测试老师', education_stage: 'junior', current_class_id: 1 })

export const HEALTHY_SERVICE = Object.freeze({
  'GET /api/batch-grade/availability': { status: 'available', label: '可用', message: '' },
  'GET /api/batch-grade/template/availability': {
    status: 'available',
    service_status: 'available',
    label: '可用',
    message: '',
    registration_hardware: { status: 'ready' },
  },
  'GET /api/usage/stats': { daily_images: { current: 0, limit: 0, remaining: 0, unlimited: true } },
})

const routeOf = (method, url) => `${String(method || 'GET').toUpperCase()} ${String(url).split('?')[0]}`
const axiosRoute = config => routeOf(config.method, `/api${config.url}`)
const fetchRoute = request => routeOf(request.init?.method, request.url)

// A PNG file (header only: enough for the page to read its size) as a teacher
// would pick it.
export function paperFile(name = 'paper.png', { width = 1200, height = 1600 } = {}) {
  const bytes = new Uint8Array(24)
  bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52])
  const view = new DataView(bytes.buffer)
  view.setUint32(16, width, false)
  view.setUint32(20, height, false)
  return new File([bytes], name, { type: 'image/png' })
}

// FormData as { key: value }, a repeated key as a list of its values.
export function formFields(formData) {
  const fields = {}
  for (const [key, value] of formData.entries()) {
    if (!Object.hasOwn(fields, key)) fields[key] = value
    else fields[key] = [fields[key], value].flat()
  }
  return fields
}

// A fetch answer that stays open: the test sends events while the page reads
// them. Aborting the request ends the stream with an AbortError, as in a
// browser; a stream still open when the test ends is aborted the same way.
export function liveSse(t) {
  const encoder = new TextEncoder()
  const streams = []
  const abort = (stream) => {
    if (!stream.open) return
    stream.open = false
    stream.controller.error(new DOMException('The operation was aborted.', 'AbortError'))
  }
  const latest = () => {
    const stream = streams.at(-1)
    if (!stream?.open) throw new Error('liveSse: no open stream (has the page sent the request?)')
    return stream
  }
  t?.after(() => streams.forEach(abort))
  return {
    respond(request) {
      const stream = { open: true, request }
      const body = new ReadableStream({ start(controller) { stream.controller = controller } })
      request.init?.signal?.addEventListener('abort', () => abort(stream))
      streams.push(stream)
      return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
    },
    send(event, data) {
      latest().controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`))
    },
    // End the stream cleanly (without a terminal event the page sees a cut connection).
    close() {
      const stream = latest()
      stream.open = false
      stream.controller.close()
    },
    get count() { return streams.length },
    get open() { return streams.some(stream => stream.open) },
  }
}

// createImageBitmap and a 2D canvas, the two browser APIs image preparation
// uses; the canvas encodes every image as a small JPEG.
function installImageStandIns() {
  const previousBitmap = Object.getOwnPropertyDescriptor(globalThis, 'createImageBitmap')
  const previousCreate = memoryDocument.createElement
  globalThis.createImageBitmap = async (_file, options = {}) => ({
    width: options.resizeWidth || 1,
    height: options.resizeHeight || 1,
    close() {},
  })
  memoryDocument.createElement = (tag) => {
    if (tag !== 'canvas') return previousCreate(tag)
    return Object.assign(new MElement('canvas'), {
      width: 0,
      height: 0,
      getContext: () => ({
        fillStyle: '',
        fillRect() {}, drawImage() {}, translate() {}, rotate() {}, setTransform() {}, putImageData() {},
        getImageData: (_x, _y, width, height) => ({ data: new Uint8ClampedArray(Math.max(1, width * height) * 4) }),
      }),
      toBlob: (callback, type) => callback(new Blob(['jpeg'], { type })),
    })
  }
  return () => {
    memoryDocument.createElement = previousCreate
    if (previousBitmap) Object.defineProperty(globalThis, 'createImageBitmap', previousBitmap)
    else delete globalThis.createImageBitmap
  }
}

// Upload's own components (AnimatedNumber, OutcomeDonut) call the browser's
// global requestAnimationFrame, which helpers/memoryWindow.js only puts on
// `window`; a frame is a zero-delay timer here as well.
function installAnimationFrames() {
  const names = ['requestAnimationFrame', 'cancelAnimationFrame']
  const previous = Object.fromEntries(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]))
  globalThis.requestAnimationFrame = callback => setTimeout(() => callback(performance.now()), 0)
  globalThis.cancelAnimationFrame = id => clearTimeout(id)
  return () => {
    for (const [name, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  }
}

// Tests whose timers are mocked already (a second mount in one test reuses them).
const mockedTimers = new WeakSet()

const isDisabled = el => el.disabled === true || el.hasAttribute('disabled') || el.hasAttribute('loading')

export async function mountUpload(t, {
  user = TEACHER,
  token = 'teacher-token',
  classId = 1,
  localStorage = {},
  props = {},
  path = '/upload',
  responses = {},
  streams = {},
  timers = false,
  images = true,
  ...mountOptions
} = {}) {
  const client = await loadApiClient({
    localStorage: { mathagent_token: token, mathagent_user: JSON.stringify(user), ...localStorage },
  })
  const answers = { ...HEALTHY_SERVICE, ...responses }
  client.respond((config) => {
    const answer = answers[axiosRoute(config)]
    if (typeof answer === 'function') return answer(config)
    return answer === undefined ? { status: 200, data: {} } : { status: 200, data: answer }
  })
  const served = { ...streams }
  const fetch = installFetch(t, async (request) => {
    const answer = served[fetchRoute(request)]
    const value = typeof answer === 'function' ? await answer(request) : answer
    if (value === undefined) return jsonResponse({ detail: `no stream for ${fetchRoute(request)}` }, { status: 404 })
    return Array.isArray(value) ? sseResponse(value) : value
  })

  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.currentClassId = classId
  if (classId != null) auth.classes = [{ id: classId, class_name: `班级 ${classId}` }]

  const restoreImageStandIns = images ? installImageStandIns() : () => {}
  const restoreAnimationFrames = installAnimationFrames()
  const restoreStandIns = () => {
    restoreImageStandIns()
    restoreAnimationFrames()
  }
  if (timers && !mockedTimers.has(t)) {
    t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
    mockedTimers.add(t)
  }

  const settle = async (rounds = 12) => {
    for (let i = 0; i < rounds; i++) {
      await new Promise(resolve => setImmediate(resolve))
      if (timers) t.mock.timers.tick(0)
      await nextTick()
    }
  }

  let page
  try {
    const mounting = mountComponent(t, Upload, { path, props, pinia, ...mountOptions })
    if (timers) {
      // mountComponent settles on zero-delay timers; run them while it waits.
      let done = false
      mounting.then(() => { done = true }, () => { done = true })
      while (!done) {
        await new Promise(resolve => setImmediate(resolve))
        t.mock.timers.tick(0)
      }
    }
    page = await mounting
  } catch (error) {
    restoreStandIns()
    fetch.restore()
    throw error
  }

  let mounted = true
  const unmount = () => {
    if (!mounted) return
    mounted = false
    page.unmount()
    restoreStandIns()
    fetch.restore()
  }
  t?.after(unmount)

  // A user does not wait for what an event starts (a click can start a stream
  // that stays open): dispatch, record a handler's failure as Vue would, settle.
  const fire = async (el, type) => {
    el.dispatch(type).catch(err => page.errors.push({ err, info: `${type} handler` }))
    await settle()
  }

  const buttons = () => page.root.findAll(el => el.tagName === 'button' || el.tagName === 'el-button')
  const button = (text) => {
    const matches = buttons().filter(el => el.text.includes(text))
    return matches.find(el => !matches.some(other => other !== el && el.contains(other))) ?? null
  }

  await settle()
  return {
    page,
    client,
    fetch,
    auth,
    user,
    requests: route => client.requests.filter(config => axiosRoute(config) === route),
    fetches: route => fetch.requests.filter(request => fetchRoute(request) === route),
    answer(route, value) { answers[route] = value },
    stream(route, value) { served[route] = value },
    settle,
    async tick(ms) {
      if (!timers) throw new Error('upload.tick needs mountUpload(t, { timers: true })')
      t.mock.timers.tick(ms)
      await settle()
    },
    button,
    async click(text) {
      const target = button(text)
      if (!target) throw new Error(`no button "${text}" on the page`)
      if (isDisabled(target)) throw new Error(`button "${text}" is disabled`)
      await fire(target, 'click')
    },
    async choosePapers(files) {
      const input = page.root.byClass('grade-card')
        ?.find(el => el.tagName === 'input' && el.type === 'file' && !el.hasAttribute('webkitdirectory'))
      if (!input) throw new Error('no student file input on the page')
      if (isDisabled(input)) throw new Error('the student file input is disabled')
      input.files = files
      await fire(input, 'change')
    },
    // Pick an option of a <select> / type into an <input> bound with v-model.
    async select(el, value) {
      if (isDisabled(el)) throw new Error('the select is disabled')
      for (const option of el.options) option.selected = String(option._value ?? option.value) === String(value)
      await fire(el, 'change')
    },
    async type(el, text) {
      if (isDisabled(el)) throw new Error('the input is disabled')
      el.value = text
      await fire(el, 'input')
    },
    text: () => page.root.text,
    // localStorage as entries, to mount again after a refresh.
    storage: () => Object.fromEntries(Array.from({ length: client.localStorage.length }, (_, i) => {
      const key = client.localStorage.key(i)
      return [key, client.localStorage.getItem(key)]
    })),
    unmount,
  }
}
