<template>
  <Teleport to="body">
    <div v-if="active" class="guided-tour">
      <div
        class="guided-tour__scrim"
        :class="{ 'is-dim': !spot }"
        aria-hidden="true"
        @pointerdown.prevent
        @mousedown.prevent
        @click.prevent.stop="focusPrimary"
      />
      <div v-if="spot" class="guided-tour__spot" :style="spotStyle" aria-hidden="true" />
      <section
        ref="cardRef"
        class="guided-tour__card"
        :class="[`is-${placement}`, { 'is-placed': placed, 'is-resolving': resolving }]"
        :style="cardStyle"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="`${uid}-title`"
        :aria-describedby="`${uid}-body`"
        tabindex="-1"
      >
        <span v-if="hasArrow" class="guided-tour__arrow" :style="arrowStyle" aria-hidden="true" />
        <div class="guided-tour__meta">
          <span class="guided-tour__count">第 {{ index + 1 }} / {{ steps.length }} 步</span>
          <span class="guided-tour__bar" aria-hidden="true"><span :style="{ width: `${progress}%` }" /></span>
        </div>
        <h2 :id="`${uid}-title`" class="guided-tour__title" aria-live="polite">{{ step.title }}</h2>
        <p :id="`${uid}-body`" class="guided-tour__body">{{ step.body }}</p>
        <ul v-if="step.legend?.length" class="guided-tour__legend">
          <li v-for="item in step.legend" :key="item.label">
            <el-tag :type="item.type" effect="plain" size="small">{{ item.label }}</el-tag>
            <span>{{ item.text }}</span>
          </li>
        </ul>
        <p v-if="step.emphasis" class="guided-tour__emphasis">{{ step.emphasis }}</p>
        <p v-if="notice" class="guided-tour__notice">{{ notice }}</p>
        <div class="guided-tour__actions">
          <el-button v-if="!isLast" text class="guided-tour__skip" @click="finish('skip')">跳过引导</el-button>
          <span class="guided-tour__spacer" />
          <el-button v-if="index > 0" :disabled="resolving" @click="go(index - 1)">上一步</el-button>
          <el-button ref="primaryRef" type="primary" :loading="resolving" @click="advance">
            {{ isLast ? '完成' : '下一步' }}
          </el-button>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { isRouteBusy } from '@/utils/routeBusy'

const emit = defineEmits(['finish'])
const router = useRouter()

const SPOT_PAD = 6
const GAP = 14
const EDGE = 12
const WAIT_MS = 4000
// A fallback anchor may render in the same frame as the preferred one; give the
// preferred anchor a moment before settling for the fallback.
const FALLBACK_GRACE_MS = 600

const uid = `guided-tour-${Math.random().toString(36).slice(2, 8)}`
const active = ref(false)
const steps = ref([])
const index = ref(0)
const resolving = ref(false)
const spot = ref(null)
const placement = ref('center')
const placed = ref(false)
const cardPosition = ref({ top: 0, left: 0 })
const arrowOffset = ref(0)
const notice = ref('')
const cardRef = ref(null)
const primaryRef = ref(null)

let run = 0
let target = null
let targetKey = ''
let observer = null
let frame = 0
let restoreFocus = null

const step = computed(() => steps.value[index.value] || {})
const isLast = computed(() => index.value === steps.value.length - 1)
const progress = computed(() => (steps.value.length ? ((index.value + 1) / steps.value.length) * 100 : 0))
const hasArrow = computed(() => Boolean(spot.value) && ['top', 'bottom', 'left', 'right'].includes(placement.value))
const spotStyle = computed(() => spot.value && {
  top: `${spot.value.top}px`,
  left: `${spot.value.left}px`,
  width: `${spot.value.width}px`,
  height: `${spot.value.height}px`,
})
const cardStyle = computed(() => (placement.value === 'center'
  ? {}
  : { top: `${cardPosition.value.top}px`, left: `${cardPosition.value.left}px` }))
const arrowStyle = computed(() => (placement.value === 'top' || placement.value === 'bottom'
  ? { left: `${arrowOffset.value}px` }
  : { top: `${arrowOffset.value}px` }))

function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
}

