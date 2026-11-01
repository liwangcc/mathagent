<template>
  <!-- The showcase's grading specimen in miniature: a page on the graph-paper desk is
       scanned, then marked. Decorative; the text beside it carries the status. -->
  <svg class="sheet-loader" :style="{ width: `${size}px` }" viewBox="0 0 96 72" aria-hidden="true" focusable="false">
    <rect class="sheet-loader__desk" width="96" height="72" rx="10" />
    <path class="sheet-loader__grid" d="M12 0V72M24 0V72M36 0V72M48 0V72M60 0V72M72 0V72M84 0V72M0 12H96M0 24H96M0 36H96M0 48H96M0 60H96" />
    <rect class="sheet-loader__pile" x="25" y="9" width="50" height="56" rx="2" transform="rotate(4 50 37)" />
    <g transform="rotate(-2 48 36)">
      <rect class="sheet-loader__shadow" x="23" y="9" width="50" height="57" rx="2" />
      <rect class="sheet-loader__sheet" x="22" y="7" width="50" height="57" rx="2" />
      <rect class="sheet-loader__title" x="27" y="12" width="24" height="2.6" rx="1.3" />
      <rect class="sheet-loader__print" x="27" y="21" width="22" height="2" rx="1" />
      <path class="sheet-loader__hand" d="M28 27.5 c 1.6 -2.4, 2.6 2.2, 4.2 0 s 2.4 -2.2, 4 0 s 2.6 1.8, 4.4 -.4" />
      <rect class="sheet-loader__print" x="27" y="35" width="25" height="2" rx="1" />
      <path class="sheet-loader__hand" d="M28 41.5 c 1.4 -2.2, 2.8 2, 4.4 .2 s 2.2 -2.4, 3.8 -.2" />
      <rect class="sheet-loader__print" x="27" y="49" width="20" height="2" rx="1" />
      <path class="sheet-loader__hand" d="M28 55.5 c 1.8 -2.2, 2.4 2.2, 4 0 s 2.6 -2.2, 4.2 0 s 2.2 2, 3.8 -.2 s 2 -1.8, 3.4 0" />
      <path class="sheet-loader__mark is-correct m1" pathLength="1" d="M58 22.5 c 1.2 .8, 2.4 2, 3.2 3.4 c 1.8 -3.4, 4 -6, 7 -8.2" />
      <path class="sheet-loader__mark is-correct m2" pathLength="1" d="M58 36.5 c 1.2 .8, 2.4 2, 3.2 3.4 c 1.8 -3.4, 4 -6, 7 -8.2" />
      <path class="sheet-loader__mark is-partial m3" pathLength="1" d="M58 50.5 c 1.2 .8, 2.4 2, 3.2 3.4 c 1.8 -3.4, 4 -6, 7 -8.2" />
      <path class="sheet-loader__mark is-partial m4" pathLength="1" d="M63 47.6 c .9 1.2, 1.8 2.4, 2.8 3.6" />
      <g class="sheet-loader__scan">
        <rect x="22" y="2" width="50" height="7" />
        <rect x="22" y="8.4" width="50" height="1.2" />
      </g>
    </g>
  </svg>
</template>

<script setup>
defineProps({
  size: {
    type: Number,
    default: 96,
  },
})
</script>

<style scoped>
.sheet-loader {
  display: block;
  flex: none;
  height: auto;
  overflow: visible;
}

.sheet-loader__desk { fill: var(--paper-deep); }
.sheet-loader__grid { fill: none; stroke: var(--hair); stroke-width: 1; }
.sheet-loader__pile { fill: #f7f4ec; stroke: rgba(31, 30, 29, 0.08); }
.sheet-loader__shadow { fill: rgba(31, 30, 29, 0.12); }

/* The page stays paper-white in both color modes: it is a scanned photo, not UI. */
.sheet-loader__sheet { fill: #fffdf7; stroke: rgba(31, 30, 29, 0.1); }
:root[data-color-mode='dark'] .sheet-loader__sheet { fill: #f1ede2; }

@media (prefers-color-scheme: dark) {
  :root[data-color-mode='system'] .sheet-loader__sheet { fill: #f1ede2; }
}

.sheet-loader__title { fill: #2a2723; opacity: 0.72; }
.sheet-loader__print { fill: #b9b2a5; }

.sheet-loader__hand {
  fill: none;
  stroke: #1f3a68;
  stroke-width: 1.1;
  stroke-linecap: round;
  stroke-linejoin: round;
  opacity: 0.85;
}

.sheet-loader__mark {
  fill: none;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-dasharray: 1;
}

.sheet-loader__mark.is-correct { stroke: #2f6b45; }
.sheet-loader__mark.is-partial { stroke: #b7791f; }

/* One 2.8s beat: the scan line reads the page, three marks follow, then the page clears. */
.sheet-loader__scan {
  opacity: 0;
  animation: sheet-loader-scan 2.8s var(--ease-in-out) infinite;
}

.sheet-loader__scan rect:first-child { fill: rgba(63, 107, 79, 0.12); }
.sheet-loader__scan rect:last-child { fill: rgba(63, 107, 79, 0.7); }

.m1 { animation: sheet-loader-mark-1 2.8s var(--ease-out) infinite; }
.m2 { animation: sheet-loader-mark-2 2.8s var(--ease-out) infinite; }
.m3 { animation: sheet-loader-mark-3 2.8s var(--ease-out) infinite; }
.m4 { animation: sheet-loader-mark-4 2.8s var(--ease-out) infinite; }

@keyframes sheet-loader-scan {
  0% { opacity: 0; transform: translateY(0); }
  4%, 34% { opacity: 1; }
  40%, 100% { opacity: 0; transform: translateY(52px); }
}

@keyframes sheet-loader-mark-1 {
  0%, 30% { stroke-dashoffset: 1; opacity: 0; }
  31% { opacity: 1; }
  40%, 86% { stroke-dashoffset: 0; opacity: 1; }
  96%, 100% { stroke-dashoffset: 0; opacity: 0; }
}

@keyframes sheet-loader-mark-2 {
  0%, 42% { stroke-dashoffset: 1; opacity: 0; }
  43% { opacity: 1; }
  52%, 86% { stroke-dashoffset: 0; opacity: 1; }
  96%, 100% { stroke-dashoffset: 0; opacity: 0; }
}

@keyframes sheet-loader-mark-3 {
  0%, 54% { stroke-dashoffset: 1; opacity: 0; }
  55% { opacity: 1; }
  64%, 86% { stroke-dashoffset: 0; opacity: 1; }
  96%, 100% { stroke-dashoffset: 0; opacity: 0; }
}

@keyframes sheet-loader-mark-4 {
  0%, 64% { stroke-dashoffset: 1; opacity: 0; }
  65% { opacity: 1; }
  70%, 86% { stroke-dashoffset: 0; opacity: 1; }
  96%, 100% { stroke-dashoffset: 0; opacity: 0; }
}

/* Reduced motion: the finished, marked page at rest. */
@media (prefers-reduced-motion: reduce) {
  .sheet-loader__scan,
  .sheet-loader__mark {
    animation: none;
  }
}
</style>
