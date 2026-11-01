<template>
  <section class="drawing-reference-editor" :class="{ 'is-shared-canvas': !!canvasClass, 'is-compact': compactControls }" aria-label="标准作图参考编辑器">
    <div v-if="workbarPinned" ref="workbarGapEl" class="drawing-reference-workbar-gap" aria-hidden="true" :style="{ height: `${workbarHeight}px` }"></div>
    <div v-if="compactControls || $slots['geometry-tools']" ref="workbarEl" class="drawing-reference-workbar" :style="workbarFixedStyle">
      <div v-if="$slots['geometry-tools'] && canvasVisible" class="drawing-reference-geometry-tools"><slot name="geometry-tools" /></div>
      <div v-if="compactControls && showControls" class="drawing-reference-drawing-tools">
        <span class="drawing-reference-tools-label">标准图</span>
        <label v-if="hasCandidate && draft.variants.length > 1" class="drawing-reference-variant-picker">
          <span>方案</span>
          <select :value="activeVariant" :disabled="!canRequestEdit" aria-label="选择标准作图方案" @change="selectVariant(Number($event.target.value))">
            <option v-for="(_, index) in draft.variants" :key="index" :value="index">{{ index + 1 }}</option>
          </select>
        </label>
        <button
          v-if="hasCandidate && !canEdit && canRequestEdit"
          type="button"
          class="drawing-reference-edit"
          @click="$emit('edit-request')"
        >编辑标准图</button>
        <fieldset :disabled="!canRequestEdit" class="drawing-reference-controls drawing-reference-toolbar">
          <button v-for="item in drawingTools" :key="item.key" type="button" :aria-pressed="canEdit && tool === item.key" @click="requestTool(item.key)">{{ item.label }}</button>
          <label v-if="tool === 'brush'">笔宽 <input v-model.number="brushWidth" type="range" min="2" max="60" aria-label="涂抹笔宽" /></label>
          <button v-if="pendingPoints.length" type="button" :disabled="!canEdit || pendingPoints.length < minimumPoints" @click="finishPolyline">完成图形</button>
          <button type="button" :disabled="!canEdit || (!history.length && !pendingPoints.length)" @click="undo">撤销</button>
          <button type="button" :disabled="!canEdit || !redoHistory.length" @click="redo">重做</button>
          <button type="button" :disabled="!canEdit || !activeStrokes.length || draft.variants.length >= 4" @click="addVariant">复制方案</button>
          <button type="button" :disabled="!canEdit || !draft.variants.length" @click="removeVariant">删除方案</button>
          <button v-if="!sharedDelete" type="button" :disabled="!canEdit || selectedStroke < 0" @click="deleteStroke">删除笔画</button>
        </fieldset>
      </div>
    </div>
    <template v-if="showControls">
    <p v-if="!compactControls" class="drawing-reference-note">青色为教师手绘标准，坐标固定在整张模板上；不随学生图形移动、缩放或贴合。每个方案必须是一个完整合法答案，多个方案不会叠加评分。标准图可留空；留空时按题干和学生作图区判断，手绘后按标准图比较。</p>
    <div class="drawing-reference-generation" aria-live="polite">
      <span v-if="draft.variants.length">已有 {{ draft.variants.length }} 个教师标准图方案，随模板保存。</span>
      <span v-else>标准图留空也可保存；批改时按题干和学生作图区判断。</span>
    </div>
    <p v-if="regionWarning" class="drawing-reference-error" role="alert">范围待核对：{{ regionWarning }}</p>
    <!-- 方案工具只负责编辑；全部参考随“保存模板”一起确认。 -->
    <div v-if="!compactControls" class="drawing-reference-settings">
    <fieldset :disabled="!canEdit" class="drawing-reference-controls">
    <div class="drawing-reference-toolbar">
      <label>完整方案
        <select :value="activeVariant" :disabled="!draft.variants.length" @change="selectVariant(Number($event.target.value))">
          <option v-if="!draft.variants.length" :value="0">待绘制</option>
          <option v-for="(_, index) in draft.variants" :key="index" :value="index">方案 {{ index + 1 }}</option>
        </select>
      </label>
      <button type="button" :disabled="!draft.variants.length || draft.variants.length >= 4" @click="addVariant">新增完整方案（复制当前）</button>
      <button type="button" :disabled="!draft.variants.length" @click="removeVariant">删除当前方案</button>
    </div>
    <div v-if="!compactControls" class="drawing-reference-toolbar">
      <button v-for="item in drawingTools" :key="item.key" type="button" :aria-pressed="tool === item.key" @click="setTool(item.key)">{{ item.label }}</button>
      <label v-if="tool === 'brush'">笔宽 <input v-model.number="brushWidth" type="range" min="2" max="60" aria-label="涂抹笔宽" /></label>
      <button type="button" :disabled="pendingPoints.length < minimumPoints" @click="finishPolyline">完成图形</button>
      <button type="button" :disabled="!history.length && !pendingPoints.length" @click="undo">撤销</button>
      <button type="button" :disabled="!redoHistory.length" @click="redo">重做</button>
      <button v-if="!sharedDelete" type="button" :disabled="selectedStroke < 0" @click="deleteStroke">删除所选笔画</button>
    </div>
    <p class="drawing-reference-tip">{{ toolHint }}</p>
    </fieldset>
    </div>
    <p v-if="compactControls && canEdit && tool !== 'select'" class="drawing-reference-tip">{{ toolHint }}</p>
    <p v-if="compactControls && error" class="drawing-reference-error" role="alert">{{ error }}</p>
    </template>
    <div v-if="imageUrl && canvasVisible && $slots['geometry-labels']" class="drawing-reference-labels"><slot name="geometry-labels" /></div>
    <div v-if="imageUrl" v-show="canvasVisible" class="drawing-reference-canvas" :class="canvasClass">
      <img :src="imageUrl" alt="教师模板原图" draggable="false" @load="onImageLoad" @error="onImageError" />
      <svg ref="canvas" :class="{ 'is-readonly': !canEdit || !showControls, 'is-select-mode': tool === 'select', 'is-reference-priority': priorityEditing || (canEdit && tool !== 'select') }" viewBox="0 0 1000 1000" preserveAspectRatio="none" role="img" aria-label="教师标准图画布，可画点、线、圆、图形并填色" @pointerdown="canvasDown" @pointermove="dragMove" @pointerup="dragEnd" @pointercancel="cancelDrag">
        <rect v-for="(box, index) in validDrawingRegions" :key="`region-${index}`" :x="box[0]" :y="box[1]" :width="box[2] - box[0]" :height="box[3] - box[1]" class="drawing-region" />
        <rect v-for="(box, index) in draft.context_regions" :key="`context-${index}`" :x="box[0]" :y="box[1]" :width="box[2] - box[0]" :height="box[3] - box[1]" class="context-region" />
        <g v-for="(stroke, strokeIndex) in visibleStrokes" :key="strokeIndex" :class="{ 'is-preview': strokeIndex >= activeStrokes.length }" @pointerdown.stop="strokeDown($event, strokeIndex)">
          <polyline v-if="stroke.kind === 'polyline'" :points="svgPoints(stroke.points)" class="reference-stroke" :class="{ selected: selectedStroke === strokeIndex }" />
          <ellipse v-else-if="stroke.kind === 'ellipse'" :cx="stroke.center[0]" :cy="stroke.center[1]" :rx="stroke.radii[0]" :ry="stroke.radii[1]" class="reference-stroke" :class="{ 'reference-area': stroke.fill, selected: selectedStroke === strokeIndex }" />
          <polygon v-else-if="stroke.kind === 'polygon'" :points="svgPoints(stroke.points)" class="reference-area" :class="{ selected: selectedStroke === strokeIndex }" />
          <g v-else-if="stroke.kind === 'brush'" :transform="brushTransform" :class="{ selected: selectedStroke === strokeIndex }">
            <circle v-if="stroke.points.length === 1" :cx="stroke.points[0][0] * imageSize[0] / 1000" :cy="stroke.points[0][1] * imageSize[1] / 1000" :r="stroke.width * Math.min(...imageSize) / 2000" class="reference-area" />
            <polyline v-else :points="brushPoints(stroke.points)" :style="{ strokeWidth: stroke.width * Math.min(...imageSize) / 1000 }" class="reference-brush" />
          </g>
          <circle v-if="stroke.kind === 'point'" :cx="stroke.point[0]" :cy="stroke.point[1]" r="5" class="reference-point" :class="{ selected: selectedStroke === strokeIndex }" @pointerdown.stop="dragStart($event, strokeIndex, 0)" />
          <template v-if="stroke.kind !== 'point' && selectedStroke === strokeIndex && tool === 'select'">
            <circle v-for="(point, pointIndex) in strokePoints(stroke)" :key="pointIndex" :cx="point[0]" :cy="point[1]" r="4" class="reference-point selected" @pointerdown.stop="dragStart($event, strokeIndex, pointIndex)" />
          </template>
        </g>
        <polyline v-if="pendingPoints.length > 1" :points="svgPoints(pendingPoints)" class="pending-stroke" />
        <circle v-for="(point, index) in pendingPoints" :key="`pending-${index}`" :cx="point[0]" :cy="point[1]" r="4" class="pending-point" />
      </svg>
      <slot name="canvas-overlays" />
    </div>
    <p v-else class="drawing-reference-error">缺少模板原图，无法核对参考坐标。</p>
    <p v-if="imageUrl && !imageLoaded && !imageFailed" class="drawing-reference-tip" role="status">模板原图加载中，加载完成后才能核对标准图。</p>
    <p v-if="imageFailed" class="drawing-reference-error" role="alert">模板原图加载失败，不能在空白底图上确认标准图，请重新加载预览。</p>
    <template v-if="showControls && !compactControls">
    <p v-if="error" class="drawing-reference-error" role="alert">{{ error }}</p>
    <p class="drawing-reference-tip">标准图随“保存模板”统一确认，无需单独勾选。没有标准图时不需要点额外的复核按钮，保存后自动走题干+作图区的 Flash 判定。</p>
    <p v-if="hasCandidate && draft.comparison_mode === 'manual'" class="drawing-reference-tip">自定义作图不会按固定坐标比较，批改时直接把题干和作图区交给 Flash。</p>
    </template>
  </section>