function visibleTarget(key) {
  for (const element of document.querySelectorAll(`[data-tour="${key}"]`)) {
    if (element.closest('[inert], [aria-hidden="true"]')) continue
    const style = window.getComputedStyle(element)
    if (style.visibility === 'hidden' || style.display === 'none') continue
    const rect = element.getBoundingClientRect()
    if (rect.width >= 1 && rect.height >= 1) return element
  }
  return null
}

function waitForTarget(keys, token) {
  if (!keys.length) return Promise.resolve(null)
  const started = performance.now()
  return new Promise((resolve) => {
    const poll = () => {
      if (token !== run || !active.value) return resolve(null)
      const elapsed = performance.now() - started
      for (const [position, key] of keys.entries()) {
        if (position > 0 && elapsed < FALLBACK_GRACE_MS) break
        const element = visibleTarget(key)
        if (element) return resolve({ element, key, fallback: position > 0 })
      }
      if (elapsed >= WAIT_MS) return resolve(null)
      window.setTimeout(poll, 80)
    }
    poll()
  })
}

function setTarget(found) {
  observer?.disconnect()
  target = found?.element || null
  targetKey = found?.key || ''
  if (!target) {
    spot.value = null
    return
  }
  const rect = target.getBoundingClientRect()
  if (rect.top < EDGE || rect.bottom > window.innerHeight - EDGE) {
    target.scrollIntoView({ block: 'center', inline: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
  }
  if (typeof ResizeObserver === 'function') {
    observer = new ResizeObserver(schedule)
    observer.observe(target)
    if (cardRef.value) observer.observe(cardRef.value)
  }
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), Math.max(min, max))
}

function placeCard(viewportWidth, viewportHeight) {
  const card = cardRef.value
  if (!card) return
  const box = spot.value
  if (!box) {
    placement.value = 'center'
    placed.value = true
    return
  }
  const width = card.offsetWidth
  const height = card.offsetHeight
  const room = {
    bottom: viewportHeight - (box.top + box.height) - GAP - EDGE >= height,
    top: box.top - GAP - EDGE >= height,
    right: viewportWidth - (box.left + box.width) - GAP - EDGE >= width,
    left: box.left - GAP - EDGE >= width,
  }
  const side = [step.value.placement, 'bottom', 'top', 'right', 'left'].find((name) => name && room[name])
  if (!side) {
    // Target fills the viewport: float the card at the bottom edge instead of covering the spot.
    placement.value = 'floating'
    cardPosition.value = { top: viewportHeight - height - EDGE, left: (viewportWidth - width) / 2 }
    placed.value = true
    return
  }
  let top
  let left
  if (side === 'bottom' || side === 'top') {
    left = clamp(box.left + box.width / 2 - width / 2, EDGE, viewportWidth - width - EDGE)
    top = side === 'bottom' ? box.top + box.height + GAP : box.top - GAP - height
    arrowOffset.value = clamp(box.left + box.width / 2 - left, 18, width - 18)
  } else {
    top = clamp(box.top + box.height / 2 - height / 2, EDGE, viewportHeight - height - EDGE)
    left = side === 'right' ? box.left + box.width + GAP : box.left - GAP - width
    arrowOffset.value = clamp(box.top + box.height / 2 - top, 18, height - 18)
  }
  placement.value = side
  cardPosition.value = { top, left }
  placed.value = true
}

function layout() {
  frame = 0
  if (!active.value) return
  // Pages re-render their controls (v-if branches); follow the anchor, not the stale node.
  if (target && !target.isConnected && targetKey) target = visibleTarget(targetKey)
  const viewportWidth = window.innerWidth
  const viewportHeight = window.innerHeight
  if (target?.isConnected) {
    const rect = target.getBoundingClientRect()
    const top = Math.max(rect.top - SPOT_PAD, 4)
    const left = Math.max(rect.left - SPOT_PAD, 4)
    const bottom = Math.min(rect.bottom + SPOT_PAD, viewportHeight - 4)
    const right = Math.min(rect.right + SPOT_PAD, viewportWidth - 4)
    spot.value = bottom > top && right > left ? { top, left, width: right - left, height: bottom - top } : null
  } else {
    spot.value = null
  }
  placeCard(viewportWidth, viewportHeight)
}

function schedule() {
  if (!frame) frame = window.requestAnimationFrame(layout)
}

