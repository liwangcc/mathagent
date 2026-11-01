<template>
  <div class="outcome-donut">
    <div class="outcome-donut__chart" @pointerleave="focus = null">
      <svg viewBox="0 0 200 200" role="img" :aria-label="ariaLabel">
        <defs>
          <pattern :id="hatchId" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
            <rect class="outcome-donut__hatch-ground" width="6" height="6" />
            <line class="outcome-donut__hatch-line" x1="1.5" y1="0" x2="1.5" y2="6" />
          </pattern>
        </defs>
        <circle class="outcome-donut__track" cx="100" cy="100" :r="DONUT_RADIUS" />
        <circle
          v-for="(arc, index) in arcs"
          :key="arc.key"
          class="outcome-donut__arc"
          :class="[`is-${arc.key}`, { 'is-dimmed': focus && focus !== arc.key }]"
          cx="100"
          cy="100"
          :r="DONUT_RADIUS"
          :stroke="arc.key === 'wrong' ? `url(#${hatchId})` : undefined"
          :style="{
            strokeDasharray: `${drawn ? arc.length : 0} ${DONUT_CIRCUMFERENCE}`,
            strokeDashoffset: -arc.offset,
            transitionDelay: `${index * 160}ms`,
          }"
          @pointerenter="focus = arc.key"
        />
      </svg>
      <div class="outcome-donut__center" aria-hidden="true">
        <strong>{{ focused ? `${Math.round(focused.share)}%` : centerValue }}</strong>
        <span>{{ focused ? `${focused.label} · ${focused.value} 题` : centerLabel }}</span>
      </div>
    </div>

    <ul class="outcome-donut__legend">
      <li
        v-for="arc in arcs"
        :key="arc.key"
        :class="{ 'is-focused': focus === arc.key }"
        @pointerenter="focus = arc.key"
        @pointerleave="focus = null"
      >
        <VerdictSwatch :verdict="arc.key" />
        <span>{{ arc.label }}</span>
        <strong>{{ arc.value }}</strong>
        <em>{{ counted ? `${Math.round(arc.share)}%` : '—' }}</em>
      </li>
      <li v-if="pending != null" class="outcome-donut__pending">
        <VerdictSwatch verdict="pending" />
        <span>待复核</span>
        <strong>{{ pending }}</strong>
        <em>未计入</em>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, useId } from 'vue'
import VerdictSwatch from '@/components/VerdictSwatch.vue'
import { DONUT_CIRCUMFERENCE, DONUT_RADIUS, donutArcs } from '@/utils/outcomeDonut'

const props = defineProps({
  // Judged outcomes in ring order: [{ key: 'correct' | 'partial' | 'wrong', label, value }].
  segments: {
    type: Array,
    required: true,
  },
  // Answers still waiting for the teacher; listed beside the ring, never drawn in it.
  pending: {
    type: Number,
    default: null,
  },
  // Scale the ring to more answers than the segments cover (0: the segments' sum).
  total: {
    type: Number,
    default: 0,
  },
  centerValue: {
    type: String,
    required: true,
  },
  centerLabel: {
    type: String,
    required: true,
  },
})

const hatchId = `outcome-hatch-${useId()}`
const focus = ref(null)
const drawn = ref(false)
const arcs = computed(() => donutArcs(props.segments, props.total))
const counted = computed(() => arcs.value.some((arc) => arc.value > 0))
const focused = computed(() => arcs.value.find((arc) => arc.key === focus.value && arc.value > 0) || null)
const ariaLabel = computed(() => {
  const parts = arcs.value.map((arc) => `${arc.label} ${arc.value} 题`).join('，')
  const pending = props.pending ? `；待复核 ${props.pending} 题未计入` : ''
  return `判定结果分布：${counted.value ? parts : '暂无已判题目'}${pending}`
})

// Arcs ink in once after the first paint, as the showcase's report does on reveal.
onMounted(() => {
  requestAnimationFrame(() => requestAnimationFrame(() => { drawn.value = true }))
})
</script>

<style scoped>
/* Ring beside its legend; the legend drops below once the card is too narrow for both. */
.outcome-donut {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  align-content: center;
  justify-content: center;
  gap: 20px 24px;
}

.outcome-donut__chart {
  position: relative;
  flex: 0 0 auto;
  width: min(100%, 184px);
  aspect-ratio: 1;
}

.outcome-donut__chart svg {
  display: block;
  width: 100%;
  height: 100%;
  transform: rotate(-90deg);
}

.outcome-donut__chart circle {
  fill: none;
  stroke-width: 22;
}

.outcome-donut__track {
  stroke: var(--fill);
}

.outcome-donut__arc {
  transition:
    stroke-dasharray 1.1s var(--ease-emphasized),
    stroke-dashoffset 1.1s var(--ease-emphasized),
    opacity var(--dur-3) var(--ease-out);
}

.outcome-donut__arc.is-correct { stroke: var(--chart-correct); }
.outcome-donut__arc.is-partial { stroke: var(--chart-partial); }
.outcome-donut__arc.is-dimmed { opacity: 0.28; }
.outcome-donut__hatch-ground { fill: var(--chart-wrong); }
.outcome-donut__hatch-line { stroke: rgba(255, 253, 247, 0.5); stroke-width: 1.6; }

.outcome-donut__center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}

.outcome-donut__center strong {
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 30px;
  font-weight: 400;
  line-height: 1;
  font-variant-numeric: lining-nums;
}

.outcome-donut__center span {
  margin-top: 6px;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.outcome-donut__legend {
  display: grid;
  flex: 1 1 220px;
  gap: 2px;
  min-width: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.outcome-donut__legend li {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) auto 44px;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  color: var(--ink-soft);
  font-size: var(--fs-md);
  transition: background-color var(--dur-2) var(--ease-out);
}

.outcome-donut__legend li.is-focused {
  background: var(--fill);
}

.outcome-donut__legend strong {
  color: var(--ink);
  font-variant-numeric: tabular-nums;
}

.outcome-donut__legend em {
  color: var(--ink-muted);
  font-style: normal;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.outcome-donut__legend .outcome-donut__pending {
  color: var(--ink-faint);
}

</style>
