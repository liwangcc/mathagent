import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { createSSRApp, defineComponent, h, nextTick, reactive, shallowRef, toRaw } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { MElement, memoryDocument } from './helpers/memoryDom.js'
import { mountComponent } from './helpers/mountComponent.js'

// The real editor, compiled as the development build compiles it (a template
// or script error fails this import), with the geometry helpers it exports.
const {
  default: Editor,
  emptyDrawingReference,
  changedDrawingReference,
  editableDrawingReference,
  drawingReferenceError,
  normalizedDrawingPoint,
  arcThroughPoints,
  splineThroughPoints,
  drawingReferenceRegionWarning,
} = await import('../src/components/DrawingReferenceEditor.vue?sfc')

const fixture = () => ({ ...emptyDrawingReference(), comparison_mode: 'fixed', variants: [{ strokes: [{ kind: 'polyline', points: [[100, 200], [300, 200]] }] }], teacher_confirmed: true, reference_hash: 'server-only' })
const SELECT = '选择／拖动端点'
const pointer = (x, y, pointerId = 7) => ({ button: 0, clientX: x, clientY: y, pointerId })

function instanceOf(vnode, type) {
  if (!vnode) return null
  if (vnode.component) return vnode.component.type === type ? vnode.component : instanceOf(vnode.component.subTree, type)
  for (const child of Array.isArray(vnode.children) ? vnode.children : []) {
    const found = instanceOf(child, type)
    if (found) return found
  }
  return null
}

// A disabled control, or one inside a disabled <fieldset>, ignores clicks.
function disabledControl(el) {
  for (let node = el; node; node = node.parentNode) {
    if (['button', 'fieldset'].includes(node.tagName) && node.disabled) return true
  }
  return false
}

// The editor as a template page uses it: a parent passes its props, listens to
// its events and reaches the methods it exposes through a template ref. The
// template image then loads at 1000×800 pixels and the canvas is laid out at
// 1000×800 CSS pixels from the viewport origin (memoryDom has no layout), so a
// pointer at (x, y) lands on normalized (x, y × 1.25).
async function editor(t, value = fixture(), { onEditRequest, slots = {}, wrap = false, load = true, ...extra } = {}) {
  const events = []
  const imageEvents = []
  const selectionEvents = []
  const editRequests = []
  const props = reactive({ modelValue: value, reference: null, imageUrl: 'synthetic.png', drawingRegions: [[50, 50, 900, 900]], ...extra })
  const editorRef = shallowRef(null)
  const Parent = defineComponent({
    setup() {
      return () => {
        const node = h(Editor, {
          ...props,
          ref: editorRef,
          'onUpdate:modelValue': payload => events.push({ name: 'update:modelValue', payload }),
          onImageReady: payload => imageEvents.push({ name: 'image-ready', payload }),
          onSelectionChange: payload => selectionEvents.push({ name: 'selection-change', payload }),
          onEditRequest: () => { editRequests.push('edit-request'); onEditRequest?.(props) },
        }, slots)
        return wrap ? h('div', { class: 'page-scroller' }, [node]) : node
      }
    },
  })
  const page = await mountComponent(t, Parent)
  const root = page.root
  const svg = root.find(el => el.tagName === 'svg' && el.getAttribute('role') === 'img')
  const captured = new Set()
  Object.assign(svg, {
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 800 }),
    setPointerCapture: id => captured.add(id),
    hasPointerCapture: id => captured.has(id),
    releasePointerCapture: id => captured.delete(id),
  })
  const img = root.find(el => el.tagName === 'img')
  // The editor waits on nothing but Vue's own ticks (re-renders, watchers and
  // the nextTick a tool request or the toolbar pin waits for).
  const flush = async () => { for (let round = 0; round < 4; round++) await nextTick() }
  const button = label => root.find(el => el.tagName === 'button' && el.text === label)
  const toolButtons = () => root.findAll(el => el.tagName === 'button' && el.props['aria-pressed'] !== undefined)
  const strokeGroups = () => svg.children.filter(el => el.tagName === 'g')
  const handles = index => strokeGroups()[index].findAll(el => el.tagName === 'circle' && el.classList.contains('reference-point'))
  const at = async (type, x, y, pointerId) => { await svg.dispatch(type, pointer(x, y, pointerId)); await flush() }
  const e = {
    page, root, props, svg, img, events, imageEvents, selectionEvents, editRequests, flush, button, toolButtons,
    get exposed() { return editorRef.value },
    get state() { return instanceOf(page.vm.subTree, Editor).setupState },
    unmount: () => page.unmount(),
    value: () => events.at(-1)?.payload,
    async loadImage() {
      Object.assign(img, { naturalWidth: 1000, naturalHeight: 800 })
      await img.dispatch('load')
      await flush()
    },
    // A click as the browser delivers it; the handler's result (if any).
    async press(target) {
      const el = typeof target === 'string' ? button(target) : target
      assert.ok(el, `no button ${target}`)
      if (disabledControl(el)) return null
      const [result] = await el.dispatch('click')
      await flush()
      return result
    },
    tool: label => e.press(toolButtons().find(el => el.text === label) ?? label),
    pressedTool: () => toolButtons().find(el => el.props['aria-pressed'] === true)?.text ?? null,
    toolsFieldset: () => root.find(el => el.tagName === 'fieldset'),
    down: (x, y, pointerId) => at('pointerdown', x, y, pointerId),
    move: (x, y, pointerId) => svg.dispatch('pointermove', pointer(x, y, pointerId)),
    up: (x, y, pointerId) => at('pointerup', x, y, pointerId),
    cancel: async (pointerId = 7) => { await svg.dispatch('pointercancel', { pointerId }); await flush() },
    async drawDrag(label, start, end) {
      await e.tool(label)
      await e.down(...start)
      e.move(...end)
      await e.up(...end)
    },
    strokeGroups,
    strokePoints: index => strokeGroups()[index]?.find(el => el.tagName === 'polyline')?.getAttribute('points'),
    selected: index => strokeGroups()[index]?.children[0]?.classList.contains('selected') ?? false,
    // Select-mode click on a drawn stroke.
    async selectStroke(index) {
      await strokeGroups()[index].children[0].dispatch('pointerdown', pointer(0, 0))
      await flush()
    },
    async grabHandle(index, pointerId = 1) {
      await handles(0)[index].dispatch('pointerdown', pointer(0, 0, pointerId))
      await flush()
    },
    async dragHandle(index, [x, y], pointerId = 1) {
      await e.grabHandle(index, pointerId)
      e.move(x, y, pointerId)
      await e.up(x, y, pointerId)
    },
    pendingPoints: () => svg.findAll(el => el.tagName === 'circle' && el.classList.contains('pending-point')).length,
    alerts: () => root.findAll(el => el.getAttribute('role') === 'alert').map(el => el.text),
    // The editor's own error line (not the drawing-region or image warnings).
    errors: () => e.alerts().filter(text => !text.startsWith('范围待核对') && !text.startsWith('模板原图加载失败')),
    status: () => root.find(el => el.getAttribute('role') === 'status')?.text ?? null,
  }
  if (load) await e.loadImage()
  return e
}

