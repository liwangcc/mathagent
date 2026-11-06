// What the stage / report / busy-state tests share when they mount a teacher
// page (helpers/mountComponent.js): a signed-in teacher whose teaching profile
// the test controls, the real API client on an in-memory backend that can hold
// a request until the test answers it, and the browser parts those pages use
// that the memory window and DOM leave out (clipboard, blob downloads,
// animation frames, v-loading's mask, focus moves between sibling controls,
// a teleport target), each for one test's duration.
//
//   const { client, auth, pinia } = await signIn({ stage: 'junior' })
//   const backend = holdingBackend(client, config => (route(config) === 'get /class/1/students' ? HOLD : {}))
//   const page = await mountView(t, ClassTable, { path: '/class/1/students', pinia })
//   backend.take(config => route(config) === 'get /class/1/students').ok({ students: [...] })
import './srcModules.js'
import { createPinia, setActivePinia } from 'pinia'
import { loadApiClient } from './apiClient.js'
import { MElement, MNode, memoryDocument, nodeOps } from './memoryDom.js'
import { flush, mountComponent } from './mountComponent.js'

const { useAuthStore } = await import('../../src/stores/auth.js')

const CLASS_ID = 1

export function teacher(overrides = {}) {
  return {
    id: 1,
    name: '王老师',
    role: 'teacher',
    education_stage: 'junior',
    profile_complete: true,
    current_class_id: CLASS_ID,
    ...overrides,
  }
}

