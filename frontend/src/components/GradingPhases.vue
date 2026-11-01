<template>
  <ol class="grading-phases" aria-label="批阅阶段">
    <li
      v-for="(label, index) in labels"
      :key="label"
      :class="{ 'is-done': index < current, 'is-current': index === current }"
      :aria-current="index === current ? 'step' : undefined"
    >
      {{ label }}
    </li>
  </ol>
</template>

<script setup>
defineProps({
  labels: {
    type: Array,
    required: true,
  },
  current: {
    type: Number,
    default: 0,
  },
})
</script>

<style scoped>
/* The showcase specimen's phase strip: finished phases keep a moss dot, the current one is raised. */
.grading-phases {
  display: flex;
  flex: 0 0 auto;
  gap: 2px;
  margin: 0;
  padding: 3px;
  border-radius: 9px;
  background: var(--fill);
  font-family: var(--font-body);
  list-style: none;
}

.grading-phases li {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 9px;
  border-radius: 6px;
  color: var(--ink-faint);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  line-height: 1.5;
  white-space: nowrap;
  transition:
    background-color var(--dur-3) var(--ease-out),
    color var(--dur-3) var(--ease-out),
    box-shadow var(--dur-3) var(--ease-out);
}

.grading-phases li.is-done {
  color: var(--ink-muted);
}

.grading-phases li.is-done::before {
  content: '';
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--moss);
}

.grading-phases li.is-current {
  background: var(--paper-raised);
  box-shadow: 0 1px 1px rgba(31, 30, 29, 0.06), 0 0 0 1px var(--line);
  color: var(--moss-deep);
}

@media (max-width: 480px) {
  .grading-phases li {
    padding-inline: 6px;
  }
}
</style>
