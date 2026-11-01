import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import { parse, compileTemplate } from '@vue/compiler-sfc'
import { createSSRApp, h, nextTick, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'

const source = readFileSync(new URL('../src/components/DrawingReferenceEditor.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
const require = createRequire(import.meta.url)
const script = descriptor.script.content.replace("from 'vue'", `from '${pathToFileURL(require.resolve('vue')).href}'`)
const { default: component, emptyDrawingReference, changedDrawingReference, editableDrawingReference, drawingReferenceError, normalizedDrawingPoint, arcThroughPoints, splineThroughPoints, drawingReferenceRegionWarning } = await import(`data:text/javascript;base64,${Buffer.from(script).toString('base64')}`)

const fixture = () => ({ ...emptyDrawingReference(), comparison_mode: 'fixed', variants: [{ strokes: [{ kind: 'polyline', points: [[100, 200], [300, 200]] }] }], teacher_confirmed: true, reference_hash: 'server-only' })
function editor(value = fixture(), onEditRequest) {
  const events = []
  const imageEvents = []
  const selectionEvents = []
  const editRequests = []
  let exposed
  const props = reactive({ modelValue: value, reference: null, imageUrl: 'synthetic.png', drawingRegions: [[50, 50, 900, 900]] })
  const state = component.setup(props, {
    emit(name, payload) {
      if (name === 'edit-request') { editRequests.push(name); onEditRequest?.(props); return }
      (name === 'image-ready' ? imageEvents : name === 'selection-change' ? selectionEvents : events).push({ name, payload })
    },
    expose(value) { exposed = value },
  })
  state.canvas.value = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 800 }), setPointerCapture() {}, hasPointerCapture: () => true, releasePointerCapture() {} }
  state.imageSize.value = [1000, 800]
  state.imageLoaded.value = true
  return { state, props, events, imageEvents, selectionEvents, editRequests, exposed }
}

test('Vue template compiles and fixed-coordinate warning is visible', () => {
  assert.deepEqual(errors, [])
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: 'DrawingReferenceEditor.vue', id: 'test' }).errors, [])
  assert.match(source, /不随学生图形移动、缩放或贴合/)
  assert.match(source, /多个方案不会叠加评分/)
  assert.equal(component.emits[0], 'update:modelValue')
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

test('point and polyline markings are one-shot: the pointer returns to select after each completed mark', () => {
  const { state } = editor()
  state.setTool('point')
  state.canvasDown({ clientX: 500, clientY: 400, button: 0 })
  assert.equal(state.tool.value, 'select')
  state.setTool('polyline')
  state.canvasDown({ clientX: 500, clientY: 400, button: 0 })
  state.canvasDown({ clientX: 600, clientY: 500, button: 0 })
  assert.equal(state.tool.value, 'polyline')
  state.finishPolyline()
  assert.equal(state.tool.value, 'select')
})

test('pointer mapping uses full image aspect ratio and clamps to template not student', () => {
  assert.deepEqual(normalizedDrawingPoint({ clientX: 520, clientY: 210 }, { left: 20, top: 10, width: 1000, height: 800 }), [500, 250])
  assert.deepEqual(normalizedDrawingPoint({ clientX: -50, clientY: 1600 }, { left: 0, top: 0, width: 1000, height: 800 }), [0, 1000])
  assert.equal(normalizedDrawingPoint({ clientX: 0, clientY: 0 }, { width: 0, height: 1 }), null)
})

test('complete variants remain separate, capped at four; mutation and undo require fresh confirmation', () => {
  const { state, events } = editor()
  for (let index = 0; index < 6; index++) state.addVariant()
  assert.equal(state.draft.value.variants.length, 4)
  assert.equal(state.draft.value.teacher_confirmed, false)
  state.draft.value.variants[3].strokes[0].points[0][0] = 333
  assert.equal(state.draft.value.variants[0].strokes[0].points[0][0], 100)
  state.undo()
  assert.equal(state.draft.value.variants.length, 3)
  assert.equal(state.draft.value.teacher_confirmed, false)
  assert.ok(events.every((event) => event.name === 'update:modelValue' && !drawingReferenceError(event.payload) && event.payload.reference_hash === undefined))
})