</template>

<script>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const clone = (value) => JSON.parse(JSON.stringify(value))

export function emptyDrawingReference() {
  return { version: 'drawing-reference-v1', coordinate_space: 'normalized_1000_v1', comparison_mode: 'manual', variants: [], context_regions: [], teacher_confirmed: false }
}

function validPoint(point) {
  return Array.isArray(point) && point.length === 2 && point.every((value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1000)
}

const drawingTools = [
  ['select', '选择／拖动端点'], ['point', '点'], ['line', '直线'], ['arrow', '箭头'],
  ['polyline', '折线'], ['spline', '平滑曲线'], ['curve', '自由曲线'], ['arc', '圆弧'],
  ['circle', '圆'], ['ellipse', '椭圆'], ['rectangle', '矩形'], ['polygon', '多边形'],
  ['fill', '区域填色'], ['filledRectangle', '矩形填色'], ['filledCircle', '圆填色'],
  ['filledEllipse', '椭圆填色'], ['sector', '扇形填色'], ['brush', '涂抹'],
].map(([key, label]) => ({ key, label }))
const clickTools = new Set(['polyline', 'spline', 'polygon', 'fill', 'arc', 'sector'])
const threePointTools = new Set(['arc', 'sector'])
const samePoint = (a, b) => a?.every((value, axis) => value === b[axis])
const roundNormalized = (value) => Math.round(Math.max(0, Math.min(1000, value)) * 10) / 10
const toNormalized = (point, [width, height]) => [roundNormalized(point[0] / width * 1000), roundNormalized(point[1] / height * 1000)]
const toPixels = (point, [width, height]) => [point[0] * width / 1000, point[1] * height / 1000]

function withoutRepeats(points) {
  return points.filter((point, index) => index === 0 || !samePoint(point, points[index - 1]))
}

/** Circle through start, any point on the arc, and end (pixel space keeps it round). */
export function arcThroughPoints(points, imageSize = [1000, 1000]) {
  const [a, b, c] = points.map((point) => toPixels(point, imageSize))
  const d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
  if (Math.abs(d) < 1e-6) return null
  const square = (p) => p[0] * p[0] + p[1] * p[1]
  const center = [
    (square(a) * (b[1] - c[1]) + square(b) * (c[1] - a[1]) + square(c) * (a[1] - b[1])) / d,
    (square(a) * (c[0] - b[0]) + square(b) * (a[0] - c[0]) + square(c) * (b[0] - a[0])) / d,
  ]
  const radius = Math.hypot(a[0] - center[0], a[1] - center[1])
  const angle = (p) => Math.atan2(p[1] - center[1], p[0] - center[0])
  const turn = (value) => ((value % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI)
  const start = angle(a)
  const span = turn(angle(c) - start)
  const sweep = turn(angle(b) - start) <= span ? span : span - 2 * Math.PI
  const count = Math.min(126, Math.max(8, Math.ceil(Math.abs(sweep) * radius / 4)))
  const arc = Array.from({ length: count + 1 }, (_, index) => {
    const value = start + sweep * index / count
    return [center[0] + radius * Math.cos(value), center[1] + radius * Math.sin(value)]
  })
  const [width, height] = imageSize
  const outside = (p) => p[0] < 0 || p[1] < 0 || p[0] > width || p[1] > height
  // Never clamp an arc onto the page edge; a sector also needs its center.
  if (arc.some(outside)) return null
  return { center: outside(center) ? null : toNormalized(center, imageSize), points: withoutRepeats(arc.map((p) => toNormalized(p, imageSize))) }
}

/** Smooth Catmull-Rom curve through every clicked point, bounded to 128 points. */
export function splineThroughPoints(points, imageSize = [1000, 1000]) {
  const pixels = points.map((point) => toPixels(point, imageSize))
  if (pixels.length < 3) return withoutRepeats(points.map((point) => [...point]))
  const lengths = pixels.slice(1).map((point, index) => Math.hypot(point[0] - pixels[index][0], point[1] - pixels[index][1]))
  const total = lengths.reduce((sum, value) => sum + value, 0) || 1
  const budget = 127 - lengths.length
  const result = []
  for (let index = 0; index < lengths.length; index += 1) {
    const p0 = pixels[Math.max(0, index - 1)]
    const p1 = pixels[index]
    const p2 = pixels[index + 1]
    const p3 = pixels[Math.min(pixels.length - 1, index + 2)]
    const steps = 1 + Math.max(0, Math.min(Math.ceil(lengths[index] / 6), Math.floor(budget * lengths[index] / total)))
    for (let step = 0; step < steps; step += 1) {
      const t = step / steps
      const t2 = t * t
      const t3 = t2 * t
      result.push([0, 1].map((axis) => 0.5 * ((2 * p1[axis]) + (-p0[axis] + p2[axis]) * t
        + (2 * p0[axis] - 5 * p1[axis] + 4 * p2[axis] - p3[axis]) * t2
        + (-p0[axis] + 3 * p1[axis] - 3 * p2[axis] + p3[axis]) * t3)))
    }
  }
  result.push(pixels.at(-1))
  return withoutRepeats(result.map((point) => toNormalized(point, imageSize)))
}

function densify(points, closed = false) {
  const path = closed ? [...points, points[0]] : points
  const samples = [path[0]]
  for (let index = 1; index < path.length; index += 1) {
    const [a, b] = [path[index - 1], path[index]]
    const count = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / 2))
    for (let step = 1; step <= count; step += 1) samples.push([a[0] + (b[0] - a[0]) * step / count, a[1] + (b[1] - a[1]) * step / count])
  }
  return samples
}

