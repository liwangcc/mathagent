<template>
  <el-popover
    :width="width"
    placement="bottom-start"
    trigger="click"
    popper-class="local-help-popover"
  >
    <template #reference>
      <button
        type="button"
        class="help-button"
        :aria-label="`查看${title}`"
        :title="`查看${title}`"
        @click.stop
      >
        <span aria-hidden="true">?</span>
      </button>
    </template>

    <div class="local-help-content">
      <strong class="local-help-title">{{ title }}</strong>
      <p v-if="content" class="local-help-description">{{ content }}</p>
      <ul v-if="items.length" class="local-help-list">
        <li v-for="item in items" :key="item">{{ item }}</li>
      </ul>
      <slot />
    </div>
  </el-popover>
</template>

<script setup>
defineProps({
  title: {
    type: String,
    default: '功能说明',
  },
  content: {
    type: String,
    default: '',
  },
  items: {
    type: Array,
    default: () => [],
  },
  width: {
    type: [Number, String],
    default: 320,
  },
})
</script>

<style>
.help-button {
  width: 22px;
  height: 22px;
  display: inline-grid;
  place-items: center;
  flex: 0 0 auto;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: var(--fill, #eef1ef);
  color: var(--ink-muted, #718096);
  font: 650 12px/1 var(--font-body, system-ui, sans-serif);
  vertical-align: middle;
  cursor: pointer;
  transition:
    color var(--dur-2, 0.18s) var(--ease-out, ease),
    background-color var(--dur-2, 0.18s) var(--ease-out, ease),
    transform var(--dur-1, 0.12s) var(--ease-out, ease);
}

.help-button:hover,
.help-button:focus-visible,
.help-button[aria-describedby] {
  background: var(--primary-soft, #edf7f2);
  color: var(--primary-dark, #1f5e4d);
  outline: none;
}

.help-button:focus-visible {
  box-shadow: var(--focus-ring);
}

.help-button:active {
  transform: scale(0.92);
}

.local-help-popover {
  max-width: min(360px, calc(100vw - 24px));
}

.local-help-content {
  color: var(--ink-soft, #485465);
  font-size: var(--fs-sm, 13px);
  line-height: 1.65;
}

.local-help-title {
  display: block;
  margin-bottom: 5px;
  color: var(--ink, #1f2937);
  font-size: 14px;
}

.local-help-description {
  margin: 0;
}

.local-help-list {
  margin: 6px 0 0;
  padding-left: 18px;
}

.local-help-list li + li {
  margin-top: 3px;
}
</style>
