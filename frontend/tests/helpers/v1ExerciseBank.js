// Shared setup of the exercise and question-bank page tests
// (exercise-*.test.js, bank-*.test.js, knowledge-classification.test.js),
// which mount the real pages with helpers/mountComponent.js:
//
//   const client = await signedInTeacher({ user: { id: 7, education_stage: 'junior' } })
//   const backend = serve(client, { 'get /teacher/chapters': config => [{ chapter: '函数' }] })
//   const page = await mountComponent(t, Bank, { path: '/bank', plugins: [pageStandIns] })
//   backend.calls('get /teacher/chapters')   // the axios configs of those requests
//
// * signedInTeacher(): a teacher whose login is in localStorage, as auth.login()
//   leaves it, so the page's auth store starts signed in (helpers/apiClient.js);
//   authOf(page) is that store;
// * serve(): answers each request by `${method} ${url}`; a handler returns the
//   response data (or a promise of it: the request stays pending until it
//   settles), or failure(status, data) for an error status;
// * pick(), choose(), click(), select(), options(): what a teacher does with
//   the element-plus stand-ins (helpers/elementPlusStubs.js) and what they show;
// * exerciseServer(), changeProfile(): the server side of the exercise page
//   and the teacher changing the teaching profile it answers for;
// * pageStandIns: what helpers/elementPlusStubs.js leaves empty and these pages
//   rely on: <el-upload> (the page calls its clearFiles(), a test chooses files
//   through it and the page hears of them by its on-change, as from the real
//   one) and the v-loading mask (shown as data-loading on the element).
import './srcModules.js'
import { defineComponent, h } from 'vue'
import { loadApiClient } from './apiClient.js'
import { installFetch } from './memoryFetch.js'
import { flush } from './mountComponent.js'

const { useAuthStore } = await import('../../src/stores/auth.js')

export function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

export async function signedInTeacher({ user, token = `teacher-${user.id}-token`, localStorage = {}, sessionStorage = {} }) {
  return loadApiClient({
    localStorage: { mathagent_token: token, mathagent_user: JSON.stringify(user), ...localStorage },
    sessionStorage,
  })
}

// The auth store of a mounted page.
export const authOf = page => useAuthStore(page.pinia)

class Failure {
  constructor(status, data) {
    this.status = status
    this.data = data
  }
}

export const failure = (status, data = {}) => new Failure(status, data)

export function serve(client, routes) {
  client.respond(async (config) => {
    const key = `${config.method} ${config.url}`
    const route = routes[key]
    if (!route) return { status: 404, data: { detail: `no test route for ${key}` } }
    const result = await route(config)
    return result instanceof Failure ? { status: result.status, data: result.data } : { data: result }
  })
  return {
    calls: key => client.requests.filter(config => `${config.method} ${config.url}` === key),
    keys: () => client.requests.map(config => `${config.method} ${config.url}`),
  }
}

// The JSON body of a request the API client sent, and the login it carried.
export const body = config => JSON.parse(config.data)
export const tokenOf = config => String(config.headers?.Authorization ?? '').replace(/^Bearer /, '')

// The el-select showing `placeholder`, the labels of its options, the form
// item labelled `label`.
export const select = (root, placeholder) => (
  root.find(el => el.tagName === 'el-select' && el.attributes.placeholder === placeholder)
)
export const options = el => el.byTag('el-option').map(option => option.attributes.label)
export const formItem = (root, label) => root.find(el => el.tagName === 'el-form-item' && el.attributes.label === label)

// Choosing a value in an element-plus control: it emits update:modelValue,
// then change with the same value.
export async function pick(el, value) {
  assert(el, 'no such control')
  el.handlers['update:modelValue']?.(value)
  await el.handlers.change?.(value)
  await flush()
}

// The `tag` element showing exactly `text`.
export const labelled = (root, tag, text) => root.find(el => el.tagName === tag && el.text === text)

// Picking the radio button labelled `label` in its group.
export async function choose(root, label) {
  const radio = labelled(root, 'el-radio-button', label)
  assert(radio, `no radio button ${label}`)
  await pick(radio.parentNode, radio._value)
}