test('Vue template compiles and fixed-coordinate warning is visible', async (t) => {
  assert.equal(typeof Editor.render, 'function', 'the template compiled')
  const e = await editor(t)
  const note = e.root.byClass('drawing-reference-note').text
  assert.match(note, /不随学生图形移动、缩放或贴合/)
  assert.match(note, /多个方案不会叠加评分/)
  assert.equal(Editor.emits[0], 'update:modelValue')
})

test('candidate starts unconfirmed and edit clears confirmation and server hash without mutation', () => {
  const input = fixture()
  const changed = changedDrawingReference(input)
  changed.variants[0].strokes[0].points[0][0] = 55
  assert.equal(changed.teacher_confirmed, false)
  assert.equal(changed.reference_hash, undefined)
  assert.equal(input.teacher_confirmed, true)
  assert.equal(input.variants[0].strokes[0].points[0][0], 100)
  assert.equal(emptyDrawingReference().teacher_confirmed, false)
  assert.equal(editableDrawingReference({ ...fixture(), teacher_confirmed: 'true' }).teacher_confirmed, false)
})

test('reference validator matches supported complete point and polyline geometry bounds', () => {
  assert.equal(drawingReferenceError(fixture()), '')
  assert.equal(drawingReferenceError(emptyDrawingReference()), '')
  const bad = [
    { ...fixture(), comparison_mode: 'automatic' },
    { ...fixture(), student_adaptive: true },
    { ...fixture(), variants: [] },
    { ...fixture(), variants: Array(5).fill(fixture().variants[0]) },
    { ...fixture(), variants: [{ strokes: [] }] },
    { ...fixture(), variants: [{ strokes: [{ kind: 'point', point: [1, 2], adaptive: true }] }] },
    { ...fixture(), variants: [{ strokes: [{ kind: 'point', point: [NaN, 0] }] }] },
    { ...fixture(), variants: [{ strokes: [{ kind: 'point', point: [true, 0] }] }] },
    { ...fixture(), variants: [{ strokes: [{ kind: 'point', point: [1001, 0] }] }] },
    { ...fixture(), variants: [{ strokes: [{ kind: 'polyline', points: [[10, 20], [10, 20]] }] }] },
    { ...fixture(), context_regions: [[900, 10, 100, 20]] },
  ]
  for (const value of bad) assert.ok(drawingReferenceError(value))
})

test('point and polyline markings are one-shot: the pointer returns to select after each completed mark', async (t) => {
  const e = await editor(t)
  await e.tool('点')
  await e.down(500, 400)
  assert.equal(e.pressedTool(), SELECT)
  await e.tool('折线')
  await e.down(500, 400)
  await e.down(600, 500)
  assert.equal(e.pressedTool(), '折线')
  await e.press('完成图形')
  assert.equal(e.pressedTool(), SELECT)
})