// A teacher signed in as auth.login() leaves it (token and user in storage),
// with the class list loaded and `classId` current; `localStorage` adds
// entries (a page's saved draft pointers, say). Returns the API client
// (helpers/apiClient.js) and the real auth store, on the Pinia to mount the
// page with.
export async function signIn({ stage = 'junior', user = {}, token = 'teacher-token', classId = CLASS_ID, className = '初二（1）班', localStorage = {} } = {}) {
  const profile = teacher({ education_stage: stage, current_class_id: classId, ...user })
  const client = await loadApiClient({
    localStorage: { ...localStorage, mathagent_token: token, mathagent_user: JSON.stringify(profile) },
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.classes = classId ? [{ id: classId, class_name: className, student_count: 1 }] : []
  auth.currentClassId = classId
  return { client, auth, pinia, user: profile }
}

// `method url` of an axios request config, e.g. 'get /class/1/students'.
export const route = config => `${config.method} ${config.url}`

// The JSON body the client sent (axios has serialized it by then).
export function bodyOf(config) {
  if (typeof config.data !== 'string') return config.data ?? null
  try {
    return JSON.parse(config.data)
  } catch {
    return config.data
  }
}

// The teaching stage a request asks for, in its query or its JSON body.
export const stageOf = config => config.params?.education_stage ?? bodyOf(config)?.education_stage

export const tokenOf = config => String(config.headers?.Authorization ?? '').replace(/^Bearer /, '')

// Answers each request with what `reply(config)` returns ({ data } or
// { status, data }, as client.respond), except that HOLD keeps it open:
// it is added to `held` as { config, ok(data), fail(status, detail) } and
// stays pending until the test answers it.
export const HOLD = Symbol('hold the request')
export function holdingBackend(client, reply = () => ({})) {
  const held = []
  client.respond((config) => {
    const answer = reply(config)
    if (answer !== HOLD) return answer
    return new Promise((resolve) => {
      held.push({
        config,
        ok: (data = {}) => resolve({ data }),
        fail: (status = 500, detail = '服务器开小差了') => resolve({ status, data: { detail } }),
      })
    })
  })
  return {
    held,
    take(predicate) {
      const index = held.findIndex(entry => predicate(entry.config))
      if (index < 0) throw new Error('no held request matches')
      return held.splice(index, 1)[0]
    },
  }
}

// v-loading as the page sees it: element-plus covers the element with a mask
// while the value is true; here the element says so in data-loading. (The
// element-plus stand-ins register a v-loading that does nothing; this gives
// that directive its hooks.)
const showLoading = (el, { value }) => { el.setAttribute('data-loading', value ? 'true' : 'false') }
const visibleLoading = {
  install(app) {
    const hooks = { mounted: showLoading, updated: showLoading }
    const registered = app.directive('loading')
    if (registered) Object.assign(registered, hooks)
    else app.directive('loading', hooks)
  },
}

export async function mountView(t, View, { pinia, plugins = [], ...options } = {}) {
  return mountComponent(t, View, { ...options, pinia, plugins: [visibleLoading, ...plugins] })
}

export const isLoading = el => el?.getAttribute('data-loading') === 'true'

// The el-button showing `text` (the innermost one).
export function button(page, text) {
  const found = page.root.byText('el-button', text)
  if (!found) throw new Error(`no el-button says ${text}`)
  return found
}

export const isBlocked = el => el.hasAttribute('disabled') || el.hasAttribute('loading')

// Picks the <el-dropdown-item> showing `text`: its dropdown emits `command`
// with the item's command, as element-plus does when the item is clicked. A
// disabled dropdown does not open, so nothing happens, unless `force` (to call
// the page's command handler the way a stale or scripted click would).
export function chooseCommand(page, text, { force = false } = {}) {
  const item = page.root.byText('el-dropdown-item', text)
  if (!item) throw new Error(`no el-dropdown-item says ${text}`)
  let dropdown = item.parentNode
  while (dropdown && dropdown.tagName !== 'el-dropdown') dropdown = dropdown.parentNode
  if (!dropdown) throw new Error(`${text} is not in an el-dropdown`)
  if (!force && dropdown.hasAttribute('disabled')) return Promise.resolve()
  return Promise.resolve(dropdown.handlers.command?.(item.getAttribute('command')))
}

// Emits update:modelValue (and change, as element-plus inputs do after it) on
// a v-model'd element-plus stand-in.
export async function choose(el, value) {
  await el.handlers['update:modelValue']?.(value)
  await el.handlers.change?.(value)
}

// Installs a stand-in once per test, however often a test asks for it (a
// test mounting several pages in turn), and restores the original after it.
const installed = new WeakMap()
function oncePerTest(t, name, install) {
  const done = installed.get(t) ?? installed.set(t, new Map()).get(t)
  if (!done.has(name)) done.set(name, install())
  return done.get(name)
}

// navigator.clipboard for the test: what pages copied, in order.
export function installClipboard(t) {
  return oncePerTest(t, 'clipboard', () => clipboard(t))
}

function clipboard(t) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  const writes = []
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      ...globalThis.navigator,
      clipboard: {
        async writeText(text) { writes.push(String(text)) },
      },
    },
  })
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'navigator', previous)
    else delete globalThis.navigator
  })
  return writes
}

// Blob downloads (utils/download.js) a mounted page starts: each as
// { filename, blob }. Timers the page sets on window after this (the object
// URL's revocation, 3 s later) are cleared when the page unmounts, so none
// outlives the test or keeps its process alive. A test may capture the
// downloads of several pages in turn: each link goes to the page that was
// being captured when the link was created.
export function captureDownloads(t, page) {
  const downloads = []
  const urls = new Map()
  downloadLinks(t, page.win.document).sink = { downloads, urls }
  let next = 0
  page.win.URL = {
    createObjectURL(blob) {
      const url = `blob:memory/${++next}`
      urls.set(url, blob)
      return url
    },
    revokeObjectURL(url) { urls.delete(url) },
  }
  const schedule = page.win.setTimeout
  const timers = new Set()
  page.win.setTimeout = (callback, ...args) => {
    const timer = schedule((...values) => { timers.delete(timer); callback(...values) }, ...args)
    timer?.unref?.()
    timers.add(timer)
    return timer
  }
  const unmount = page.unmount
  page.unmount = () => {
    for (const timer of timers) clearTimeout(timer)
    timers.clear()
    unmount()
  }
  t.after(() => page.unmount())
  return downloads
}

