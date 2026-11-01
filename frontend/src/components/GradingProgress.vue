<template>
  <section class="grading-progress">
    <div class="grading-progress__line">
      <span class="grading-progress__count">
        {{ verb }} <b>{{ current }}</b><template v-if="known"> / {{ total }}</template> {{ unit }}
      </span>
      <span
        class="grading-progress__track"
        :class="{ 'is-indeterminate': !known }"
        role="progressbar"
        :aria-label="known ? `${verb} ${current} / ${total} ${unit}` : `${verb} ${current} ${unit}`"
        aria-valuemin="0"
        :aria-valuemax="known ? total : undefined"
        :aria-valuenow="known ? current : undefined"
      >
        <b :style="known ? { transform: `scaleX(${ratio})` } : undefined" />
      </span>
      <span v-if="elapsed" class="grading-progress__elapsed" aria-live="off">已用时 {{ elapsed }}</span>
    </div>
    <div v-if="live" class="grading-progress__live">
      <SheetLoader :size="88" />
      <p class="grading-progress__copy">
        <strong>{{ message }}</strong>
        <span v-if="hint">{{ hint }}</span>
      </p>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import SheetLoader from '@/components/SheetLoader.vue'

const props = defineProps({
  current: {
    type: Number,
    required: true,
  },
  // 0 while the server has not reported a total yet; the track then runs indeterminate.
  total: {
    type: Number,
    default: 0,
  },
  verb: {
    type: String,
    default: '已完成',
  },
  unit: {
    type: String,
    default: '份',
  },
  elapsed: {
    type: String,
    default: '',
  },
  message: {
    type: String,
    default: '',
  },
  hint: {
    type: String,
    default: '',
  },
  // Line only: no desk with the sheet loader and message.
  live: {
    type: Boolean,
    default: true,
  },
})

const known = computed(() => props.total > 0)
const ratio = computed(() => (known.value ? Math.min(1, Math.max(0, props.current / props.total)) : 0))
</script>

<style scoped>
/* The showcase specimen's page counter and desk, as one block inside a work card. */
.grading-progress {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--paper-raised);
}

.grading-progress__line {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 38px;
  padding: 8px 16px;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}

.grading-progress__count {
  flex: 0 0 auto;
  white-space: nowrap;
}

.grading-progress__count b {
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: var(--fs-lg);
  font-weight: 500;
}

.grading-progress__track {
  position: relative;
  flex: 1;
  height: 3px;
  min-width: 48px;
  overflow: hidden;
  border-radius: 3px;
  background: var(--fill);
}

.grading-progress__track b {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: var(--moss);
  transform: scaleX(0);
  transform-origin: left center;
  transition: transform 700ms var(--ease-emphasized);
}

.grading-progress__track.is-indeterminate b {
  transform: none;
  animation: ma-progress-indeterminate 1.4s var(--ease-in-out) infinite;
}

.grading-progress__elapsed {
  flex: 0 0 auto;
  white-space: nowrap;
}

.grading-progress__live {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 14px 16px;
  border-top: 1px solid var(--line);
  background: color-mix(in srgb, var(--paper-deep) 42%, var(--paper-raised));
}

.grading-progress__copy {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  margin: 0;
}

.grading-progress__copy strong {
  color: var(--ink);
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-snug);
  overflow-wrap: anywhere;
}

.grading-progress__copy span {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

@media (max-width: 520px) {
  .grading-progress__line {
    flex-wrap: wrap;
    row-gap: 6px;
  }

  .grading-progress__track {
    order: 3;
    flex-basis: 100%;
  }

  .grading-progress__elapsed {
    margin-left: auto;
  }

  .grading-progress__live {
    gap: 12px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .grading-progress__track.is-indeterminate b {
    animation: none;
    opacity: 0.6;
  }
}
</style>