test('pointer mapping uses full image aspect ratio and clamps to template not student', () => {
  assert.deepEqual(normalizedDrawingPoint({ clientX: 520, clientY: 210 }, { left: 20, top: 10, width: 1000, height: 800 }), [500, 250])
  assert.deepEqual(normalizedDrawingPoint({ clientX: -50, clientY: 1600 }, { left: 0, top: 0, width: 1000, height: 800 }), [0, 1000])
  assert.equal(normalizedDrawingPoint({ clientX: 0, clientY: 0 }, { width: 0, height: 1 }), null)
})

test('complete variants remain separate, capped at four; mutation and undo require fresh confirmation', async (t) => {
  const e = await editor(t)
  const copy = '新增完整方案（复制当前）'
  for (let index = 0; index < 6; index++) await e.press(copy)
  assert.equal(e.value().variants.length, 4)
  assert.deepEqual(e.root.find(el => el.tagName === 'select').byTag('option').map(option => option.text), ['方案 1', '方案 2', '方案 3', '方案 4'])
  assert.equal(e.button(copy).disabled, true)
  assert.equal(e.value().teacher_confirmed, false)
  // The newest copy is selected; moving its endpoint leaves every other
  // variant alone, the one it was copied from included.
  await e.selectStroke(0)
  await e.dragHandle(0, [333, 160])
  assert.deepEqual(e.value().variants.map(variant => variant.strokes[0].points[0]), [[100, 200], [100, 200], [100, 200], [333, 200]])
  assert.deepEqual(e.value().variants[0].strokes[0].points[0], [100, 200])
  await e.press('撤销')
  assert.equal(e.value().variants.length, 4)
  await e.press('撤销')
  assert.equal(e.value().variants.length, 3)
  assert.equal(e.value().teacher_confirmed, false)
  assert.ok(e.events.every((event) => event.name === 'update:modelValue' && !drawingReferenceError(event.payload) && event.payload.reference_hash === undefined))
})

test('deleting final stroke selects manual mode; no invalid empty fixed answer emitted', async (t) => {
  const e = await editor(t)
  await e.selectStroke(0)
  await e.press('删除所选笔画')
  assert.deepEqual(e.value().variants, [])
  assert.equal(e.value().comparison_mode, 'manual')
  assert.equal(e.value().teacher_confirmed, false)
  assert.equal(drawingReferenceError(e.value()), '')
  assert.equal(e.strokeGroups().length, 0)
  assert.match(e.root.byClass('drawing-reference-generation').text, /标准图留空也可保存/)
})

test('point and staged polyline drawing emits complete references only, with confirmation owned by save', async (t) => {
  const e = await editor(t, emptyDrawingReference())
  await e.tool('折线')
  await e.down(100, 160)
  assert.equal(e.events.length, 0)
  assert.equal(e.pendingPoints(), 1)
  assert.equal(e.exposed.valueForSave().teacher_confirmed, false)
  await e.down(300, 160)
  await e.press('完成图形')
  assert.deepEqual(e.value().variants[0].strokes[0].points, [[100, 200], [300, 200]])
  assert.equal(e.value().teacher_confirmed, false)
  await e.tool('点')
  await e.down(500, 400)
  assert.deepEqual(e.value().variants[0].strokes[1], { kind: 'point', point: [500, 500] })
  assert.equal(e.value().teacher_confirmed, false)
})

test('dragging endpoints edits only fixed geometry and rejects zero-length segments', async (t) => {
  const e = await editor(t)
  await e.selectStroke(0)
  await e.dragHandle(0, [200, 160])
  assert.deepEqual(e.value().variants[0].strokes[0].points[0], [200, 200])
  assert.equal(e.value().teacher_confirmed, false)
  await e.dragHandle(0, [300, 160])
  assert.match(e.errors().join('\n'), /重合/)
  assert.equal(e.strokePoints(0), '200,200 300,200')
  assert.equal(e.events.length, 1)
})

test('parent v-model echo does not discard edit history, and external new candidate is not auto-confirmed', async (t) => {
  const e = await editor(t)
  await e.press('新增完整方案（复制当前）')
  e.props.modelValue = e.value()
  await e.flush()
  assert.equal(e.button('撤销').disabled, false, 'the echoed value keeps the edit to undo')
  e.props.modelValue = { ...fixture(), teacher_confirmed: false, comparison_mode: 'manual' }
  await e.flush()
  assert.equal(e.button('撤销').disabled, true, 'a new external value starts a fresh history')
  assert.equal(e.exposed.valueForSave().teacher_confirmed, false)
})

