<template>
  <i class="verdict-swatch" :class="`is-${verdict}`" aria-hidden="true">
    <svg v-if="verdict !== 'pending'" viewBox="0 0 24 24" focusable="false">
      <path v-if="verdict === 'correct'" d="M20 6 9 17l-5-5" />
      <path v-else-if="verdict === 'partial'" d="M5 12h14" />
      <path v-else d="M18 6 6 18M6 6l12 12" />
    </svg>
  </i>
</template>

<script setup>
// Legend key for one grading outcome. Outcomes never rely on color alone:
// each carries its own glyph, wrong also a hatch, pending a dashed outline.
defineProps({
  verdict: {
    type: String,
    required: true,
    validator: (value) => ['correct', 'partial', 'wrong', 'pending'].includes(value),
  },
})
</script>

<style scoped>
.verdict-swatch {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  flex: none;
  border-radius: 6px;
  color: #fffdf7;
}

.verdict-swatch svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 3;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.verdict-swatch.is-correct {
  background: var(--chart-correct);
}

.verdict-swatch.is-partial {
  background: var(--chart-partial);
}

.verdict-swatch.is-wrong {
  background:
    repeating-linear-gradient(45deg, transparent 0 3px, rgba(255, 253, 247, 0.45) 3px 4.6px),
    var(--chart-wrong);
}

.verdict-swatch.is-pending {
  border: 1px dashed var(--ink-faint);
}
</style>