/** Mirror the server's rendered containment closely enough to warn while drawing. */
export function drawingReferenceRegionWarning(reference, regions = [], imageSize = [1000, 1000]) {
  const variants = Array.isArray(reference?.variants) ? reference.variants : []
  if (!variants.length) return ''
  if (!regions.length) return '该题尚未框选作图区域；请先框选作图区，否则保存模板时会被拒绝。'
  const [width, height] = imageSize
  const scale = Math.min(width / 1000, height / 820)
  const margin = (pixels) => [pixels / 2 / width * 1000, pixels / 2 / height * 1000]
  const line = margin(Math.max(1, Math.round(4.5 * scale)))
  const inside = ([x, y], [mx, my]) => regions.some((box) => x - mx >= box[0] - 0.5 && x + mx <= box[2] + 0.5
    && y - my >= box[1] - 0.5 && y + my <= box[3] + 0.5)
  for (const [index, variant] of variants.entries()) {
    for (const stroke of variant?.strokes || []) {
      let samples = []
      let extent = [0, 0]
      if (stroke.kind === 'point') {
        samples = [stroke.point]
        extent = margin(2 * Math.max(1, Math.round(5.5 * scale)))
      } else if (stroke.kind === 'ellipse') {
        samples = Array.from({ length: 72 }, (_, step) => [stroke.center[0] + stroke.radii[0] * Math.cos(step * Math.PI / 36),
          stroke.center[1] + stroke.radii[1] * Math.sin(step * Math.PI / 36)])
        extent = stroke.fill ? [0, 0] : line
      } else if (stroke.kind === 'polygon') {
        samples = densify(stroke.points, true)
      } else if (Array.isArray(stroke.points)) {
        samples = densify(stroke.points)
        extent = stroke.kind === 'brush' ? margin(Math.max(1, Math.round(stroke.width * Math.min(width, height) / 1000))) : line
      }
      if (samples.some((point) => !inside(point, extent))) {
        return `方案 ${index + 1} 有笔画超出该题作图区域，保存模板时会被拒绝；请调整标准图或扩大作图区。`
      }
    }
  }
  return ''
}