test('shared geometry mode does not consume endpoint mutations or confirm a reference', async (t) => {
  const e = await editor(t, fixture(), { slots: { 'canvas-overlays': () => h('div', { class: 'geometry-overlay' }, '几何层') } })
  e.props.interactive = false
  await e.flush()
  assert.equal(e.toolsFieldset().disabled, true)
  // Even a click that slips through the disabled toolbar draws nothing.
  await e.button('点').dispatch('click')
  await e.down(400, 320)
  await e.selectStroke(0)
  e.state.dragStart({ button: 0, pointerId: 1, preventDefault() {} }, 0, 0)
  e.move(400, 320, 1)
  await e.up(400, 320, 1)
  assert.equal(e.events.length, 0)
  assert.equal(e.strokePoints(0), '100,200 300,200')
  assert.equal(e.selected(0), false)
  assert.ok(e.root.byClass('drawing-reference-canvas').byClass('geometry-overlay'), 'geometry overlays render on the drawing canvas')
  assert.equal(e.root.byTag('img').length, 1)
  assert.ok(e.svg.classList.contains('is-readonly'))
  assert.equal(e.svg.classList.contains('is-reference-priority'), false)
  e.props.priorityEditing = true
  await e.flush()
  assert.ok(e.svg.classList.contains('is-reference-priority'))
  e.props.priorityEditing = false
  e.props.interactive = true
  await e.flush()
  assert.equal(e.svg.classList.contains('is-readonly'), false)
  assert.equal(e.svg.classList.contains('is-reference-priority'), false, 'select mode leaves the overlays on top')
  await e.tool('点')
  assert.ok(e.svg.classList.contains('is-reference-priority'), 'a drawing tool takes the pointer')
})

test('failed image and active save never allow confirming or editing against a blank preview', async (t) => {
  for (const reason of ['image', 'save']) {
    const e = await editor(t)
    await e.tool('点')
    if (reason === 'image') await e.img.dispatch('error')
    else e.props.disabled = true
    await e.flush()
    assert.equal(e.events.length, 0, reason)
    assert.equal(e.toolsFieldset().disabled, true, reason)
    assert.equal(e.pressedTool(), SELECT, `${reason}: the armed tool is dropped`)
    await e.down(500, 400)
    assert.equal(e.events.length, 0, reason)
    if (reason === 'image') assert.ok(e.alerts().some(text => text.startsWith('模板原图加载失败，不能在空白底图上确认标准图')))
    e.unmount()
  }
})

test('switching to geometry cancels an unfinished endpoint drag without publishing partial positions', async (t) => {
  const e = await editor(t)
  await e.selectStroke(0)
  await e.grabHandle(0)
  e.move(400, 320, 1)
  await e.flush()
  assert.equal(e.strokePoints(0), '400,400 300,200', 'the drag previews on the canvas')
  e.props.interactive = false
  await e.flush()
  assert.equal(e.strokePoints(0), '100,200 300,200')
  assert.equal(e.events.length, 0)
})

test('new image URL resets readiness until that exact original image loads successfully', async (t) => {
  const e = await editor(t)
  const loading = '模板原图加载中，加载完成后才能核对标准图。'
  assert.deepEqual(e.imageEvents.map(event => event.payload), [false, true])
  e.props.imageUrl = 'new-original.png'
  await e.flush()
  assert.equal(e.status(), loading)
  assert.equal(e.toolsFieldset().disabled, true)
  assert.equal(e.events.length, 0)
  // A late load event of the previous image does not count.
  await e.img.handlers.load({ currentTarget: { getAttribute: () => 'old-original.png' } })
  await e.flush()
  assert.equal(e.status(), loading)
  assert.equal(e.toolsFieldset().disabled, true)
  await e.loadImage()
  assert.equal(e.img.getAttribute('src'), 'new-original.png')
  assert.equal(e.status(), null)
  assert.equal(e.toolsFieldset().disabled, false)
  assert.equal(e.imageEvents.at(-1).payload, true)
  await e.img.dispatch('error')
  await e.flush()
  assert.equal(e.toolsFieldset().disabled, true)
  assert.equal(e.imageEvents.at(-1).payload, false)
})

test('shared delete exposes selection and deletion without changing coordinate or confirmation rules', async (t) => {
  const e = await editor(t)
  const exposed = e.exposed
  assert.deepEqual(Object.keys(exposed).sort(), ['clearSelection', 'deleteSelectedStroke', 'prepareForSave', 'valueForSave'])
  assert.deepEqual(e.selectionEvents.map((event) => event.payload), [false])
  await e.selectStroke(0)
  assert.equal(e.selectionEvents.at(-1).payload, true)
  exposed.clearSelection()
  await e.flush()
  assert.equal(e.selectionEvents.at(-1).payload, false)
  assert.equal(e.selected(0), false)
  assert.equal(exposed.deleteSelectedStroke(), false)
  assert.equal(e.events.length, 0)
  await e.selectStroke(0)
  await e.grabHandle(0, 2)
  assert.equal(e.selectionEvents.at(-1).payload, true)
  e.move(250, 160, 2)
  exposed.clearSelection()
  await e.flush()
  assert.equal(e.strokePoints(0), '100,200 300,200')
  assert.equal(e.selectionEvents.at(-1).payload, false)
  await e.up(250, 160, 2)
  assert.equal(e.events.length, 0, 'a cleared drag publishes nothing when the pointer is released')
  await e.selectStroke(0)
  assert.equal(exposed.deleteSelectedStroke(), true)
  await e.flush()
  assert.equal(e.selectionEvents.at(-1).payload, false)
  assert.equal(e.value().comparison_mode, 'manual')
  assert.equal(e.value().teacher_confirmed, false)
  assert.equal(drawingReferenceError(e.value()), '')
})

