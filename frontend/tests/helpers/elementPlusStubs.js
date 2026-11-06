// Stand-ins for the element-plus components of a page mounted on
// helpers/memoryDom.js. The real components need a browser DOM (teleports to
// body, document listeners, requestAnimationFrame, popper positioning); the
// stand-ins keep only the parts of their behaviour the page's own code relies
// on, so a test can see what the page renders and drive it:
//
// * every <el-x> renders as an <el-x> element carrying its attributes and
//   listeners (`el.handlers['update:modelValue'](value)` is what typing into
//   it would emit); the default slot renders in place, every named slot
//   inside <el-slot name="...">;
// * <el-button> ignores clicks while disabled or loading, as the real one;
// * <el-dialog> / <el-drawer> render their slots only while open (modelValue)
//   and emit open/opened and close/closed when that changes;
// * <el-table :data> with <el-table-column> renders an <el-cell> per row,
//   calling the column's slot with the real scoped-slot props { row, $index }
//   (or showing row[prop]);
// * the v-loading and v-infinite-scroll directives do nothing.
import * as ElementPlus from 'element-plus'
import { defineComponent, h, inject, nextTick, provide, watch } from 'vue'

const kebab = name => name.replace(/^El/, 'el').replace(/[A-Z]/g, c => `-${c.toLowerCase()}`)

const renderSlots = (slots, props = {}) => Object.entries(slots).map(([name, slot]) => (
  name === 'default' ? slot(props) : h('el-slot', { name }, slot(props))
))

const generic = name => defineComponent({
  name,
  inheritAttrs: false,
  setup(_props, { slots, attrs }) {
    return () => h(kebab(name), attrs, renderSlots(slots))
  },
})

const isOn = value => value !== undefined && value !== null && value !== false

const ElButton = defineComponent({
  name: 'ElButton',
  inheritAttrs: false,
  setup(_props, { slots, attrs }) {
    return () => {
      const { onClick, ...rest } = attrs
      const blocked = isOn(attrs.disabled) || isOn(attrs.loading)
      return h('el-button', { ...rest, onClick: blocked || !onClick ? undefined : onClick }, renderSlots(slots))
    }
  },
})

const overlay = name => defineComponent({
  name,
  inheritAttrs: false,
  props: { modelValue: Boolean },
  // update:modelValue stays a listener on the element: a test calls it to close.
  emits: ['open', 'opened', 'close', 'closed'],
  setup(props, { slots, attrs, emit }) {
    watch(() => props.modelValue, async (open) => {
      emit(open ? 'open' : 'close')
      await nextTick()
      emit(open ? 'opened' : 'closed')
    })
    return () => h(kebab(name), { ...attrs, 'data-open': String(props.modelValue) },
      props.modelValue ? renderSlots(slots) : [])
  },
})

const ROWS = Symbol('el-table rows')
const ElTable = defineComponent({
  name: 'ElTable',
  inheritAttrs: false,
  props: { data: { type: Array, default: () => [] } },
  setup(props, { slots, attrs }) {
    provide(ROWS, () => props.data)
    return () => h('el-table', attrs, [
      ...(slots.default?.() ?? []),
      props.data.length ? null : h('el-empty', attrs['empty-text'] ?? ''),
    ])
  },
})
const ElTableColumn = defineComponent({
  name: 'ElTableColumn',
  inheritAttrs: false,
  props: { prop: String, label: String },
  setup(props, { slots, attrs }) {
    const rows = inject(ROWS, () => [])
    return () => h('el-table-column', { ...attrs, prop: props.prop, label: props.label }, rows().map((row, $index) => (
      h('el-cell', { 'data-row': $index }, slots.default
        ? slots.default({ row, $index, column: { property: props.prop, label: props.label } })
        : String(row?.[props.prop] ?? ''))
    )))
  },
})

const SPECIAL = {
  ElButton,
  ElDialog: overlay('ElDialog'),
  ElDrawer: overlay('ElDrawer'),
  ElTable,
  ElTableColumn,
}

export const elementPlusStubs = {
  install(app) {
    for (const name of Object.keys(ElementPlus)) {
      if (/^El[A-Z]/.test(name)) app.component(name, SPECIAL[name] ?? generic(name))
    }
    app.directive('loading', {})
    app.directive('infinite-scroll', {})
  },
}