test('deleting final stroke selects manual mode; no invalid empty fixed answer emitted', () => {
  const { state, events } = editor()
  state.selectStroke(0)
  state.deleteStroke()
  assert.deepEqual(state.draft.value.variants, [])
  assert.equal(state.draft.value.comparison_mode, 'manual')
  assert.equal(state.draft.value.teacher_confirmed, false)
  assert.equal(drawingReferenceError(events.at(-1).payload), '')
})

test('point and staged polyline drawing emits complete references only, with confirmation owned by save', () => {
  const { state, events } = editor(emptyDrawingReference())
  state.setTool('polyline')
  state.canvasDown({ button: 0, clientX: 100, clientY: 160 })
  assert.equal(events.length, 0)
  assert.equal(state.draft.value.teacher_confirmed, false)
  state.canvasDown({ button: 0, clientX: 300, clientY: 160 })
  state.finishPolyline()
  assert.deepEqual(state.draft.value.variants[0].strokes[0].points, [[100, 200], [300, 200]])
  assert.equal(events.at(-1).payload.teacher_confirmed, false)
  state.setTool('point')
  state.canvasDown({ button: 0, clientX: 500, clientY: 400 })
  assert.deepEqual(events.at(-1).payload.variants[0].strokes[1], { kind: 'point', point: [500, 500] })
  assert.equal(events.at(-1).payload.teacher_confirmed, false)
})

test('dragging endpoints edits only fixed geometry and rejects zero-length segments', () => {
  const { state, events } = editor()
  const down = { button: 0, pointerId: 1, preventDefault() {} }
  state.dragStart(down, 0, 0)
  state.dragMove({ pointerId: 1, clientX: 200, clientY: 160 })
  state.dragEnd({ pointerId: 1 })
  assert.deepEqual(events.at(-1).payload.variants[0].strokes[0].points[0], [200, 200])
  assert.equal(events.at(-1).payload.teacher_confirmed, false)
  state.dragStart(down, 0, 0)
  state.dragMove({ pointerId: 1, clientX: 300, clientY: 160 })
  state.dragEnd({ pointerId: 1 })
  assert.match(state.error.value, /重合/)
  assert.deepEqual(state.draft.value.variants[0].strokes[0].points[0], [200, 200])
  assert.equal(events.length, 1)
})

test('parent v-model echo does not discard edit history, and external new candidate is not auto-confirmed', async () => {
  const { state, props, events } = editor()
  state.addVariant()
  props.modelValue = events.at(-1).payload
  await nextTick()
  assert.equal(state.history.value.length, 1)
  props.modelValue = { ...fixture(), teacher_confirmed: false, comparison_mode: 'manual' }
  await nextTick()
  assert.equal(state.history.value.length, 0)
  assert.equal(state.draft.value.teacher_confirmed, false)
})