test('saving flushes a pending complete polyline without an extra finish or confirmation click', async (t) => {
  const e = await editor(t)
  await e.tool('折线')
  await e.down(400, 320)
  await e.down(600, 480)
  assert.equal(e.exposed.prepareForSave(), '')
  await e.flush()
  assert.equal(e.pendingPoints(), 0)
  assert.deepEqual(e.value().variants[0].strokes.at(-1), { kind: 'polyline', points: [[400, 400], [600, 600]] })
  assert.equal(e.value().teacher_confirmed, false)
  assert.equal(e.exposed.prepareForSave(), '')
  await e.tool('折线')
  await e.down(700, 560)
  assert.match(e.exposed.prepareForSave(), /至少需要/)
  await e.flush()
  assert.equal(e.pendingPoints(), 1)
})

test('shared selection is cleared on tool, variant, background, image, and editing-mode changes', async (t) => {
  for (const [name, clear] of [
    ['tool', e => e.tool('点')],
    ['variant', e => e.root.find(el => el.tagName === 'select').dispatch('change')],
    ['background', e => e.down(10, 10)],
    ['geometry mode', e => { e.props.interactive = false }],
    ['controls hidden', e => { e.props.showControls = false }],
    ['image', e => { e.props.imageUrl = 'different-image.png' }],
    ['new value', e => { e.props.modelValue = { ...fixture(), teacher_confirmed: false } }],
  ]) {
    const e = await editor(t)
    await e.selectStroke(0)
    assert.equal(e.selected(0), true, name)
    await clear(e)
    await e.flush()
    assert.equal(e.selected(0), false, name)
    assert.equal(e.selectionEvents.at(-1).payload, false, name)
    e.unmount()
  }
})

test('both toolbar layouts keep editing tools without separate confirmation controls', async (t) => {
  async function markup(props) {
    return renderToString(createSSRApp({ render: () => h(Editor, { modelValue: fixture(), imageUrl: 'synthetic.png', ...props }, {
      'geometry-tools': () => h('button', { 'data-geometry-tool': '' }, '新增豁免框'),
    }) }))
  }
  assert.equal(Editor.props.compactControls.default, false)
  assert.equal(Editor.props.sharedDelete.default, false)
  // The fixture line runs from x=100 to x=300; this region stops at x=200.
  const compact = await markup({ compactControls: true, sharedDelete: true, drawingRegions: [[50, 150, 200, 260]] })
  assert.match(compact, /drawing-reference-workbar/)
  assert.match(compact, /data-geometry-tool/)
  assert.match(compact, /drawing-reference-drawing-tools/)
  assert.doesNotMatch(compact, /drawing-reference-settings/)
  assert.doesNotMatch(compact, /比较方式/)
  assert.doesNotMatch(compact, /drawing-reference-confirm/)
  assert.doesNotMatch(compact, /删除所选笔画|删除笔画/)
  assert.ok(compact.indexOf('data-geometry-tool') < compact.indexOf('drawing-reference-drawing-tools'))
  assert.match(compact, /方案 1 有笔画超出该题作图区域/)
  assert.doesNotMatch(compact, /geometrywarning/i)
  const geometryOnly = await markup({ compactControls: true, sharedDelete: true, showControls: false })
  assert.match(geometryOnly, /data-geometry-tool/)
  assert.doesNotMatch(geometryOnly, /drawing-reference-drawing-tools|drawing-reference-confirm/)
  const legacy = await markup({ compactControls: false })
  assert.doesNotMatch(legacy, /type="checkbox"|drawing-reference-confirm/)
  assert.match(legacy, /删除所选笔画/)
  assert.match(legacy, /选择／拖动端点/)
  assert.doesNotMatch(legacy, /<details/)
  assert.match(legacy, /标准图随“保存模板”统一确认/)

  // The compact toolbar offers 编辑标准图 for an existing reference only while
  // the page is in geometry mode and the reference could be edited.
  const editButton = '编辑标准图'
  const geometry = await editor(t, fixture(), { compactControls: true, interactive: false })
  await geometry.press(editButton)
  assert.deepEqual(geometry.editRequests, ['edit-request'])
  assert.deepEqual(geometry.events, [])
  geometry.unmount()
  for (const [name, value, options] of [
    ['editing already', fixture(), { interactive: true }],
    ['no reference yet', null, { interactive: false }],
    ['saving', fixture(), { interactive: false, disabled: true }],
    ['image still loading', fixture(), { interactive: false, load: false }],
  ]) {
    const e = await editor(t, value, { compactControls: true, ...options })
    assert.equal(e.button(editButton), null, name)
    e.unmount()
  }
})

