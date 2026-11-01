<template>
  <section class="template-geometry-legend" aria-label="图外题号与作答框对应表">
    <div class="legend-heading">
      <span>题号在图外显示，点击对应项高亮定位框；框可直接拖动。</span>
      <button type="button" :aria-pressed="hidden" @click="$emit('toggle')">{{ hidden ? '显示定位框' : '隐藏定位框看原图' }}</button>
    </div>
    <div class="legend-items">
      <button v-for="item in items" :key="item.key" type="button" :class="`kind-${item.kind}`"
        :aria-pressed="selected?.target === item.target" :disabled="disabled"
        @click="$emit('select', { kind: item.kind, target: item.target })">
        <strong>Q{{ item.target.slot_id || item.target.question_number }}</strong> {{ item.label }}
      </button>
    </div>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({
  anchors: { type: Array, default: () => [] }, answers: { type: Array, default: () => [] },
  drawings: { type: Array, default: () => [] }, labels: { type: Array, default: () => [] },
  selected: { type: Object, default: null }, disabled: Boolean, hidden: Boolean,
})
defineEmits(['select', 'toggle'])
const items = computed(() => {
  const result = []
  for (const [kind, values, label] of [
    ['question', props.anchors.filter(a => Array.isArray(a.question_number_bbox) && a.question_number_bbox.length === 4), '题号位置'],
    ['answer', props.answers, '作答框'], ['drawing', props.drawings, '画图区'],
    ['drawing-label', props.labels, '作图锚点'],
  ]) {
    const counts = new Map()
    values.forEach((target, index) => {
      const part = (counts.get(target.question_number) || 0) + 1
      counts.set(target.question_number, part)
      result.push({ key: `${kind}-${index}`, kind, target, label: `${label} ${part}` })
    })
  }
  return result
})
</script>
<style scoped>
.template-geometry-legend { padding: 8px; border: 1px solid var(--border, #ccd5df); background: var(--card, #fff); color: var(--ink, #182b40); }
.legend-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; }
.legend-items { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
button { font: inherit; min-height: 32px; border: 1px solid var(--border); border-radius: 4px; padding: 4px 8px; background: var(--card); color: var(--primary-dark); cursor: pointer; overflow-wrap: anywhere; max-width: 100%; }
button[aria-pressed="true"] { background: var(--primary); color: var(--on-primary); outline: 2px solid var(--primary-dark); outline-offset: 1px; }
button:focus-visible { outline: 3px solid #b86100; outline-offset: 2px; }
button:disabled { opacity: .6; cursor: default; }
.kind-question { border-color: var(--danger); }
.kind-drawing, .kind-drawing-label { border-color: var(--success); }
</style>