function strokeError(stroke) {
  const keys = Object.keys(stroke || {}).sort().join(',')
  if (stroke?.kind === 'point' && keys === 'kind,point' && validPoint(stroke.point)) return ''
  if (stroke?.kind === 'ellipse' && keys === 'center,fill,kind,radii'
    && validPoint(stroke.center) && validPoint(stroke.radii) && typeof stroke.fill === 'boolean'
    && stroke.radii.every((radius, axis) => radius > 0 && stroke.center[axis] - radius >= 0 && stroke.center[axis] + radius <= 1000)) return ''
  const minimum = { polyline: 2, polygon: 3, brush: 1 }[stroke?.kind]
  if (!minimum || keys !== (stroke.kind === 'brush' ? 'kind,points,width' : 'kind,points')
    || !Array.isArray(stroke.points) || stroke.points.length < minimum || stroke.points.length > 128
    || !stroke.points.every(validPoint)) return '图形坐标无效或超出画布，请调整后重试。'
  if (stroke.points.some((point, index) => index > 0 && samePoint(point, stroke.points[index - 1]))) return '笔画不能含重合的相邻端点。'
  if (stroke.kind === 'polygon') {
    const area = stroke.points.reduce((sum, a, index) => {
      const b = stroke.points[(index + 1) % stroke.points.length]
      return sum + a[0] * b[1] - b[0] * a[1]
    }, 0)
    if (Math.abs(area) < 0.01) return '填色区域必须有面积。'
  }
  if (stroke.kind === 'brush' && (typeof stroke.width !== 'number' || !Number.isFinite(stroke.width) || stroke.width < 1 || stroke.width > 80)) return '涂抹笔宽无效。'
  return ''
}

export function drawingReferenceError(value) {
  if (!value || value.version !== 'drawing-reference-v1' || value.coordinate_space !== 'normalized_1000_v1') return '参考版本或坐标格式无效，请重新提供候选。'
  if (Object.keys(value).some((key) => !['version', 'coordinate_space', 'comparison_mode', 'variants', 'context_regions', 'teacher_confirmed', 'reference_hash'].includes(key))) return '参考含不支持的字段。'
  if (!['fixed', 'manual'].includes(value.comparison_mode)) return '请选择有效比较方式。'
  if (!Array.isArray(value.variants) || value.variants.length > 4 || (value.comparison_mode === 'fixed' && !value.variants.length)) return '固定模式需至少一个完整方案，最多四个。'
  for (const variant of value.variants) {
    if (!variant || Object.keys(variant).join(',') !== 'strokes' || !Array.isArray(variant.strokes) || !variant.strokes.length || variant.strokes.length > 64) return '每个完整方案须含 1–64 笔。'
    for (const stroke of variant.strokes) {
      const invalid = strokeError(stroke)
      if (invalid) return invalid
    }
  }
  if (!Array.isArray(value.context_regions) || value.context_regions.length > 64 || value.context_regions.some((box) => !Array.isArray(box) || box.length !== 4 || !validPoint(box.slice(0, 2)) || !validPoint(box.slice(2)) || box[0] >= box[2] || box[1] >= box[3])) return '图外参照范围无效。'
  return ''
}

export function editableDrawingReference(value) {
  if (value == null) return emptyDrawingReference()
  const result = clone(value)
  delete result.reference_hash // Server recomputes this; browser never signs geometry.
  result.teacher_confirmed = value.teacher_confirmed === true
  return result
}

export function changedDrawingReference(value) {
  return { ...editableDrawingReference(value), teacher_confirmed: false }
}

export function normalizedDrawingPoint(event, rect) {
  if (!(rect.width > 0 && rect.height > 0)) return null
  return [event.clientX - rect.left, event.clientY - rect.top].map((value, index) => Math.round(Math.max(0, Math.min(1000, value / (index ? rect.height : rect.width) * 1000)) * 10) / 10)
}