// memoryDom elements know their parentNode only; a browser also gives every
// element parentElement and isConnected, and getComputedStyle, which the
// sticky toolbar reads to find the scroll container it pins itself to. (An
// element held in a ref is a reactive proxy here, unlike a browser node.)
function scrollContainerLayout(t, scrollerClass) {
  const descriptors = {
    parentElement: { get() { return this.parentNode instanceof MElement ? this.parentNode : null }, configurable: true },
    isConnected: { get() { return memoryDocument.roots.some(root => root.contains(toRaw(this))) }, configurable: true },
  }
  Object.defineProperties(MElement.prototype, descriptors)
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'getComputedStyle')
  globalThis.getComputedStyle = el => ({ overflowY: el.classList?.contains(scrollerClass) ? 'auto' : 'visible' })
  t.after(() => {
    for (const name of Object.keys(descriptors)) delete MElement.prototype[name]
    if (previous) Object.defineProperty(globalThis, 'getComputedStyle', previous)
    else delete globalThis.getComputedStyle
  })
}

test('switching away from a drawing releases its pinned toolbar', async (t) => {
  scrollContainerLayout(t, 'page-scroller')
  const e = await editor(t, fixture(), { compactControls: true, canvasVisible: true, wrap: true })
  const scroller = e.root.byClass('page-scroller')
  const workbar = e.root.byClass('drawing-reference-workbar')
  const gap = () => e.root.byClass('drawing-reference-workbar-gap')
  assert.equal(gap(), null)
  // The teacher scrolls until the toolbar passes the container's top edge.
  scroller.getBoundingClientRect = () => ({ top: 64, left: 0, width: 800, height: 600 })
  workbar.getBoundingClientRect = () => ({ top: 20, left: 32, width: 640, height: 48 })
  Object.defineProperty(workbar, 'offsetHeight', { value: 48, configurable: true })
  await scroller.dispatch('scroll')
  await e.flush()
  assert.deepEqual([workbar.style.position, workbar.style.top, workbar.style.left, workbar.style.width], ['fixed', '64px', '32px', '640px'])
  assert.equal(gap().style.height, '48px', 'a gap keeps the toolbar\'s place')
  e.props.canvasVisible = false
  await e.flush()
  assert.equal(gap(), null)
  assert.equal(workbar.style.position, undefined)
})

test('compact point and polyline tools request reference mode directly and wait for the parent', async (t) => {
  for (const tool of [SELECT, '点', '折线']) {
    const e = await editor(t, fixture(), { compactControls: true, interactive: false, onEditRequest: (props) => { props.interactive = true } })
    assert.equal(e.toolsFieldset().disabled, false, 'geometry mode still offers the tools')
    assert.ok(e.button('编辑标准图'), 'the reference is not being edited yet')
    assert.equal(e.pressedTool(), null)
    assert.equal(await e.tool(tool), true)
    assert.deepEqual(e.editRequests, ['edit-request'])
    assert.equal(e.pressedTool(), tool)
    assert.equal(e.button('编辑标准图'), null)
    assert.equal(e.events.length, 0, 'requesting a tool must not mutate the reference')
    assert.equal(await e.tool(tool), true)
    assert.equal(e.editRequests.length, 1, 'already editing needs no parent mode request')
    e.unmount()
  }
  const refused = await editor(t, fixture(), { compactControls: true, interactive: false })
  assert.equal(await refused.tool('点'), false)
  assert.deepEqual(refused.editRequests, ['edit-request'])
  assert.equal(refused.pressedTool(), null)
  assert.ok(refused.button('编辑标准图'), 'still not editing')
  refused.props.interactive = true
  await refused.flush()
  assert.equal(refused.pressedTool(), SELECT, 'the refused tool was never armed')
})

test('one-click tool activation cannot bypass save, image, or controls locks', async (t) => {
  const enable = e => { e.props.interactive = true }
  for (const [name, lock, unlock] of [
    ['saving', (e) => { e.props.disabled = true }, (e) => { e.props.disabled = false }],
    ['controls hidden', (e) => { e.props.showControls = false }, (e) => { e.props.showControls = true }],
    ['image loading', (e) => { e.props.imageUrl = 'next-original.png' }, e => e.loadImage()],
    ['image failed', e => e.img.dispatch('error'), e => e.loadImage()],
  ]) {
    const e = await editor(t, fixture(), { compactControls: true, interactive: false, onEditRequest: enable })
    const point = e.toolButtons().find(el => el.text === '点')
    await lock(e)
    await e.flush()
    assert.ok(!e.toolsFieldset() || e.toolsFieldset().disabled, `${name}: the toolbar is locked`)
    // A click that lands as the lock arrives.
    const [granted] = await point.dispatch('click')
    await e.flush()
    assert.equal(granted, false, name)
    assert.deepEqual(e.editRequests, [], name)
    assert.deepEqual(e.events, [], name)
    await unlock(e)
    enable(e)
    await e.flush()
    assert.equal(e.pressedTool(), SELECT, `${name}: the point tool was never armed`)
    e.unmount()
  }
  const delayed = await editor(t, fixture(), { compactControls: true, interactive: false, onEditRequest: (props) => { props.interactive = true; props.disabled = true } })
  assert.equal(await delayed.tool('折线'), false)
  assert.deepEqual(delayed.events, [])
  delayed.props.disabled = false
  await delayed.flush()
  assert.equal(delayed.pressedTool(), SELECT)
})