function focusPrimary() {
  const button = primaryRef.value?.$el || primaryRef.value
  if (typeof button?.focus === 'function') button.focus({ preventScroll: true })
  else cardRef.value?.focus({ preventScroll: true })
}

async function go(next) {
  const token = ++run
  const current = steps.value[next]
  if (!current) return
  index.value = next
  resolving.value = true
  notice.value = ''
  let pageReady = true
  if (current.route && typeof current.isOn === 'function' && !current.isOn(router.currentRoute.value.path)) {
    if (isRouteBusy()) {
      pageReady = false
      notice.value = '当前页面有任务正在处理，引导不会切换页面，以免中断任务。'
    } else {
      let failure = null
      try {
        failure = await router.push(current.route)
      } catch (error) {
        failure = error
      }
      if (token !== run) return
      if (failure) {
        pageReady = false
        notice.value = '页面没有切换，先看说明即可。'
      }
    }
  }
  const keys = Array.isArray(current.targets) ? current.targets : []
  const found = pageReady ? await waitForTarget(keys, token) : null
  if (token !== run) return
  setTarget(found)
  if (found?.fallback && current.fallbackNote) notice.value = current.fallbackNote
  else if (pageReady && keys.length && !found) notice.value = '当前页面没有显示这个位置，先看说明即可。'
  resolving.value = false
  await nextTick()
  layout()
  // The card is visibility:hidden until its first placement renders; focus after that.
  await nextTick()
  if (token === run) focusPrimary()
}

function advance() {
  if (resolving.value) return
  if (isLast.value) finish('done')
  else go(index.value + 1)
}

function trapFocus(event) {
  const card = cardRef.value
  if (!card) return
  const focusable = [...card.querySelectorAll('button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (!card.contains(document.activeElement)) {
    event.preventDefault()
    first.focus()
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function onKeydown(event) {
  if (!active.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    finish('skip')
  } else if (event.key === 'Tab') {
    trapFocus(event)
  } else if (!resolving.value && event.key === 'ArrowRight') {
    event.preventDefault()
    advance()
  } else if (!resolving.value && event.key === 'ArrowLeft' && index.value > 0) {
    event.preventDefault()
    go(index.value - 1)
  }
}

function listen(on) {
  const method = on ? 'addEventListener' : 'removeEventListener'
  window[method]('resize', schedule)
  // Capture: the page scrolls inside el-main, not the window.
  window[method]('scroll', schedule, true)
  // Page enter transitions move the target without resizing it.
  document[method]('transitionend', schedule, true)
  document[method]('animationend', schedule, true)
  document[method]('keydown', onKeydown, true)
}

function teardown() {
  run += 1
  listen(false)
  observer?.disconnect()
  observer = null
  if (frame) window.cancelAnimationFrame(frame)
  frame = 0
  target = null
  targetKey = ''
  spot.value = null
  placed.value = false
  resolving.value = false
}

async function start(list, startIndex = 0) {
  const usable = Array.isArray(list) ? list.filter(Boolean) : []
  if (!usable.length) return
  steps.value = usable
  if (!active.value) {
    restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    active.value = true
    listen(true)
  }
  await go(clamp(startIndex, 0, usable.length - 1))
}

function finish(reason) {
  if (!active.value) return
  teardown()
  active.value = false
  emit('finish', reason)
  if (restoreFocus?.isConnected) restoreFocus.focus({ preventScroll: true })
  restoreFocus = null
}

onBeforeUnmount(() => {
  if (active.value) teardown()
})

defineExpose({ start, finish })
</script>

<style scoped>
.guided-tour {
  position: fixed;
  inset: 0;
  z-index: 3000;
  pointer-events: none;
}

.guided-tour__scrim {
  position: fixed;
  inset: 0;
  pointer-events: auto;
}

.guided-tour__scrim.is-dim {
  background: var(--tour-scrim, rgba(24, 22, 19, 0.56));
}

.guided-tour__spot {
  position: fixed;
  border-radius: 10px;
  box-shadow: 0 0 0 9999px var(--tour-scrim, rgba(24, 22, 19, 0.56));
  pointer-events: none;
  transition:
    top var(--dur-3) var(--ease-emphasized),
    left var(--dur-3) var(--ease-emphasized),
    width var(--dur-3) var(--ease-emphasized),
    height var(--dur-3) var(--ease-emphasized);
}

.guided-tour__spot::after {
  content: '';
  position: absolute;
  inset: -3px;
  border: 2px solid var(--primary);
  border-radius: 12px;
  animation: guided-tour-pulse 1.8s var(--ease-out) infinite;
}

@keyframes guided-tour-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary) 55%, transparent); }
  70% { box-shadow: 0 0 0 12px color-mix(in srgb, var(--primary) 0%, transparent); }
  100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--primary) 0%, transparent); }
}