// The document's createElement, wrapped once per test (however many pages it
// captures) so that a clicked <a> reports its download to the current sink;
// the document's own createElement is put back after the test.
function downloadLinks(t, doc) {
  return oncePerTest(t, 'download links', () => {
    const links = { sink: null }
    const created = doc.createElement
    doc.createElement = (tag, ...rest) => {
      const el = created.call(doc, tag, ...rest)
      const sink = links.sink
      if (tag === 'a' && sink) {
        el.addEventListener('click', () => sink.downloads.push({ filename: el.download, blob: sink.urls.get(el.href) }))
      }
      return el
    }
    t.after(() => { doc.createElement = created })
    return links
  })
}

// requestAnimationFrame / cancelAnimationFrame as bare globals (AnimatedNumber
// calls them so): frames are counted and never run.
export function installAnimationFrames(t) {
  oncePerTest(t, 'animation frames', () => animationFrames(t))
}

function animationFrames(t) {
  const previous = {
    requestAnimationFrame: Object.getOwnPropertyDescriptor(globalThis, 'requestAnimationFrame'),
    cancelAnimationFrame: Object.getOwnPropertyDescriptor(globalThis, 'cancelAnimationFrame'),
  }
  let frames = 0
  globalThis.requestAnimationFrame = () => ++frames
  globalThis.cancelAnimationFrame = () => {}
  t.after(() => {
    for (const [name, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  })
}

// Element.parentElement and Element.querySelectorAll / querySelector on the
// memory DOM for the test's duration, for pages that move focus between
// sibling controls (a radio group's arrow keys). Selectors: tag names, .class,
// [attr] and [attr="value"], compounds and lists of these; anything else
// throws rather than matching the wrong elements.
export function installElementQueries(t) {
  oncePerTest(t, 'element queries', () => elementQueries(t))
}

function elementQueries(t) {
  const added = []
  const define = (target, name, descriptor) => {
    if (Object.hasOwn(target, name)) return
    Object.defineProperty(target, name, { configurable: true, ...descriptor })
    added.push([target, name])
  }
  define(MNode.prototype, 'parentElement', {
    get() { return this.parentNode instanceof MElement ? this.parentNode : null },
  })
  define(MElement.prototype, 'querySelectorAll', {
    value(selector) { return this.findAll(matcher(selector)) },
  })
  define(MElement.prototype, 'querySelector', {
    value(selector) { return this.find(matcher(selector)) },
  })
  t.after(() => { for (const [target, name] of added) delete target[name] })
}

// <Teleport to="body"> (GuidedTour, inside the layout's usage guide) mounts
// into the memory document's body for the test's duration; the renderer
// otherwise finds no teleport target. Set it up before mounting.
export function installTeleportToBody(t) {
  oncePerTest(t, 'teleport to body', () => {
    const original = nodeOps.querySelector
    nodeOps.querySelector = selector => (selector === 'body' ? memoryDocument.body : original(selector))
    t.after(() => { nodeOps.querySelector = original })
  })
}

const SIMPLE_SELECTOR = /^([a-z][\w-]*)?((?:\.[\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/i
function matcher(selector) {
  const alternatives = String(selector).split(',').map((part) => {
    const match = SIMPLE_SELECTOR.exec(part.trim())
    if (!match) throw new Error(`memory DOM: unsupported selector ${part.trim()}`)
    const [, tag, rest] = match
    const conditions = [...rest.matchAll(/\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g)]
    return el => (!tag || el.tagName === tag.toLowerCase()) && conditions.every(([, className, attr, value]) => (
      className ? el.classList.contains(className) : value === undefined ? el.hasAttribute(attr) : el.getAttribute(attr) === value
    ))
  })
  return el => alternatives.some(test => test(el))
}

export { flush }