test('shared geometry mode does not consume endpoint mutations or confirm a reference', async () => {
  const { state, props, events } = editor()
  props.interactive = false
  await nextTick()
  state.setTool('point')
  state.canvasDown({ button: 0, clientX: 400, clientY: 320 })
  state.dragStart({ button: 0, pointerId: 1, preventDefault() {} }, 0, 0)
  state.dragMove({ pointerId: 1, clientX: 400, clientY: 320 })
  state.dragEnd({ pointerId: 1 })
  assert.equal(events.length, 0)
  assert.equal(state.draft.value.variants[0].strokes[0].points[0][0], 100)
  assert.match(source, /slot name="canvas-overlays"/)
  assert.match(source, /svg\.is-readonly \{ pointer-events: none;/)
  assert.match(source, /is-reference-priority/)
  assert.match(source, /z-index: 30; pointer-events: auto;/)
  assert.equal((descriptor.template.content.match(/<img\s/g) || []).length, 1)
})

test('failed image and active save never allow confirming or editing against a blank preview', async () => {
  for (const reason of ['image', 'save']) {
    const { state, props, events } = editor()
    if (reason === 'image') state.imageFailed.value = true
    else props.disabled = true
    await nextTick()
    assert.equal(events.length, 0)
    assert.equal(state.canEdit.value, false)
  }
})

test('switching to geometry cancels an unfinished endpoint drag without publishing partial positions', async () => {
  const { state, props, events } = editor()
  state.dragStart({ button: 0, pointerId: 1, preventDefault() {} }, 0, 0)
  state.dragMove({ pointerId: 1, clientX: 400, clientY: 320 })
  props.interactive = false
  await nextTick()
  assert.deepEqual(state.draft.value.variants[0].strokes[0].points[0], [100, 200])
  assert.equal(events.length, 0)
})

test('new image URL resets readiness until that exact original image loads successfully', async () => {
  const { state, props, events, imageEvents } = editor()
  props.imageUrl = 'new-original.png'
  await nextTick()
  assert.equal(state.imageLoaded.value, false)
  assert.equal(state.canEdit.value, false)
  assert.equal(events.length, 0)
  state.onImageLoad({ currentTarget: { getAttribute: () => 'old-original.png' } })
  assert.equal(state.imageLoaded.value, false)
  state.onImageLoad({ currentTarget: { getAttribute: () => 'new-original.png' } })
  await nextTick()
  assert.equal(state.canEdit.value, true)
  assert.equal(imageEvents.at(-1).payload, true)
  state.onImageError()
  await nextTick()
  assert.equal(state.imageLoaded.value, false)
  assert.equal(imageEvents.at(-1).payload, false)
})

test('shared delete exposes selection and deletion without changing coordinate or confirmation rules', () => {
  const { state, selectionEvents, exposed, events } = editor()
  assert.deepEqual(Object.keys(exposed).sort(), ['clearSelection', 'deleteSelectedStroke', 'prepareForSave', 'valueForSave'])
  assert.deepEqual(selectionEvents.map((event) => event.payload), [false])
  state.selectStroke(0)
  assert.equal(selectionEvents.at(-1).payload, true)
  exposed.clearSelection()
  assert.equal(selectionEvents.at(-1).payload, false)
  assert.equal(exposed.deleteSelectedStroke(), false)
  assert.equal(events.length, 0)
  state.dragStart({ button: 0, pointerId: 2, preventDefault() {} }, 0, 0)
  assert.equal(selectionEvents.at(-1).payload, true)
  state.dragMove({ pointerId: 2, clientX: 250, clientY: 160 })
  exposed.clearSelection()
  assert.deepEqual(state.draft.value.variants[0].strokes[0].points[0], [100, 200])
  assert.equal(selectionEvents.at(-1).payload, false)
  state.selectStroke(0)
  assert.equal(exposed.deleteSelectedStroke(), true)
  assert.equal(selectionEvents.at(-1).payload, false)
  assert.equal(state.draft.value.comparison_mode, 'manual')
  assert.equal(state.draft.value.teacher_confirmed, false)
  assert.equal(drawingReferenceError(events.at(-1).payload), '')
})

test('saving flushes a pending complete polyline without an extra finish or confirmation click', () => {
  const { state, exposed, events } = editor()
  state.setTool('polyline')
  state.pendingPoints.value = [[400, 400], [600, 600]]
  assert.equal(exposed.prepareForSave(), '')
  assert.equal(state.pendingPoints.value.length, 0)
  assert.deepEqual(events.at(-1).payload.variants[0].strokes.at(-1), { kind: 'polyline', points: [[400, 400], [600, 600]] })
  assert.equal(events.at(-1).payload.teacher_confirmed, false)
  assert.equal(exposed.prepareForSave(), '')
  state.pendingPoints.value = [[700, 700]]
  assert.match(exposed.prepareForSave(), /至少需要/)
  assert.deepEqual(state.pendingPoints.value, [[700, 700]])
})

test('shared selection is cleared on tool, variant, background, image, and editing-mode changes', async () => {
  for (const clear of [
    ({ state }) => state.setTool('point'),
    ({ state }) => state.selectVariant(0),
    ({ state }) => state.canvasDown({ button: 0, clientX: 10, clientY: 10 }),
    ({ props }) => { props.interactive = false },
    ({ props }) => { props.showControls = false },
    ({ props }) => { props.imageUrl = 'different-image.png' },
    ({ props }) => { props.modelValue = { ...fixture(), teacher_confirmed: false } },
  ]) {
    const harness = editor()
    harness.state.selectStroke(0)
    clear(harness)
    await nextTick()
    assert.equal(harness.state.selectedStroke.value, -1)
    assert.equal(harness.selectionEvents.at(-1).payload, false)
  }
})

test('both toolbar layouts keep editing tools without separate confirmation controls', async () => {
  const compiled = compileTemplate({ source: descriptor.template.content, filename: 'DrawingReferenceEditor.vue', id: 'compact-test' })
  const renderCode = compiled.code.replaceAll('from "vue"', `from '${pathToFileURL(require.resolve('vue')).href}'`)
  const { render } = await import(`data:text/javascript;base64,${Buffer.from(renderCode).toString('base64')}`)
  const renderComponent = { ...component, render }
  async function markup(props) {
    return renderToString(createSSRApp({ render: () => h(renderComponent, { modelValue: fixture(), imageUrl: 'synthetic.png', ...props }, {
      'geometry-tools': () => h('button', { 'data-geometry-tool': '' }, '新增豁免框'),
    }) }))
  }
  assert.equal(component.props.compactControls.default, false)
  assert.equal(component.props.sharedDelete.default, false)
  // The fixture line runs from x=100 to x=300; this region stops at x=200.
  const compact = await markup({ compactControls: true, sharedDelete: true, drawingRegions: [[50, 150, 200, 260]] })
  assert.match(compact, /drawing-reference-workbar/)
  assert.match(compact, /data-geometry-tool/)
  assert.match(compact, /drawing-reference-drawing-tools/)
  assert.match(source, /hasCandidate && !canEdit && canRequestEdit/)
  assert.match(source, /编辑标准图/)
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
})

test('switching away from a drawing releases its pinned toolbar', async () => {
  const { state, props } = editor()
  props.canvasVisible = true
  state.workbarPinned.value = true
  state.workbarFixedStyle.value = { position: 'fixed' }
  props.canvasVisible = false
  await nextTick()
  await nextTick()
  assert.equal(state.workbarPinned.value, false)
  assert.equal(state.workbarFixedStyle.value, null)
})

test('compact point and polyline tools request reference mode directly and wait for the parent', async () => {
  for (const tool of ['select', 'point', 'polyline']) {
    const { state, props, editRequests, events } = editor(fixture(), (value) => { value.interactive = true })
    props.interactive = false
    assert.equal(state.canEdit.value, false)
    assert.equal(state.canRequestEdit.value, true)
    assert.equal(await state.requestTool(tool), true)
    assert.deepEqual(editRequests, ['edit-request'])
    assert.equal(state.tool.value, tool)
    assert.equal(state.canEdit.value, true)
    assert.equal(events.length, 0, 'requesting a tool must not mutate the reference')
    assert.equal(await state.requestTool(tool), true)
    assert.equal(editRequests.length, 1, 'already editing needs no parent mode request')
  }
  const refused = editor()
  refused.props.interactive = false
  assert.equal(await refused.state.requestTool('point'), false)
  assert.equal(refused.state.tool.value, 'select')
  assert.equal(refused.state.canEdit.value, false)
})

test('one-click tool activation cannot bypass save, image, or controls locks', async () => {
  for (const lock of [
    ({ props }) => { props.disabled = true },
    ({ props }) => { props.showControls = false },
    ({ state }) => { state.imageLoaded.value = false },
    ({ state }) => { state.imageFailed.value = true },
  ]) {
    const harness = editor(fixture(), (value) => { value.interactive = true })
    harness.props.interactive = false
    lock(harness)
    assert.equal(harness.state.canRequestEdit.value, false)
    assert.equal(await harness.state.requestTool('point'), false)
    assert.deepEqual(harness.editRequests, [])
    assert.equal(harness.state.tool.value, 'select')
  }
  const delayed = editor(fixture(), (props) => { props.interactive = true; props.disabled = true })
  delayed.props.interactive = false
  assert.equal(await delayed.state.requestTool('polyline'), false)
  assert.equal(delayed.state.tool.value, 'select')
  assert.deepEqual(delayed.events, [])
})


const pointer = (x, y, pointerId = 7) => ({ button: 0, clientX: x, clientY: y, pointerId, preventDefault() {} })
function drawDrag(state, tool, start, end) {
  state.setTool(tool)
  state.canvasDown(pointer(...start))
  state.dragMove(pointer(...end))
  state.dragEnd(pointer(...end))
}

test('teacher circle uses real page aspect ratio, saves fixed and supports undo/redo', () => {
  const { state, exposed, events } = editor(null)
  assert.equal(exposed.valueForSave(), null)
  drawDrag(state, 'circle', [500, 400], [600, 400])
  const stroke = state.activeStrokes.value[0]
  assert.deepEqual(stroke, { kind: 'ellipse', center: [500, 500], radii: [100, 125], fill: false })
  assert.equal(exposed.valueForSave().comparison_mode, 'fixed')
  assert.equal(events.at(-1).payload.teacher_confirmed, false)
  state.undo()
  assert.equal(state.draft.value.variants.length, 0)
  state.redo()
  assert.deepEqual(state.activeStrokes.value[0], stroke)
  assert.equal(state.draft.value.teacher_confirmed, false)
})

test('drag tools and filled polygons produce valid bounded teacher geometry', () => {
  for (const tool of ['line', 'arrow', 'rectangle', 'ellipse', 'filledEllipse', 'curve', 'brush']) {
    const { state, exposed } = editor(null)
    drawDrag(state, tool, [300, 240], [600, 480])
    const value = exposed.valueForSave()
    assert.equal(drawingReferenceError(value), '', tool)
    assert.equal(value.comparison_mode, 'fixed', tool)
    assert.equal(state.tool.value, 'select', tool)
  }
  for (const tool of ['polygon', 'fill']) {
    const { state, exposed } = editor(null)
    state.setTool(tool)
    for (const point of [[300, 240], [600, 240]]) state.canvasDown(pointer(...point))
    assert.match(exposed.prepareForSave(), /至少需要 3/)
    state.canvasDown(pointer(600, 480))
    assert.equal(exposed.prepareForSave(), '')
    const stroke = state.activeStrokes.value[0]
    assert.equal(stroke.kind, tool === 'fill' ? 'polygon' : 'polyline')
    assert.equal(stroke.points.length, tool === 'fill' ? 3 : 4)
    assert.equal(drawingReferenceError(exposed.valueForSave()), '')
  }
})

test('cancelling drawing gestures and invalid ellipse edits never publishes partial geometry', () => {
  const { state, exposed, events } = editor(null)
  state.setTool('ellipse')
  state.canvasDown(pointer(300, 240))
  state.dragMove(pointer(600, 480))
  assert.match(exposed.prepareForSave(), /尚在拖动/)
  state.cancelDrag({ pointerId: 7 })
  assert.equal(exposed.valueForSave(), null)
  assert.deepEqual(events, [])
  drawDrag(state, 'ellipse', [300, 240], [600, 480])
  const before = structuredClone(exposed.valueForSave())
  state.dragStart(pointer(450, 360), 0, 0)
  state.dragMove(pointer(0, 0))
  state.dragEnd(pointer(0, 0))
  assert.deepEqual(exposed.valueForSave(), before)
  assert.match(state.error.value, /坐标无效|超出/)
})

test('brush click renders a dot and long strokes keep both endpoints within the contract', () => {
  const { state, exposed } = editor(null)
  drawDrag(state, 'brush', [300, 240], [300, 240])
  assert.deepEqual(state.activeStrokes.value[0], { kind: 'brush', points: [[300, 300]], width: 12 })
  state.setTool('curve')
  state.canvasDown(pointer(100, 160))
  for (let x = 101; x <= 800; x++) state.dragMove(pointer(x, 160 + x % 50))
  state.dragEnd(pointer(800, 160))
  const stroke = state.activeStrokes.value.at(-1)
  assert.ok(stroke.points.length <= 128)
  assert.deepEqual(stroke.points[0], [100, 200])
  assert.deepEqual(stroke.points.at(-1), [800, 200])
  assert.equal(drawingReferenceError(exposed.valueForSave()), '')
})

test('teacher controls offer the full tool set and no model generation or manual confirmation', () => {
  const { state } = editor(null)
  for (const name of ['circle', 'ellipse', 'rectangle', 'arrow', 'polygon', 'fill', 'brush', 'curve']) {
    assert.ok(state.drawingTools.some(({ key }) => key === name))
  }
  assert.doesNotMatch(descriptor.template.content, /生成标准图|@generate|requestConfirm|requestManual/)
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

test('new shape tools produce existing contract strokes and return to selection', () => {
  const { state } = editor(null)
  const down = (x, y) => state.canvasDown({ button: 0, clientX: x, clientY: y, pointerId: 7, preventDefault() {} })
  const drag = (from, to) => {
    down(...from)
    state.dragMove({ pointerId: 7, clientX: to[0], clientY: to[1] })
    state.dragEnd({ pointerId: 7, clientX: to[0], clientY: to[1] })
  }
  const last = () => state.draft.value.variants[0].strokes.at(-1)
  state.setTool('filledCircle')
  drag([500, 400], [580, 400])
  assert.deepEqual(last(), { kind: 'ellipse', center: [500, 500], radii: [80, 100], fill: true })
  assert.equal(state.tool.value, 'select')
  state.setTool('filledRectangle')
  drag([200, 160], [300, 240])
  assert.deepEqual(last(), { kind: 'polygon', points: [[200, 200], [300, 200], [300, 300], [200, 300]] })
  state.setTool('arc')
  down(400, 400); down(500, 300); down(600, 400)
  assert.equal(last().kind, 'polyline')
  assert.equal(state.tool.value, 'select')
  state.setTool('sector')
  down(400, 400); down(500, 300); down(600, 400)
  assert.equal(last().kind, 'polygon')
  assert.deepEqual(last().points[0], [500, 500])
  state.setTool('spline')
  down(100, 600); down(300, 200); down(500, 600)
  state.finishPolyline()
  assert.equal(last().kind, 'polyline')
  assert.ok(last().points.length > 3)
  assert.equal(drawingReferenceError(state.draft.value), '')
  assert.equal(state.draft.value.comparison_mode, 'fixed')
})

test('a circle clamped to the page edge still satisfies the containment contract', () => {
  const { state } = editor(null)
  state.setTool('circle')
  state.canvasDown({ button: 0, clientX: 103, clientY: 400, pointerId: 8, preventDefault() {} })
  state.dragEnd({ pointerId: 8, clientX: 900, clientY: 400 })
  const stroke = state.draft.value.variants[0].strokes[0]
  assert.equal(stroke.kind, 'ellipse')
  assert.ok(stroke.center[0] - stroke.radii[0] >= 0)
  assert.equal(state.error.value, '')
})
