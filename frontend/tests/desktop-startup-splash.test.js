import './helpers/v3bAppEntry.js'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { installBrowserGlobals } from './helpers/browser.js'
import { installDomGlobals, MElement, memoryDocument } from './helpers/memoryDom.js'
import { installMemoryWindow } from './helpers/memoryWindow.js'

const frontendFile = (relativePath) =>
  new URL(`../${relativePath}`, import.meta.url)

// The page index.html serves before the app script runs, holding the startup
// plate (unless `plate` is false). The app entry then runs `now` ms after
// navigation start, with or without reduced motion; its timers are the test's
// (t.mock.timers).
function coldStart(t, { now, reducedMotion = false, plate = true }) {
  installBrowserGlobals()
  const restoreDom = installDomGlobals()
  const win = installMemoryWindow()
  t.after(() => {
    win.restore()
    restoreDom()
  })
  if (reducedMotion) {
    win.matchMedia = query => ({
      matches: query === '(prefers-reduced-motion: reduce)', media: query, addEventListener() {}, removeEventListener() {},
    })
  }
  const body = new MElement('body')
  const screen = new MElement('div')
  screen.setAttribute('id', plate ? 'mathagent-startup' : 'mathagent-other')
  body.appendChild(screen)
  memoryDocument.roots.push(body)
  t.mock.method(performance, 'now', () => now)
  // Vue reports that it finds no #app to mount into: the memory document has
  // no selector engine, and the plate's exit does not depend on the mount.
  const warnings = t.mock.method(console, 'warn', () => {})
  const timers = t.mock.method(win, 'setTimeout')
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const state = () => ({ leaving: screen.classList.contains('is-leaving'), shown: screen.parentNode === body })
  return { state, timers, warnings: () => warnings.mock.calls.map(call => String(call.arguments[0])), tick: ms => t.mock.timers.tick(ms) }
}

test('desktop cold start grades a sample page on the startup plate before Vue mounts', async (t) => {
  const index = await readFile(frontendFile('index.html'), 'utf8')

  assert.match(index, /id="mathagent-startup"/)
  assert.match(index, /role="status" aria-live="polite" aria-label="MathAgent 正在启动"/)
  // The showcase specimen in miniature: phases, a scanned page, marks and the review stamp.
  assert.match(index, /<ol class="startup-phases"><li>识别<\/li><li>判分<\/li><li>复核<\/li><\/ol>/)
  assert.match(index, /class="sheet-scan"/)
  assert.match(index, /class="sheet-mark is-correct m1" pathLength="1"/)
  assert.match(index, /<strong>已复核<\/strong><span>可入档<\/span>/)
  assert.match(index, /animation: startup-stamp \.5s cubic-bezier\(\.16, 1, \.3, 1\) 1\.6s both/)
  assert.match(index, /正在准备批阅环境/)
  // Reduced motion shows the finished, stamped page at rest.
  assert.match(index, /prefers-reduced-motion: reduce\) \{\s*#mathagent-startup \*, #mathagent-startup \*::before \{ animation: none !important; \}/)

  // The app script runs 500 ms after navigation start: the plate stays until
  // its page is stamped, 2100 ms after navigation start, then fades for 400 ms
  // and is removed.
  const start = coldStart(t, { now: 500 })
  await import('../src/main.js?cold-start')
  // The entry ran through; its one complaint is the missing mount target.
  assert.deepEqual(start.warnings().map(message => /mount target selector "#app" returned null/.test(message)), [true])
  assert.deepEqual(start.state(), { leaving: false, shown: true })
  start.tick(1599)
  assert.deepEqual(start.state(), { leaving: false, shown: true })
  start.tick(1)
  assert.deepEqual(start.state(), { leaving: true, shown: true })
  start.tick(399)
  assert.deepEqual(start.state(), { leaving: true, shown: true })
  start.tick(1)
  assert.deepEqual(start.state(), { leaving: true, shown: false })
})

test('a slow cold start fades the startup plate at once instead of waiting longer', async (t) => {
  const start = coldStart(t, { now: 5000 })
  await import('../src/main.js?slow-start')
  assert.deepEqual(start.state(), { leaving: false, shown: true })
  start.tick(0)
  assert.deepEqual(start.state(), { leaving: true, shown: true })
  start.tick(400)
  assert.deepEqual(start.state(), { leaving: true, shown: false })
})

test('with reduced motion the static startup plate leaves as soon as the app has mounted', async (t) => {
  const start = coldStart(t, { now: 500, reducedMotion: true })
  await import('../src/main.js?reduced-motion')
  start.tick(0)
  assert.deepEqual(start.state(), { leaving: true, shown: true })
  start.tick(400)
  assert.deepEqual(start.state(), { leaving: true, shown: false })
})

test('a page without the startup plate starts without scheduling its exit', async (t) => {
  const start = coldStart(t, { now: 500, plate: false })
  await import('../src/main.js?no-plate')
  start.tick(5000)
  assert.equal(start.timers.mock.callCount(), 0)
  assert.deepEqual(start.state(), { leaving: false, shown: true })
})