export default {
  name: 'DrawingReferenceEditor',
  props: {
    modelValue: { type: Object, default: null },
    reference: { type: Object, default: null },
    imageUrl: { type: String, default: '' },
    drawingRegions: { type: Array, default: () => [] },
    canvasClass: { type: String, default: '' },
    canvasVisible: { type: Boolean, default: true },
    // The parent uses this when the current page is in standard-reference
    // mode. Geometry overlays remain available in layout mode, while the
    // standard drawing surface owns the highest interaction layer here.
    priorityEditing: { type: Boolean, default: false },
    showControls: { type: Boolean, default: true },
    compactControls: { type: Boolean, default: false },
    sharedDelete: { type: Boolean, default: false },
    interactive: { type: Boolean, default: true },
    disabled: { type: Boolean, default: false },
  },
  emits: ['update:modelValue', 'image-ready', 'selection-change', 'edit-request'],
  setup(props, { emit, expose }) {
    const draft = ref(emptyDrawingReference())
    const canvas = ref(null)
    const activeVariant = ref(0)
    const selectedStroke = ref(-1)
    const tool = ref('select')
    const pendingPoints = ref([])
    const history = ref([])
    const redoHistory = ref([])
    const brushWidth = ref(12)
    const imageSize = ref([1000, 1000])
    const draftStroke = ref(null)
    const error = ref('')
    const hasCandidate = ref(false)
    const imageFailed = ref(false)
    const imageLoaded = ref(false)
    const canEdit = computed(() => props.interactive !== false && !props.disabled && imageLoaded.value && !imageFailed.value)
    const canRequestEdit = computed(() => props.showControls !== false && !props.disabled && imageLoaded.value && !imageFailed.value)
    let lastEmitted = ''
    let drag = null
    let drawGesture = null
    const activeStrokes = computed(() => draft.value.variants?.[activeVariant.value]?.strokes || [])
    const visibleStrokes = computed(() => [...activeStrokes.value, ...(draftStroke.value ? [draftStroke.value] : [])])
    const minimumPoints = computed(() => ['polyline', 'spline'].includes(tool.value) ? 2 : 3)
    const brushTransform = computed(() => `scale(${1000 / imageSize.value[0]} ${1000 / imageSize.value[1]})`)
    const toolHint = computed(() => {
      if (tool.value === 'select') return '点击选择笔画，拖动控制点调整；可删除、撤销或重做。'
      if (threePointTools.has(tool.value)) return '依次点击圆弧起点、弧上任一点和终点；扇形填色会自动连到圆心。'
      if (tool.value === 'spline') return '依次点击曲线经过的关键点（如顶点、交点），再点“完成图形”，自动连成平滑曲线。'
      if (clickTools.has(tool.value)) return `逐点点击，再点“完成图形”（至少 ${minimumPoints.value} 点）。区域填色会自动闭合。`
      if (tool.value === 'point') return '在需要标记的位置点击。'
      if (['circle', 'filledCircle'].includes(tool.value)) return '从圆心拖到圆周；圆的比例按原图保持。'
      if (['brush', 'curve'].includes(tool.value)) return '按住并拖动绘制，松开完成；涂抹可调整笔宽。'
      return '按住并拖动绘制，松开完成。椭圆、矩形按对角两点确定范围。'
    })
    const validDrawingRegions = computed(() => props.drawingRegions.filter((box) => Array.isArray(box) && box.length === 4 && validPoint(box.slice(0, 2)) && validPoint(box.slice(2)) && box[0] < box[2] && box[1] < box[3]))
    const regionWarning = computed(() => drawingReferenceRegionWarning(draft.value, validDrawingRegions.value, imageSize.value))
    watch(selectedStroke, (index) => emit('selection-change', index >= 0), { immediate: true, flush: 'sync' })

    watch(() => props.modelValue ?? props.reference, (value) => {
      const next = editableDrawingReference(value)
      if (value != null && JSON.stringify(next) === lastEmitted) return
      const invalid = drawingReferenceError(next)
      cancelDrag()
      draft.value = invalid ? emptyDrawingReference() : next
      hasCandidate.value = value != null && !invalid
      error.value = invalid
      activeVariant.value = 0
      selectedStroke.value = -1
      pendingPoints.value = []
      history.value = []
      redoHistory.value = []
      drag = null
    }, { immediate: true, deep: true })
    watch(() => props.imageUrl, () => { imageFailed.value = false; imageLoaded.value = false }, { flush: 'sync' })
    watch(imageLoaded, (value) => emit('image-ready', value), { immediate: true, flush: 'sync' })
    watch(() => props.showControls, (value) => { if (value === false) clearSelection() }, { flush: 'sync' })
    watch(canEdit, (value) => {
      if (!value) {
        cancelDrag()
        pendingPoints.value = []
        tool.value = 'select'
        clearSelection()
      }
    }, { flush: 'sync' })

    function publish() {
      const invalid = drawingReferenceError(draft.value)
      if (invalid) { error.value = invalid; return false }
      const value = editableDrawingReference(draft.value)
      lastEmitted = JSON.stringify(value)
      emit('update:modelValue', value)
      return true
    }
    function onImageLoad(event) {
      if (event?.currentTarget?.getAttribute('src') !== props.imageUrl) return
      imageFailed.value = false
      if (event.currentTarget.naturalWidth > 0 && event.currentTarget.naturalHeight > 0) imageSize.value = [event.currentTarget.naturalWidth, event.currentTarget.naturalHeight]
      imageLoaded.value = true
    }
    function onImageError() { imageFailed.value = true; imageLoaded.value = false }
    function edit(mutator) {
      if (!canEdit.value) return false
      const previous = clone(draft.value)
      const next = changedDrawingReference(previous)
      mutator(next)
      const invalid = drawingReferenceError(next)
      if (invalid) { error.value = invalid; return false }
      history.value.push(previous)
      redoHistory.value = []
      if (history.value.length > 50) history.value.shift()
      draft.value = next
      hasCandidate.value = true
      error.value = ''
      publish()
      return true
    }
    function invalidateConfirmation() {
      if (draft.value.teacher_confirmed) {
        draft.value = changedDrawingReference(draft.value)
        publish()
      }
    }
    function setTool(value) {
      if (!canEdit.value || !drawingTools.some((item) => item.key === value)) return
      cancelDrag()
      tool.value = value; pendingPoints.value = []; selectedStroke.value = -1
    }
    async function requestTool(value) {
      if (!drawingTools.some((item) => item.key === value) || !canRequestEdit.value) return false
      if (props.interactive === false) {
        emit('edit-request')
        // The parent owns the geometry/reference mode. Wait for its prop update,
        // then recheck locks so a delayed click cannot bypass a save.
        await nextTick()
      }
      if (!canRequestEdit.value || !canEdit.value) return false
      setTool(value)
      return true
    }
    function selectVariant(index) { cancelDrag(); activeVariant.value = index; pendingPoints.value = []; selectedStroke.value = -1 }
    function addVariant() {
      if (!activeStrokes.value.length || draft.value.variants.length >= 4) return
      if (edit((next) => { next.variants.push(clone(next.variants[activeVariant.value])) })) selectVariant(draft.value.variants.length - 1)
    }
    function removeVariant() {
      if (!draft.value.variants.length) return
      edit((next) => { next.variants.splice(activeVariant.value, 1); if (!next.variants.length) next.comparison_mode = 'manual' })
      selectVariant(Math.max(0, Math.min(activeVariant.value, draft.value.variants.length - 1)))
    }
    function addStroke(stroke) {
      return edit((next) => {
        if (!next.variants.length) next.variants.push({ strokes: [] })
        next.variants[activeVariant.value].strokes.push(stroke)
        next.comparison_mode = 'fixed'
      })
    }
    function canvasDown(event) {
      if (!canEdit.value || props.showControls === false) return
      if (event.button !== 0 || tool.value === 'select') { selectedStroke.value = -1; return }
      const point = normalizedDrawingPoint(event, canvas.value.getBoundingClientRect())
      if (!point) return
      if (tool.value === 'point') { if (addStroke({ kind: 'point', point })) tool.value = 'select' }
      else if (clickTools.has(tool.value)) {
        if (pendingPoints.value.length >= (tool.value === 'polygon' ? 127 : 128)) { error.value = '每个图形最多 128 个端点。'; return }
        if (pendingPoints.value.at(-1)?.every((value, axis) => value === point[axis])) return
        invalidateConfirmation()
        pendingPoints.value.push(point)
        if (threePointTools.has(tool.value) && pendingPoints.value.length === 3) finishPolyline()
      } else {
        event.preventDefault()
        drawGesture = { start: point, points: [point], pointerId: event.pointerId }
        canvas.value.setPointerCapture(event.pointerId)
        updateGesture(point)
      }
    }
    function updateGesture(point) {
      const start = drawGesture.start
      const [width, height] = imageSize.value
      const delta = [(point[0] - start[0]) * width / 1000, (point[1] - start[1]) * height / 1000]
      const length = Math.hypot(...delta)
      if (['brush', 'curve'].includes(tool.value)) {
        const points = drawGesture.points
        const last = points.at(-1)
        if (Math.hypot((point[0] - last[0]) * width / 1000, (point[1] - last[1]) * height / 1000) >= 2) {
          // Keep a bounded, ordered path while preserving both ends.
          if (points.length >= 128) drawGesture.points = points.filter((_, index) => index % 2 === 0 || index === points.length - 1)
          drawGesture.points.push(point)
        }
        draftStroke.value = tool.value === 'brush'
          ? { kind: 'brush', points: clone(drawGesture.points), width: brushWidth.value }
          : drawGesture.points.length > 1 ? { kind: 'polyline', points: clone(drawGesture.points) } : null
        return
      }
      if (length < 1) { draftStroke.value = null; return }
      if (['circle', 'filledCircle', 'ellipse', 'filledEllipse'].includes(tool.value)) {
        const round = ['circle', 'filledCircle'].includes(tool.value)
        const center = round ? start : start.map((value, axis) => (value + point[axis]) / 2)
        const radius = Math.min(length, start[0] * width / 1000, (1000 - start[0]) * width / 1000, start[1] * height / 1000, (1000 - start[1]) * height / 1000)
        // Round down so a circle clamped to the page edge still fits exactly.
        const radii = (round ? [radius / width * 1000, radius / height * 1000] : start.map((value, axis) => Math.abs(value - point[axis]) / 2))
          .map((value) => Math.floor(value * 100) / 100)
        draftStroke.value = radii.every((value) => value > 0) ? { kind: 'ellipse', center: [...center], radii, fill: ['filledCircle', 'filledEllipse'].includes(tool.value) } : null
      } else if (tool.value === 'rectangle') {
        draftStroke.value = delta.every((value) => value !== 0) ? { kind: 'polyline', points: [start, [point[0], start[1]], point, [start[0], point[1]], start] } : null
      } else if (tool.value === 'filledRectangle') {
        draftStroke.value = delta.every((value) => value !== 0) ? { kind: 'polygon', points: [start, [point[0], start[1]], point, [start[0], point[1]]] } : null
      } else if (tool.value === 'arrow') {
        const head = Math.min(length * 0.3, Math.min(width, height) * 0.025)
        const wing = (sign) => [
          point[0] + (-delta[0] * 0.85 + sign * delta[1] * 0.53) / length * head / width * 1000,
          point[1] + (-delta[1] * 0.85 - sign * delta[0] * 0.53) / length * head / height * 1000,
        ].map((value) => Math.max(0, Math.min(1000, value)))
        draftStroke.value = { kind: 'polyline', points: [start, point, wing(1), point, wing(-1)] }
      } else draftStroke.value = { kind: 'polyline', points: [start, point] }
    }
    function finishPolyline() {
      if (pendingPoints.value.length < minimumPoints.value) return
      const points = clone(pendingPoints.value)
      if (tool.value === 'polygon' && !samePoint(points[0], points.at(-1))) points.push([...points[0]])
      let stroke = { kind: tool.value === 'fill' ? 'polygon' : 'polyline', points }
      if (tool.value === 'spline') stroke = { kind: 'polyline', points: splineThroughPoints(points, imageSize.value) }
      if (threePointTools.has(tool.value)) {
        const arc = arcThroughPoints(points, imageSize.value)
        if (!arc || (tool.value === 'sector' && !arc.center)) {
          error.value = tool.value === 'sector' ? '三点不能共线，圆心和圆弧须在模板内，请重新点击。' : '三点不能共线，圆弧须在模板内，请重新点击。'
          pendingPoints.value = []
          return
        }
        stroke = tool.value === 'arc' ? { kind: 'polyline', points: arc.points } : { kind: 'polygon', points: [arc.center, ...arc.points] }
      }
      if (addStroke(stroke)) {
        pendingPoints.value = []
        // 教师标注：一次标记完成后指针回到选择状态（点工具同理），避免连续误画。
        tool.value = 'select'
      }
    }
    function prepareForSave() {
      if (drawGesture || drag) return '当前图形尚在拖动，请松开后再保存模板'
      if (!pendingPoints.value.length) return ''
      if (pendingPoints.value.length < minimumPoints.value) return `当前图形至少需要 ${minimumPoints.value} 个端点，请补齐或撤销`
      if (!canEdit.value) return '当前图形尚未完成，请恢复该题编辑后重试保存模板'
      finishPolyline()
      return pendingPoints.value.length ? error.value || '折线未保存，请检查端点' : ''
    }
    function valueForSave() {
      // The parent normally receives every edit through v-model.  Return the
      // current editor draft as well so a save started in the same event loop
      // cannot submit the previous prop value. A blank reference is optional.
      return hasCandidate.value ? editableDrawingReference(draft.value) : null
    }
    function clearSelection() {
      cancelDrag()
      selectedStroke.value = -1
    }
    function selectStroke(index) {
      if (!canEdit.value || props.showControls === false || !Number.isInteger(index) || !activeStrokes.value[index]) return
      selectedStroke.value = index
    }
    function strokeDown(event, index) {
      if (tool.value !== 'select') { canvasDown(event); return }
      selectStroke(index)
    }
    function deleteStroke() {
      if (!canEdit.value || props.showControls === false || selectedStroke.value < 0 || !activeStrokes.value[selectedStroke.value]) return false
      cancelDrag()
      const deleted = edit((next) => {
        next.variants[activeVariant.value].strokes.splice(selectedStroke.value, 1)
        if (!next.variants[activeVariant.value].strokes.length) next.variants.splice(activeVariant.value, 1)
        if (!next.variants.length) next.comparison_mode = 'manual'
      })
      selectVariant(Math.max(0, Math.min(activeVariant.value, draft.value.variants.length - 1)))
      return deleted
    }
    function dragStart(event, strokeIndex, pointIndex) {
      if (!canEdit.value || props.showControls === false || !activeStrokes.value[strokeIndex]) return
      if (tool.value !== 'select') { canvasDown(event); return }
      selectStroke(strokeIndex)
      if (tool.value !== 'select' || event.button !== 0) return
      event.preventDefault()
      drag = { strokeIndex, pointIndex, previous: clone(draft.value), pointerId: event.pointerId }
      canvas.value.setPointerCapture(event.pointerId)
    }
    function dragMove(event) {
      if (canEdit.value && drawGesture && event.pointerId === drawGesture.pointerId) {
        const point = normalizedDrawingPoint(event, canvas.value.getBoundingClientRect())
        if (point) updateGesture(point)
        return
      }
      if (!canEdit.value || !drag || event.pointerId !== drag.pointerId) return
      const point = normalizedDrawingPoint(event, canvas.value.getBoundingClientRect())
      if (!point) return
      const stroke = draft.value.variants[activeVariant.value].strokes[drag.strokeIndex]
      if (stroke.kind === 'point') stroke.point = point
      else if (stroke.kind === 'ellipse') {
        if (drag.pointIndex === 0) stroke.center = point
        else stroke.radii[drag.pointIndex - 1] = Math.abs(point[drag.pointIndex - 1] - stroke.center[drag.pointIndex - 1])
      }
      else stroke.points[drag.pointIndex] = point
      draft.value.teacher_confirmed = false
    }
    function dragEnd(event) {
      if (drawGesture && event.pointerId === drawGesture.pointerId) {
        dragMove(event)
        const stroke = draftStroke.value
        cancelDrag(event)
        if (stroke && addStroke(stroke)) tool.value = 'select'
        return
      }
      if (!drag || event.pointerId !== drag.pointerId) return
      const next = changedDrawingReference(draft.value)
      const previous = drag.previous
      drag = null
      if (canvas.value.hasPointerCapture(event.pointerId)) canvas.value.releasePointerCapture(event.pointerId)
      draft.value = previous
      if (JSON.stringify(next.variants) !== JSON.stringify(previous.variants)) edit((value) => { value.variants = next.variants; value.comparison_mode = 'fixed' })
    }
    function cancelDrag(event) {
      const pointerId = drag?.pointerId ?? drawGesture?.pointerId
      if (event && pointerId !== event.pointerId) return
      if (drag) draft.value = drag.previous
      drag = null
      drawGesture = null
      draftStroke.value = null
      if (pointerId !== undefined && canvas.value?.hasPointerCapture(pointerId)) canvas.value.releasePointerCapture(pointerId)
    }
    function undo() {
      if (!canEdit.value) return
      if (pendingPoints.value.length) { pendingPoints.value.pop(); return }
      if (!history.value.length) return
      cancelDrag()
      redoHistory.value.push(clone(draft.value))
      draft.value = changedDrawingReference(history.value.pop())
      selectVariant(Math.max(0, Math.min(activeVariant.value, draft.value.variants.length - 1)))
      error.value = ''
      publish()
    }
    function redo() {
      if (!canEdit.value || !redoHistory.value.length || pendingPoints.value.length) return
      cancelDrag()
      history.value.push(clone(draft.value))
      draft.value = changedDrawingReference(redoHistory.value.pop())
      selectVariant(Math.max(0, Math.min(activeVariant.value, draft.value.variants.length - 1)))
      error.value = ''
      publish()
    }
    // 紧凑工具条：教师标注要求「向下滑动时这一部分都要保留」——滚动超出其上沿时
    // 切换为 fixed 固定在滚动容器顶部，回到顶部时自然归位；占位元素保持原始布局高度。
    const workbarEl = ref(null)
    const workbarGapEl = ref(null)
    const workbarPinned = ref(false)
    const workbarHeight = ref(0)
    const workbarFixedStyle = ref(null)
    let workbarScroller = null

    function findScrollParent(node) {
      let parent = node && node.parentElement
      while (parent && parent !== document.body) {
        if (/(auto|scroll)/.test(getComputedStyle(parent).overflowY)) return parent
        parent = parent.parentElement
      }
      return null
    }

    function applyWorkbarFixed(anchorRect, scrollerRect) {
      workbarFixedStyle.value = {
        position: 'fixed',
        top: `${Math.max(0, Math.round(scrollerRect.top))}px`,
        left: `${Math.round(anchorRect.left)}px`,
        width: `${Math.round(anchorRect.width)}px`,
        zIndex: '30',
      }
    }

    function refreshWorkbarPin() {
      if (!props.canvasVisible) {
        workbarPinned.value = false
        workbarFixedStyle.value = null
        return
      }
      const el = workbarEl.value
      if (!el || !el.isConnected || !workbarScroller) return
      const scrollerRect = workbarScroller.getBoundingClientRect()
      if (!workbarPinned.value) {
        const rect = el.getBoundingClientRect()
        if (rect.top < scrollerRect.top) {
          workbarHeight.value = el.offsetHeight
          workbarPinned.value = true
          applyWorkbarFixed(rect, scrollerRect)
        }
        return
      }
      const gapRect = workbarGapEl.value && workbarGapEl.value.getBoundingClientRect()
      if (!gapRect || gapRect.top >= scrollerRect.top) {
        workbarPinned.value = false
        workbarFixedStyle.value = null
        return
      }
      applyWorkbarFixed(gapRect, scrollerRect)
    }

    watch(() => props.canvasVisible, () => nextTick(refreshWorkbarPin))

    onMounted(() => {
      nextTick(() => {
        workbarScroller = findScrollParent(workbarEl.value)
        if (workbarScroller) workbarScroller.addEventListener('scroll', refreshWorkbarPin, { passive: true })
        if (typeof window !== 'undefined') window.addEventListener('resize', refreshWorkbarPin)
        refreshWorkbarPin()
      })
    })
    onBeforeUnmount(() => {
      cancelDrag()
      if (workbarScroller) workbarScroller.removeEventListener('scroll', refreshWorkbarPin)
      if (typeof window !== 'undefined') window.removeEventListener('resize', refreshWorkbarPin)
    })

    expose({ deleteSelectedStroke: deleteStroke, clearSelection, prepareForSave, valueForSave })
    return { draft, canvas, activeVariant, selectedStroke, tool, pendingPoints, history, redoHistory, brushWidth, imageSize, visibleStrokes, brushTransform, drawingTools, minimumPoints, toolHint, error, activeStrokes, validDrawingRegions, regionWarning, hasCandidate, imageFailed, imageLoaded, canEdit, canRequestEdit, onImageLoad, onImageError,
      workbarEl, workbarGapEl, workbarPinned, workbarHeight, workbarFixedStyle,
      setTool, requestTool, selectVariant, addVariant, removeVariant, canvasDown, finishPolyline, selectStroke, strokeDown, deleteStroke, clearSelection,
      dragStart, dragMove, dragEnd, cancelDrag, undo, redo,
      brushPoints: (points) => points.map((point) => point.map((value, axis) => value * imageSize.value[axis] / 1000).join(',')).join(' '),
      svgPoints: (points) => points.map((point) => point.join(',')).join(' '),
      strokePoints: (stroke) => stroke.kind === 'point' ? [stroke.point] : stroke.kind === 'ellipse'
        ? [stroke.center, [stroke.center[0] + stroke.radii[0], stroke.center[1]], [stroke.center[0], stroke.center[1] + stroke.radii[1]]]
        : stroke.points }
  },
}
</script>

