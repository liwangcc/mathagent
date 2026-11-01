<template>
  <nav v-if="visible" class="grading-quick-dock" aria-label="批阅快捷导航">
    <button
      v-for="item in items"
      :key="item.key"
      type="button"
      class="dock-btn"
      :aria-label="item.label"
      @click="go(item)"
      @mouseenter="hovered = item.key"
      @mouseleave="hovered = null"
      @focus="hovered = item.key"
      @blur="hovered = null"
    >
      <span class="dock-tip" :class="{ show: hovered === item.key }" role="tooltip">{{ item.label }}</span>
      <svg v-if="item.key === 'top'" viewBox="0 0 24 24" class="dock-icon" aria-hidden="true">
        <path d="M12 19V6M6 11l6-6 6 6" />
      </svg>
      <svg v-else-if="item.key === 'summary'" viewBox="0 0 24 24" class="dock-icon" aria-hidden="true">
        <path d="M7 7h10M7 12h10M7 17h6" />
      </svg>
      <svg v-else viewBox="0 0 24 24" class="dock-icon" aria-hidden="true">
        <path d="M9 6l9 6-9 6z" />
      </svg>
    </button>
  </nav>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

const hovered = ref(null)

const targets = {
  top: null,
  summary: 'grading-summary-anchor',
  start: 'grading-start-anchor',
}

const present = ref({ top: true, summary: false, start: false })

function probe() {
  present.value = {
    top: true,
    summary: !!document.getElementById(targets.summary),
    start: !!document.getElementById(targets.start),
  }
}

const items = computed(() => {
  const list = [{ key: 'top', label: '返回顶部' }]
  if (present.value.summary) list.push({ key: 'summary', label: '批阅总结' })
  if (present.value.start) list.push({ key: 'start', label: '开始批阅' })
  return list
})

const visible = computed(() => items.value.length > 1)

function go(item) {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const behavior = reduce ? 'auto' : 'smooth'
  const pad = 12
  const anchorEl = item.key === 'top' ? null : document.getElementById(targets[item.key])
  const refEl = anchorEl
    || document.getElementById(targets.summary)
    || document.getElementById(targets.start)
    || document.body
  const scroller = findScroller(refEl)
  const targetTop = () => {
    // The results anchor locates the scroller, not the page's bottom edge.
    if (item.key === 'start') return Math.max(0, scroller.scrollHeight - scroller.clientHeight)
    if (!anchorEl) return 0
    const rect = anchorEl.getBoundingClientRect()
    const base = scroller === document.scrollingElement ? 0 : scroller.getBoundingClientRect().top
    return Math.max(0, scroller.scrollTop + (rect.top - base) - pad)
  }
  setTop(scroller, targetTop(), behavior)
  // Content can grow while a smooth scroll is in flight (lazy panels, rows),
  // which parks the viewport at a stale offset.  Recompute on the SAME scroller
  // after the animation and once more after layout settles.
  for (const delay of [350, 750]) {
    window.setTimeout(() => setTop(scroller, targetTop(), 'auto'), delay)
  }
}

function findScroller(el) {
  let node = el ? el.parentElement : document.body
  while (node) {
    const st = window.getComputedStyle(node)
    if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && node.scrollHeight > node.clientHeight + 4) return node
    node = node.parentElement
  }
  return document.scrollingElement
}

function setTop(scroller, top, behavior) {
  if (scroller === document.scrollingElement) window.scrollTo({ top, behavior })
  else scroller.scrollTo({ top, behavior })
}

let timer = null
onMounted(() => {
  probe()
  timer = window.setInterval(probe, 1500)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped>
.grading-quick-dock {
  position: fixed;
  right: 18px;
  bottom: 84px;
  z-index: 1200;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-2);
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 22px;
  box-shadow: 0 10px 28px rgba(31, 30, 29, 0.12);
}

.dock-btn {
  position: relative;
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--paper-raised);
  color: var(--ink-soft);
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease, transform 0.15s ease;
}

.dock-btn:hover {
  background: var(--primary);
  color: var(--on-primary);
  transform: translateY(-1px);
}

.dock-btn:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

.dock-icon {
  width: 20px;
  height: 20px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.dock-icon path[d^='M9 6l9'] {
  fill: currentColor;
  stroke: none;
}

.dock-tip {
  position: absolute;
  right: calc(100% + 10px);
  top: 50%;
  transform: translateY(-50%);
  padding: 5px 10px;
  background: var(--ink-solid);
  color: var(--on-ink);
  font-size: var(--fs-xs);
  line-height: 1.2;
  white-space: nowrap;
  border-radius: 999px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease;
}

.dock-tip.show {
  opacity: 1;
}

/* The shell reserves a separate bottom band so this never sits over a field. */
@media (max-width: 768px) {
  .grading-quick-dock {
    right: 12px;
    bottom: calc(10px + env(safe-area-inset-bottom, 0px));
    max-width: calc(100vw - 24px);
    flex-direction: row;
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-md);
  }

  .dock-tip {
    right: 0;
    top: auto;
    bottom: calc(100% + 12px);
    transform: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .dock-btn,
  .dock-tip {
    transition: none;
  }
}
</style>