// Clicking the `tag` element labelled `text` (exactly, or else containing it),
// once its handlers have settled.
export async function click(root, tag, text) {
  const el = labelled(root, tag, text) ?? root.byText(tag, text)
  assert(el, `no ${tag} ${text}`)
  const results = await el.dispatch('click')
  await flush()
  return results
}

function assert(value, message) {
  if (!value) throw new Error(message)
}

const renderSlots = slots => Object.entries(slots).map(([name, slot]) => (
  name === 'default' ? slot() : h('el-slot', { name }, slot())
))

const ElUpload = defineComponent({
  name: 'ElUpload',
  inheritAttrs: false,
  props: {
    onChange: Function,
    onRemove: Function,
    fileList: { type: Array, default: () => [] },
  },
  setup(props, { attrs, slots, expose }) {
    // The page passes its own file list back as file-list.
    expose({ clearFiles() {} })
    return () => h('el-upload', {
      ...attrs,
      'data-files': String(props.fileList.length),
      // Choosing files: el-upload reports each new file with the whole list.
      onSelect: (files) => {
        const list = [...props.fileList]
        for (const raw of files) {
          const file = { name: raw.name, size: raw.size, status: 'ready', uid: list.length + 1, raw }
          list.push(file)
          props.onChange?.(file, list)
        }
      },
    }, renderSlots(slots))
  },
})

const loadingMask = {
  mounted(el, { value }) { el.dataset.loading = String(Boolean(value)) },
  updated(el, { value }) { el.dataset.loading = String(Boolean(value)) },
}

// Installed after helpers/elementPlusStubs.js, in place of its empty ones
// (dropped from the app's registry first: re-registering a name only warns).
export const pageStandIns = {
  install(app) {
    delete app._context.components.ElUpload
    delete app._context.directives.loading
    app.component('ElUpload', ElUpload)
    app.directive('loading', loadingMask)
  },
}

// Choosing image files in the el-upload whose text includes `text`.
export async function chooseFiles(root, text, files) {
  const upload = root.byText('el-upload', text)
  assert(upload, `no upload ${text}`)
  upload.handlers.select(files)
  await flush()
}

// The server side of the exercise page. `profiles` holds each account's
// teaching profile by login token: the catalog and the student list answer
// for the account a request carries the login of, as the server does (the
// page sends no stage of its own). `chapters(profile)` / `students(profile)`
// give the reply (or a promise of it); generation answers with
// `generate(payload)`; the exports and the downloads they lead to are recorded.
export function exerciseServer(t, client, { profiles, chapters, students, generate } = {}) {
  const seen = { chapters: [], students: [], generate: [], exports: [] }
  const profileOf = config => ({ ...profiles[tokenOf(config)] })
  const backend = serve(client, {
    'get /teacher/chapters': (config) => {
      const profile = profileOf(config)
      seen.chapters.push({ profile, params: config.params })
      return chapters ? chapters(profile) : [{ chapter: `${profile.education_stage}-chapter` }]
    },
    'get /student/list': async (config) => {
      const profile = profileOf(config)
      seen.students.push(profile)
      return { items: await (students ? students(profile) : [{ id: profile.id, name: '虚拟学生' }]) }
    },
    'post /exercises/generate': (config) => {
      seen.generate.push(body(config))
      return generate(body(config))
    },
    'post /exercise/export-docx': (config) => {
      seen.exports.push({ kind: 'student-and-teacher', data: body(config) })
      return { paper_url: '/api/mock-paper', answer_url: '/api/mock-teacher' }
    },
    'post /exercise/export-answer-sheet-docx': (config) => {
      seen.exports.push({ kind: 'answer-sheet', data: body(config) })
      return { download_url: '/api/mock-card' }
    },
  })
  const files = installFetch(t, () => new Response('synthetic document'))
  return {
    ...backend,
    seen,
    files,
    downloads: () => files.requests.map(request => request.url),
  }
}

// The teacher changes the teaching profile: the server keeps it, and the
// page's auth store takes the new copy, as auth.fetchMe() does.
export function changeProfile(page, profiles, changes) {
  const auth = authOf(page)
  Object.assign(profiles[auth.token], changes)
  auth.user = { ...profiles[auth.token] }
}
