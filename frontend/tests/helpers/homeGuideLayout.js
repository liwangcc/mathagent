// Shared setup for the home page, layout and usage guide tests: a signed-in
// teacher, the usage tour's <Teleport to="body"> rendered into the memory
// document's body, a desktop (Tauri) bridge answering native commands in
// memory, and a few queries over what those components render.
//
//   const client = await signedInTeacher({ storage: { disclaimer_agreed: 'true' } })
//   const body = teleportToBody(t)
//   const desktop = desktopBridge({ runtime_info: { runtime: 'desktop' } })
//   const page = await mountComponent(t, Layout, { path: '/upload', plugins: [desktop.plugin] })
//   tourCard().text, desktop.commands()
import './srcModules.js'
import assert from 'node:assert/strict'
import { setActivePinia } from 'pinia'
import { loadApiClient } from './apiClient.js'
import { MElement, memoryDocument, nodeOps } from './memoryDom.js'

const { useAuthStore } = await import('../../src/stores/auth.js')

// A teacher whose teaching profile is complete: every teacher page is open.
export const TEACHER = Object.freeze({
  id: 7,
  username: 'lin',
  name: '林老师',
  role: 'teacher',
  education_stage: 'senior',
  profile_complete: true,
})

// The login a teacher's browser keeps (what auth.login() stores), plus any
// other localStorage entries, on a fresh in-memory API client.
export function signedInTeacher({ user = TEACHER, token = 'teacher-token', storage = {} } = {}) {
  const session = token ? { mathagent_token: token, mathagent_user: JSON.stringify(user) } : {}
  return loadApiClient({ localStorage: { ...session, ...storage } })
}

// The app has one auth store; the desktop binding manager (a module-level
// singleton, created by the first component that needs it) keeps reading the
// store it was created with. Tests that mount the binding UI more than once
// share one Pinia across their mounts and sign in on it, so the manager and
// every mounted component read the same session, as in the app. Signing in
// with no token signs the shared store out.
export async function signIn(pinia, { user = TEACHER, token = 'teacher-token', storage = {} } = {}) {
  const client = await signedInTeacher({ user, token, storage })
  setActivePinia(pinia)
  const auth = useAuthStore(pinia)
  if (token) auth.login(token, user)
  else auth.clearSession()
  return { client, auth }
}

// GuidedTour teleports its overlay to `body`. For this test the memory
// renderer finds `body` (the memory document's body), and the tour's
// `instanceof HTMLElement` check of the element to refocus sees memory
// elements. Call before mounting.
export function teleportToBody(t) {
  t.mock.method(nodeOps, 'querySelector', selector => (selector === 'body' ? memoryDocument.body : null))
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'HTMLElement')
  Object.defineProperty(globalThis, 'HTMLElement', { value: MElement, configurable: true, writable: true })
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'HTMLElement', previous)
    else delete globalThis.HTMLElement
  })
  return memoryDocument.body
}

// The usage tour's step card, while the tour is shown.
export function tourCard() {
  return memoryDocument.body.find(el => el.classList.contains('guided-tour__card'))
}

// A key pressed while the page has focus: the document's keydown listeners
// (capture listeners included) receive it.
export function pressKey(key) {
  const event = {
    type: 'keydown',
    key,
    shiftKey: false,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true },
    stopPropagation() {},
  }
  for (const listener of [...(memoryDocument.listeners.get('keydown') ?? [])]) listener(event)
  return event
}

// An element of the page the tour returns focus to; the memory DOM has no
// isConnected, so this one says whether it is still in the mounted tree.
export function focusedOpener(root) {
  const opener = new MElement('button')
  Object.defineProperty(opener, 'isConnected', { get: () => root.contains(opener) })
  root.appendChild(opener)
  opener.focus()
  return opener
}

// The desktop client's native bridge (window.__TAURI_INTERNALS__.invoke), as
// a plugin for mountComponent: every command is recorded; `replies` maps a
// command to its answer (a value, or a function of the args that may throw).
// A command without a reply fails, as an unknown native command does.
export function desktopBridge(replies = {}) {
  const calls = []
  async function invoke(command, args) {
    calls.push({ command, args })
    if (!Object.hasOwn(replies, command)) throw new Error(`unknown native command ${command}`)
    const reply = replies[command]
    return typeof reply === 'function' ? reply(args) : reply
  }
  return {
    calls,
    replies,
    commands: () => calls.map(call => call.command),
    plugin: { install() { window.__TAURI_INTERNALS__ = { invoke } } },
  }
}

// Desktop worker states as the native bridge reports them.
export const cpuWorker = (overrides = {}) => ({
  computeBackend: 'windows_cpu_opencv',
  localProcessingAvailable: true,
  paused: false,
  cpuAcceleration: { workerCount: 4, reasonCodes: [] },
  handoff: { configured: true, running: true, connected: true, lastErrorCode: null },
  ...overrides,
})

// The el-dialog rendered with this title.
export function dialogTitled(root, title) {
  return root.find(el => el.tagName === 'el-dialog' && el.getAttribute('title') === title)
}

export const isOpen = dialog => dialog?.getAttribute('data-open') === 'true'

// Assertions about rendered elements that report a failure without printing
// the element: its parent links reach the whole memory tree.
export function assertAbsent(element, message = 'expected no such element') {
  assert.ok(element == null, `${message} (found <${element?.tagName}> "${element?.text?.slice(0, 80)}")`)
}

export function assertSame(actual, expected, message = 'expected the same element') {
  assert.ok(actual === expected, `${message} (got <${actual?.tagName}> "${actual?.text?.slice(0, 80)}")`)
}