<style scoped>
.drawing-reference-editor { padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-subtle); }
.drawing-reference-editor.is-shared-canvas { padding: 0; border: 0; background: transparent; }
.drawing-reference-workbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px 12px; margin-bottom: 6px; }
.is-compact .drawing-reference-workbar { position: sticky; top: 0; z-index: 25; padding: 4px 0; background: var(--card); border-bottom: 1px solid var(--line); }
.drawing-reference-geometry-tools { display: flex; flex: 1 1 auto; flex-wrap: wrap; align-items: center; min-width: 0; gap: 6px; }
.drawing-reference-drawing-tools { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 5px; margin-left: auto; min-width: 0; }
.drawing-reference-tools-label { color: var(--ink-muted); font-size: 12px; white-space: nowrap; }
.drawing-reference-edit { padding: 5px 7px; border: 1px solid var(--border); border-radius: 6px; background: var(--card); color: var(--primary-dark); font-size: 12px; cursor: pointer; }
.drawing-reference-drawing-tools .drawing-reference-toolbar { margin: 0; gap: 4px; }
.drawing-reference-settings { min-width: 0; }
.is-compact .drawing-reference-generation { display: inline-flex; margin: 0 10px 4px 0; font-size: 12px; }
.is-compact .drawing-reference-error { margin: 4px 0; }
.is-compact .drawing-reference-tip, .is-compact .drawing-reference-note { margin: 4px 0 6px; font-size: 12px; }
.is-compact .drawing-reference-toolbar { gap: 5px; margin-bottom: 6px; }
.is-compact .drawing-reference-drawing-tools .drawing-reference-toolbar { margin: 0; }
.is-compact .drawing-reference-toolbar button, .is-compact .drawing-reference-toolbar select { padding: 5px 7px; font-size: 12px; }
.is-compact .drawing-reference-toolbar label { min-width: 0; font-size: 12px; }
@media (max-width: 640px) { .drawing-reference-drawing-tools { justify-content: flex-start; margin-left: 0; } }
.drawing-reference-controls { min-width: 0; margin: 0; padding: 0; border: 0; }
.drawing-reference-generation { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 13px; }
.drawing-reference-note, .drawing-reference-tip { margin: 0 0 10px; line-height: 1.6; color: var(--ink-muted); font-size: 13px; }
.drawing-reference-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
.drawing-reference-toolbar label { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 13px; }
.drawing-reference-toolbar button, .drawing-reference-toolbar select { max-width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--card); color: var(--primary-dark); }
.drawing-reference-toolbar button { cursor: pointer; }
.drawing-reference-toolbar button[aria-pressed="true"] { color: var(--on-primary); background: var(--primary); }
.drawing-reference-toolbar button:disabled { opacity: .45; cursor: default; }
.drawing-reference-canvas { position: relative; width: 100%; box-sizing: border-box; border: 1px solid #ced7d5; background: var(--card); }
.drawing-reference-canvas img { display: block; width: 100%; height: auto; }
.drawing-reference-canvas svg { position: absolute; inset: 0; width: 100%; height: 100%; touch-action: none; user-select: none; }
.drawing-reference-canvas svg.is-readonly { pointer-events: none; }
.drawing-reference-canvas svg.is-select-mode { z-index: 8; pointer-events: none; }
.drawing-reference-canvas svg.is-reference-priority:not(.is-readonly) { z-index: 30; pointer-events: auto; }
.drawing-reference-canvas svg.is-select-mode .reference-stroke,
.drawing-reference-canvas svg.is-select-mode .reference-area,
.drawing-reference-canvas svg.is-select-mode .reference-brush,
.drawing-reference-canvas svg.is-select-mode .reference-point { pointer-events: visiblePainted; }
.drawing-region, .context-region { fill: none; stroke: #53766c; stroke-width: 1; stroke-dasharray: 6 4; vector-effect: non-scaling-stroke; pointer-events: none; }
.context-region { stroke: #a16a16; }
.reference-stroke { fill: none; stroke: #507690; stroke-opacity: .60; stroke-width: 7; vector-effect: non-scaling-stroke; cursor: pointer; }
.reference-area { fill: #507690; fill-opacity: .45; stroke: none; cursor: pointer; }
.reference-brush { fill: none; stroke: #507690; stroke-opacity: .45; stroke-linecap: round; stroke-linejoin: round; cursor: pointer; }
.reference-area.selected { fill: var(--primary); }
.reference-brush.selected, .selected .reference-brush { stroke: var(--primary); }
.is-preview { pointer-events: none; opacity: .65; }
.reference-point { fill: #507690; fill-opacity: .6; stroke: white; stroke-width: 1; vector-effect: non-scaling-stroke; cursor: grab; }
.reference-stroke.selected { stroke: var(--primary); }
.reference-point.selected { stroke: var(--primary); stroke-width: 2; }
.pending-stroke { fill: none; stroke: #a56816; stroke-width: 2; stroke-dasharray: 5 3; vector-effect: non-scaling-stroke; pointer-events: none; }
.pending-point { fill: #a56816; pointer-events: none; }
.drawing-reference-error { color: var(--danger); font-size: 13px; }
</style>