test('teacher circle uses real page aspect ratio, saves fixed and supports undo/redo', async (t) => {
  const e = await editor(t, null)
  assert.equal(e.exposed.valueForSave(), null)
  await e.drawDrag('圆', [500, 400], [600, 400])
  const stroke = e.value().variants[0].strokes[0]
  assert.deepEqual(stroke, { kind: 'ellipse', center: [500, 500], radii: [100, 125], fill: false })
  const ellipse = e.strokeGroups()[0].children[0]
  assert.deepEqual(['cx', 'cy', 'rx', 'ry'].map(name => ellipse.getAttribute(name)), ['500', '500', '100', '125'])
  assert.equal(e.exposed.valueForSave().comparison_mode, 'fixed')
  assert.equal(e.value().teacher_confirmed, false)
  await e.press('撤销')
  assert.equal(e.value().variants.length, 0)
  assert.equal(e.strokeGroups().length, 0)
  await e.press('重做')
  assert.deepEqual(e.value().variants[0].strokes[0], stroke)
  assert.equal(e.value().teacher_confirmed, false)
})

test('drag tools and filled polygons produce valid bounded teacher geometry', async (t) => {
  for (const tool of ['直线', '箭头', '矩形', '椭圆', '椭圆填色', '自由曲线', '涂抹']) {
    const e = await editor(t, null)
    await e.drawDrag(tool, [300, 240], [600, 480])
    const value = e.exposed.valueForSave()
    assert.equal(drawingReferenceError(value), '', tool)
    assert.equal(value.comparison_mode, 'fixed', tool)
    assert.equal(e.pressedTool(), SELECT, tool)
    e.unmount()
  }
  for (const tool of ['多边形', '区域填色']) {
    const e = await editor(t, null)
    await e.tool(tool)
    for (const point of [[300, 240], [600, 240]]) await e.down(...point)
    assert.match(e.exposed.prepareForSave(), /至少需要 3/)
    await e.down(600, 480)
    assert.equal(e.exposed.prepareForSave(), '')
    const stroke = e.exposed.valueForSave().variants[0].strokes[0]
    assert.equal(stroke.kind, tool === '区域填色' ? 'polygon' : 'polyline')
    assert.equal(stroke.points.length, tool === '区域填色' ? 3 : 4)
    assert.equal(drawingReferenceError(e.exposed.valueForSave()), '')
    e.unmount()
  }
})

test('cancelling drawing gestures and invalid ellipse edits never publishes partial geometry', async (t) => {
  const e = await editor(t, null)
  await e.tool('椭圆')
  await e.down(300, 240)
  e.move(600, 480)
  await e.flush()
  assert.equal(e.strokeGroups().length, 1, 'the gesture previews')
  assert.match(e.exposed.prepareForSave(), /尚在拖动/)
  await e.cancel(7)
  assert.equal(e.strokeGroups().length, 0)
  assert.equal(e.exposed.valueForSave(), null)
  assert.deepEqual(e.events, [])
  await e.drawDrag('椭圆', [300, 240], [600, 480])
  const before = structuredClone(e.exposed.valueForSave())
  await e.selectStroke(0)
  await e.dragHandle(0, [0, 0], 7)
  assert.deepEqual(e.exposed.valueForSave(), before)
  assert.match(e.errors().join('\n'), /坐标无效|超出/)
})

test('brush click renders a dot and long strokes keep both endpoints within the contract', async (t) => {
  const e = await editor(t, null)
  await e.drawDrag('涂抹', [300, 240], [300, 240])
  assert.deepEqual(e.value().variants[0].strokes[0], { kind: 'brush', points: [[300, 300]], width: 12 })
  assert.ok(e.strokeGroups()[0].find(el => el.tagName === 'circle' && el.classList.contains('reference-area')), 'the dot is drawn')
  await e.tool('自由曲线')
  await e.down(100, 160)
  for (let x = 101; x <= 800; x++) e.move(x, 160 + x % 50)
  await e.up(800, 160)
  const stroke = e.value().variants[0].strokes.at(-1)
  assert.ok(stroke.points.length <= 128)
  assert.deepEqual(stroke.points[0], [100, 200])
  assert.deepEqual(stroke.points.at(-1), [800, 200])
  assert.equal(drawingReferenceError(e.exposed.valueForSave()), '')
})

test('teacher controls offer the full tool set and no model generation or manual confirmation', async (t) => {
  const e = await editor(t, null)
  const labels = e.toolButtons().map(el => el.text)
  for (const label of ['圆', '椭圆', '矩形', '箭头', '多边形', '区域填色', '涂抹', '自由曲线']) {
    assert.ok(labels.includes(label), label)
    await e.tool(label)
    assert.equal(e.pressedTool(), label)
  }
  e.unmount()
  assert.deepEqual(Editor.emits, ['update:modelValue', 'image-ready', 'selection-change', 'edit-request'])
  for (const compactControls of [false, true]) {
    const layout = await editor(t, fixture(), { compactControls, interactive: !compactControls })
    assert.doesNotMatch(layout.root.text, /生成标准图/)
    const buttons = layout.root.findAll(el => el.tagName === 'button').map(el => el.text)
    assert.deepEqual(buttons.filter(text => /生成|确认|人工|手动/.test(text)), [], `compact=${compactControls}`)
    layout.unmount()
  }
})

