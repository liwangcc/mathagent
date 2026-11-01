<template>
  <span class="animated-number">
    <!-- Assistive tech reads only the final value, never the frames of the count. -->
    <span aria-hidden="true">{{ format(shown) }}</span>
    <span class="animated-number__value">{{ format(value) }}</span>
  </span>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

// A report figure that counts up to its value, as the showcase's metrics do.
// Reduced motion shows the value at once; a missing value reads as "—".
const props = defineProps({
  value: {
    type: Number,
    default: null,
  },
  decimals: {
    type: Number,
    default: 0,
  },
  duration: {
    type: Number,
    default: 900,
  },
})

const shown = ref(0)
let frame = 0

function format(number) {
  return number != null && Number.isFinite(number) ? number.toFixed(props.decimals) : '—'
}

function countTo(target) {
  cancelAnimationFrame(frame)
  if (target == null || !Number.isFinite(target)) {
    shown.value = Number.NaN
    return
  }
  const from = Number.isFinite(shown.value) ? shown.value : 0
  if (from === target || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    shown.value = target
    return
  }
  const start = performance.now()
  const tick = (now) => {
    const t = Math.min(1, (now - start) / props.duration)
    shown.value = from + (target - from) * (1 - Math.pow(1 - t, 3))
    if (t < 1) frame = requestAnimationFrame(tick)
  }
  frame = requestAnimationFrame(tick)
}

onMounted(() => countTo(props.value))
watch(() => props.value, countTo)
onBeforeUnmount(() => cancelAnimationFrame(frame))
</script>

<style scoped>
.animated-number {
  font-variant-numeric: tabular-nums;
}

.animated-number__value {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
</style>
