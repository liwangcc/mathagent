import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../src/components/GradingQuickDock.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
  .replace(/^import .* from 'vue'\s*$/m, '')
const loadDock = new Function('window', 'document', 'computed', 'onMounted', 'onUnmounted', 'ref', `${script}\nreturn { go, probe, items }`)

function fixture({ nested = true, reducedMotion = false } = {}) {
  const calls = [], timers = []
  const root = { scrollTop: 0, scrollHeight: nested ? 800 : 4000, clientHeight: 800, parentElement: null }
  const body = { parentElement: root }
  const scroller = nested
    ? { scrollTop: 300, scrollHeight: 4000, clientHeight: 640, parentElement: body, overflowY: 'auto' }
    : root
  const viewportTop = nested ? 64 : 0
  scroller.getBoundingClientRect = () => ({ top: viewportTop })
  const scrollTo = (options) => {
    calls.push({ ...options })
    scroller.scrollTop = Math.max(0, Math.min(options.top, scroller.scrollHeight - scroller.clientHeight))
  }
  scroller.scrollTo = scrollTo
  const anchor = (offset, height) => ({
    parentElement: scroller,
    getBoundingClientRect: () => ({
      top: viewportTop + offset - scroller.scrollTop,
      bottom: viewportTop + offset + height - scroller.scrollTop,
    }),
  })
  const anchors = {
    'grading-start-anchor': anchor(1000, 600),
    'grading-summary-anchor': anchor(1700, 100),
  }
  const window = {
    matchMedia: () => ({ matches: reducedMotion }),
    getComputedStyle: node => ({ overflowY: node.overflowY || 'visible' }),
    scrollTo,
    setTimeout: (callback, delay) => { timers.push({ callback, delay }) },
  }
  const document = { body, scrollingElement: root, getElementById: id => anchors[id] || null }
  const dock = loadDock(window, document, getter => ({ get value() { return getter() } }), () => {}, () => {}, value => ({ value }))
  dock.probe()
  return { ...dock, scroller, root, calls, timers }
}

for (const nested of [true, false]) {
  const layout = nested ? 'nested main' : 'document'

  test(`start reaches the ${layout} bottom, not the results-list bottom`, () => {
    const h = fixture({ nested })
    h.go({ key: 'start' })
    assert.equal(h.scroller.scrollTop, h.scroller.scrollHeight - h.scroller.clientHeight)
    assert.equal(h.calls[0].behavior, 'smooth')
    if (nested) assert.equal(h.root.scrollTop, 0, 'Only the actual scrolling container moves')
  })

  test(`start re-pins to the ${layout} bottom after content grows`, () => {
    const h = fixture({ nested })
    h.go({ key: 'start' })
    assert.deepEqual(h.timers.map(timer => timer.delay), [350, 750])
    for (const timer of h.timers) {
      h.scroller.scrollHeight += 500
      timer.callback()
      assert.equal(h.scroller.scrollTop, h.scroller.scrollHeight - h.scroller.clientHeight)
      assert.equal(h.calls.at(-1).behavior, 'auto')
    }
  })

  test(`top and summary keep their ${layout} destinations`, () => {
    const h = fixture({ nested })
    h.scroller.scrollTop = h.scroller.scrollHeight - h.scroller.clientHeight
    h.go({ key: 'summary' })
    assert.equal(h.scroller.scrollTop, 1700 - 12)
    for (const timer of h.timers.splice(0)) timer.callback()
    assert.equal(h.scroller.scrollTop, 1700 - 12)
    h.go({ key: 'top' })
    for (const timer of h.timers.splice(0)) timer.callback()
    assert.equal(h.scroller.scrollTop, 0)
  })
}

test('the dock keeps all three labels and respects reduced motion', () => {
  const h = fixture({ reducedMotion: true })
  assert.deepEqual(h.items.value, [
    { key: 'top', label: '返回顶部' },
    { key: 'summary', label: '批阅总结' },
    { key: 'start', label: '开始批阅' },
  ])
  h.go({ key: 'start' })
  assert.equal(h.calls[0].behavior, 'auto')
})