test('three-point arc keeps a round circle in pixels and rejects collinear or off-page arcs', () => {
  // On a 1000x800 image, x and y normalized units differ; the arc is fitted in pixels.
  const arc = arcThroughPoints([[400, 500], [500, 375], [600, 500]], [1000, 800])
  assert.deepEqual(arc.center, [500, 500])
  assert.deepEqual(arc.points[0], [400, 500])
  assert.deepEqual(arc.points.at(-1), [600, 500])
  assert.ok(arc.points.every(([x, y]) => Math.abs(Math.hypot(x - 500, (y - 500) * 0.8) - 100) < 0.5))
  assert.ok(arc.points.every(([, y]) => y <= 500), 'the arc passes through the clicked middle point side')
  assert.ok(arc.points.length <= 127)
  assert.equal(arcThroughPoints([[100, 100], [200, 200], [300, 300]], [1000, 800]), null)
  // Center (50,325)px, radius 85px: the arc through the top bulges to x=-35px.
  assert.equal(arcThroughPoints([[10, 500], [50, 300], [90, 500]], [1000, 800]), null)
})

test('smooth curve passes through every clicked point within the stroke budget', () => {
  const clicks = [[100, 800], [300, 300], [500, 100], [700, 300], [900, 800]]
  const curve = splineThroughPoints(clicks, [1000, 800])
  for (const point of clicks) assert.ok(curve.some((sample) => Math.hypot(sample[0] - point[0], sample[1] - point[1]) < 0.2), point)
  assert.ok(curve.length > clicks.length && curve.length <= 128)
  assert.ok(splineThroughPoints(Array.from({ length: 128 }, (_, index) => [index * 7, 500 + (index % 2)]), [1000, 800]).length <= 128)
})

test('range warning mirrors the rendered stroke, including brush width', () => {
  const reference = (stroke) => ({ ...emptyDrawingReference(), comparison_mode: 'fixed', variants: [{ strokes: [stroke] }] })
  const regions = [[100, 100, 500, 500]]
  assert.equal(drawingReferenceRegionWarning(emptyDrawingReference(), regions), '')
  assert.equal(drawingReferenceRegionWarning(reference({ kind: 'polygon', points: [[200, 200], [400, 200], [300, 400]] }), regions), '')
  assert.match(drawingReferenceRegionWarning(reference({ kind: 'ellipse', center: [300, 300], radii: [250, 50], fill: true }), regions), /超出/)
  assert.equal(drawingReferenceRegionWarning(reference({ kind: 'brush', points: [[300, 120]], width: 10 }), regions, [1000, 1000]), '')
  assert.match(drawingReferenceRegionWarning(reference({ kind: 'brush', points: [[300, 120]], width: 60 }), regions, [1000, 1000]), /超出/)
  assert.match(drawingReferenceRegionWarning(reference({ kind: 'point', point: [300, 300] }), []), /尚未框选作图区域/)
})

test('new shape tools produce existing contract strokes and return to selection', async (t) => {
  const e = await editor(t, null)
  const last = () => e.value().variants[0].strokes.at(-1)
  await e.drawDrag('圆填色', [500, 400], [580, 400])
  assert.deepEqual(last(), { kind: 'ellipse', center: [500, 500], radii: [80, 100], fill: true })
  assert.equal(e.pressedTool(), SELECT)
  await e.drawDrag('矩形填色', [200, 160], [300, 240])
  assert.deepEqual(last(), { kind: 'polygon', points: [[200, 200], [300, 200], [300, 300], [200, 300]] })
  await e.tool('圆弧')
  await e.down(400, 400); await e.down(500, 300); await e.down(600, 400)
  assert.equal(last().kind, 'polyline')
  assert.equal(e.pressedTool(), SELECT)
  await e.tool('扇形填色')
  await e.down(400, 400); await e.down(500, 300); await e.down(600, 400)
  assert.equal(last().kind, 'polygon')
  assert.deepEqual(last().points[0], [500, 500])
  await e.tool('平滑曲线')
  await e.down(100, 600); await e.down(300, 200); await e.down(500, 600)
  await e.press('完成图形')
  assert.equal(last().kind, 'polyline')
  assert.ok(last().points.length > 3)
  assert.equal(drawingReferenceError(e.value()), '')
  assert.equal(e.value().comparison_mode, 'fixed')
})

test('a circle clamped to the page edge still satisfies the containment contract', async (t) => {
  const e = await editor(t, null)
  await e.tool('圆')
  await e.down(103, 400, 8)
  await e.up(900, 400, 8)
  const stroke = e.value().variants[0].strokes[0]
  assert.equal(stroke.kind, 'ellipse')
  assert.ok(stroke.center[0] - stroke.radii[0] >= 0)
  assert.deepEqual(e.errors(), [])
})
