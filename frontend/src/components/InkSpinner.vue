<template>
  <!-- Decorative: the text beside the spinner carries the status. -->
  <svg class="ink-spinner" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path pathLength="1" d="M12.6 3.3 C 17.6 3.1, 20.9 7, 20.7 12 C 20.5 17.2, 16.6 20.8, 11.8 20.7 C 7 20.6, 3.3 16.9, 3.4 12 C 3.5 7.3, 7 3.6, 13.6 3.5" />
  </svg>
</template>

<style scoped>
/* A pen keeps re-tracing the hand-drawn review circle used on the showcase's marked pages. */
.ink-spinner {
  display: inline-block;
  width: 1.2em;
  height: 1.2em;
  flex: none;
  overflow: visible;
  color: var(--moss);
  vertical-align: -0.25em;
  animation: ink-spinner-turn 2.4s linear infinite;
}

.ink-spinner path {
  fill: none;
  stroke: currentColor;
  stroke-width: 2.4;
  stroke-linecap: round;
  stroke-dasharray: 0.72 0.28;
  animation: ink-spinner-trace 1.6s var(--ease-in-out) infinite;
}

@keyframes ink-spinner-turn {
  to { transform: rotate(360deg); }
}

@keyframes ink-spinner-trace {
  0% { stroke-dasharray: 0.08 0.92; stroke-dashoffset: 0; }
  50% { stroke-dasharray: 0.66 0.34; stroke-dashoffset: -0.2; }
  100% { stroke-dasharray: 0.08 0.92; stroke-dashoffset: -1; }
}

@media (prefers-reduced-motion: reduce) {
  .ink-spinner,
  .ink-spinner path {
    animation: none;
  }
}
</style>