.guided-tour__card {
  position: fixed;
  width: min(360px, calc(100vw - 24px));
  padding: 14px 16px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--card);
  color: var(--ink);
  box-shadow: var(--shadow-pop);
  pointer-events: auto;
  visibility: hidden;
  outline: none;
  transition:
    top var(--dur-3) var(--ease-emphasized),
    left var(--dur-3) var(--ease-emphasized);
}

.guided-tour__card.is-placed {
  visibility: visible;
}

.guided-tour__card.is-center {
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  transition: none;
}

.guided-tour__card.is-resolving .guided-tour__title,
.guided-tour__card.is-resolving .guided-tour__body {
  opacity: 0.55;
}

.guided-tour__arrow {
  position: absolute;
  width: 12px;
  height: 12px;
  border: 1px solid var(--border);
  background: var(--card);
  transform: rotate(45deg);
}

.is-bottom .guided-tour__arrow {
  top: -7px;
  margin-left: -6px;
  border-right: 0;
  border-bottom: 0;
}

.is-top .guided-tour__arrow {
  bottom: -7px;
  margin-left: -6px;
  border-top: 0;
  border-left: 0;
}

.is-right .guided-tour__arrow {
  left: -7px;
  margin-top: -6px;
  border-top: 0;
  border-right: 0;
}

.is-left .guided-tour__arrow {
  right: -7px;
  margin-top: -6px;
  border-bottom: 0;
  border-left: 0;
}

.guided-tour__meta {
  display: flex;
  align-items: center;
  gap: 10px;
}

.guided-tour__count {
  flex: 0 0 auto;
  color: var(--primary-dark);
  font-size: 12px;
  font-weight: 650;
}

.guided-tour__bar {
  flex: 1;
  height: 3px;
  overflow: hidden;
  border-radius: 3px;
  background: var(--line);
}

.guided-tour__bar span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--primary);
  transition: width var(--dur-3) var(--ease-out);
}

.guided-tour__title {
  margin: 10px 0 0;
  color: var(--ink);
  font-size: 16px;
  font-weight: 650;
  line-height: 1.45;
}

.guided-tour__body {
  margin: 6px 0 0;
  color: var(--ink-soft);
  font-size: 13px;
  line-height: 1.65;
}

.guided-tour__legend {
  display: grid;
  gap: 6px;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}

.guided-tour__legend li {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  align-items: start;
  gap: 8px;
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.55;
}

.guided-tour__legend :deep(.el-tag) {
  justify-self: start;
}

.guided-tour__emphasis,
.guided-tour__notice {
  margin: 10px 0 0;
  padding: 7px 10px;
  border-radius: 0 8px 8px 0;
  font-size: 12px;
  line-height: 1.55;
}

.guided-tour__emphasis {
  border-left: 3px solid var(--primary);
  background: var(--primary-soft);
  color: var(--primary-dark);
}

.guided-tour__notice {
  border-left: 3px solid var(--warning);
  background: var(--warning-soft);
  color: var(--ink-soft);
}

.guided-tour__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
}

.guided-tour__actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.guided-tour__skip {
  padding-inline: 4px;
  color: var(--ink-muted);
}

.guided-tour__spacer {
  flex: 1;
}

/* Vue rewrites :global() to the bare selector, so the dark scrim is set on :root. */
:global(:root[data-color-mode='dark']) {
  --tour-scrim: rgba(0, 0, 0, 0.64);
}

@media (prefers-color-scheme: dark) {
  :global(:root[data-color-mode='system']) {
    --tour-scrim: rgba(0, 0, 0, 0.64);
  }
}

@media (prefers-reduced-motion: reduce) {
  .guided-tour__spot,
  .guided-tour__card,
  .guided-tour__bar span {
    transition: none;
  }

  .guided-tour__spot::after {
    animation: none;
  }
}
</style>
