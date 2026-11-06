import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/srcModules.js'
import { defineComponent, h, ref } from 'vue'
import { MElement, memoryDocument } from './helpers/memoryDom.js'
import { mountComponent } from './helpers/mountComponent.js'

const { default: GradingQuickDock } = await import('../src/components/GradingQuickDock.vue?sfc')

// A grading page: the dock beside the results list (#grading-start-anchor)
// and the summary (#grading-summary-anchor), both inside the page's <main>.
const anchorsShown = ref(true)
const GradingPage = defineComponent({
  setup() {
    return () => h('div', { class: 'grading-page' }, [
      h('main', { class: 'grading-main' }, anchorsShown.value
        ? [h('div', { id: 'grading-start-anchor' }), h('div', { id: 'grading-summary-anchor' })]
        : []),
      h(GradingQuickDock),
    ])
  },
})

async function mountPage(t, { anchors = true } = {}) {
  anchorsShown.value = anchors
  return mountComponent(t, GradingPage)
}

// memoryDom has no layout engine, so the test gives the mounted page the
// geometry a browser would: which element scrolls (the nested <main>, or the
// document), how tall it is, where the anchors sit, and whether the teacher
// asked for reduced motion. Scrolls the dock asks for are recorded and
// applied, clamped like a browser clamps them. From here on the test owns the
// clock (the dock re-pins its scroll after a delay); a page that is laid out
// is not flushed again.
function layOut(t, page, { nested = true, reducedMotion = false } = {}) {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const calls = []
  const html = new MElement('html')
  const body = new MElement('body')
  const main = page.root.byClass('grading-main')
  const overflow = new Map()
  const metrics = (el, { scrollTop, scrollHeight, clientHeight }) => {
    el.scrollTop = scrollTop
    el.scrollHeight = scrollHeight
    Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
  }
  for (const el of [page.root, ...page.root.walk()]) el.parentElement = el.parentNode instanceof MElement ? el.parentNode : body
  body.parentElement = html
  html.parentElement = null
  metrics(html, { scrollTop: 0, scrollHeight: nested ? 800 : 4000, clientHeight: 800 })
  const scroller = nested ? main : html
  if (nested) {
    metrics(main, { scrollTop: 300, scrollHeight: 4000, clientHeight: 640 })
    overflow.set(main, 'auto')
  }
  const viewportTop = nested ? 64 : 0
  const scrollTo = (el) => (options) => {
    calls.push({ ...options })
    el.scrollTop = Math.max(0, Math.min(options.top, el.scrollHeight - el.clientHeight))
  }
  main.getBoundingClientRect = () => ({ top: viewportTop })
  main.scrollTo = scrollTo(main)
  const place = (id, offset, height) => {
    memoryDocument.getElementById(id).getBoundingClientRect = () => ({
      top: viewportTop + offset - scroller.scrollTop,
      bottom: viewportTop + offset + height - scroller.scrollTop,
    })
  }
  place('grading-start-anchor', 1000, 600)
  place('grading-summary-anchor', 1700, 100)
  Object.assign(page.win, {
    matchMedia: query => ({ matches: reducedMotion && query === '(prefers-reduced-motion: reduce)' }),
    getComputedStyle: el => ({ overflowY: overflow.get(el) || 'visible' }),
    scrollTo: scrollTo(html),
  })
  // The page's document: the same rendered tree, with its root scroller.
  globalThis.document = Object.create(memoryDocument, {
    body: { value: body },
    documentElement: { value: html },
    scrollingElement: { value: html },
  })
  return { scroller, root: html, calls }
}

const dockButton = (page, label) => page.root.find(el => el.tagName === 'button' && el.getAttribute('aria-label') === label)
const press = (page, label) => dockButton(page, label).dispatch('click')

for (const nested of [true, false]) {
  const layout = nested ? 'nested main' : 'document'

  test(`start reaches the ${layout} bottom, not the results-list bottom`, async (t) => {
    const page = await mountPage(t)
    const h = layOut(t, page, { nested })
    await press(page, '开始批阅')
    assert.equal(h.scroller.scrollTop, h.scroller.scrollHeight - h.scroller.clientHeight)
    assert.equal(h.calls[0].behavior, 'smooth')
    if (nested) assert.equal(h.root.scrollTop, 0, 'Only the actual scrolling container moves')
  })

  test(`start re-pins to the ${layout} bottom after content grows`, async (t) => {
    const page = await mountPage(t)
    const h = layOut(t, page, { nested })
    await press(page, '开始批阅')
    const bottom = () => h.scroller.scrollHeight - h.scroller.clientHeight
    // Two re-pins, after the smooth scroll (350 ms) and once layout settles (750 ms).
    for (const wait of [350, 400]) {
      const before = h.scroller.scrollTop
      h.scroller.scrollHeight += 500
      t.mock.timers.tick(wait - 1)
      assert.equal(h.scroller.scrollTop, before, `no re-pin before ${wait} ms`)
      t.mock.timers.tick(1)
      assert.equal(h.scroller.scrollTop, bottom())
      assert.equal(h.calls.at(-1).behavior, 'auto')
    }
    const settled = h.scroller.scrollTop
    const scrolls = h.calls.length
    h.scroller.scrollHeight += 500
    t.mock.timers.tick(10_000)
    assert.equal(h.scroller.scrollTop, settled, 'no third re-pin')
    assert.equal(h.calls.length, scrolls)
  })

  test(`top and summary keep their ${layout} destinations`, async (t) => {
    const page = await mountPage(t)
    const h = layOut(t, page, { nested })
    h.scroller.scrollTop = h.scroller.scrollHeight - h.scroller.clientHeight
    await press(page, '批阅总结')
    assert.equal(h.scroller.scrollTop, 1700 - 12)
    t.mock.timers.tick(750)
    assert.equal(h.scroller.scrollTop, 1700 - 12)
    await press(page, '返回顶部')
    t.mock.timers.tick(750)
    assert.equal(h.scroller.scrollTop, 0)
  })
}

test('the dock keeps all three labels and respects reduced motion', async (t) => {
  const page = await mountPage(t)
  const nav = page.root.find(el => el.tagName === 'nav')
  assert.equal(nav.getAttribute('aria-label'), '批阅快捷导航')
  assert.deepEqual(nav.byTag('button').map(button => [button.getAttribute('aria-label'), button.byClass('dock-tip').text]), [
    ['返回顶部', '返回顶部'],
    ['批阅总结', '批阅总结'],
    ['开始批阅', '开始批阅'],
  ])
  const h = layOut(t, page, { reducedMotion: true })
  await press(page, '开始批阅')
  assert.equal(h.calls[0].behavior, 'auto')
})

test('the dock shows up once the grading anchors render, and only then', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  const page = await mountPage(t, { anchors: false })
  assert.equal(page.root.find(el => el.tagName === 'nav'), null, 'returning to the top alone is no dock')
  anchorsShown.value = true
  await page.flush()
  assert.equal(page.root.find(el => el.tagName === 'nav'), null, 'the dock checks for anchors on its own schedule')
  t.mock.timers.tick(1500)
  await page.flush()
  assert.deepEqual(page.root.find(el => el.tagName === 'nav').byTag('button').map(button => button.getAttribute('aria-label')), ['返回顶部', '批阅总结', '开始批阅'])
})
