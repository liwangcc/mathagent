// Mount a real frontend/src component in Node and drive it.
//
//   import './helpers/srcModules.js'
//   const client = await loadApiClient({ localStorage: { ... } })   // HTTP
//   const { default: Upload } = await import('../src/views/Upload.vue?sfc')
//   const page = await mountComponent(t, Upload, { path: '/upload' })
//   await page.root.byText('el-button', '开始识别').dispatch('click')
//   await page.flush()
//   page.root.byClass('recognition-summary').text
//
// The component is compiled the way the development build compiles it (see
// helpers/srcModuleHooks.js, `?sfc`), rendered by Vue's own runtime through a
// custom renderer onto helpers/memoryDom.js, with element-plus replaced by
// helpers/elementPlusStubs.js. It runs inside a real router (the app's route
// table on an in-memory history, so named routes resolve and route guards
// such as onBeforeRouteLeave work; other pages are empty components) and a
// fresh Pinia. Its lifecycle hooks run, including onMounted.
//
// Returns:
//   root     the mounted tree (an MElement: find, byText, byClass, dispatch, ...)
//   vm       the component's internal instance; `setup` is vm.setupState, every
//            binding of its <script setup> (refs unwrapped, writable)
//   router, pinia, app, win (helpers/memoryWindow.js)
//   errors   errors Vue caught while rendering or in hooks / handlers;
//            mounting fails if any happen before it returns, unless
//            `allowErrors` is set
//   warnings Vue's warnings
//   flush()  let pending promises, zero-delay timers and re-renders settle
// Everything is unmounted and the globals restored after the test.
import './srcModules.js'
import { createPinia } from 'pinia'
import { createRenderer, h, nextTick } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { answerMessageBoxes, boxes } from './elementPlus.js'
import { elementPlusStubs } from './elementPlusStubs.js'
import { installDomGlobals, MElement, memoryDocument, nodeOps, patchProp, toHtml } from './memoryDom.js'
import { installMemoryWindow } from './memoryWindow.js'

const { routes: appRoutes } = await import('../../src/router/routes.js')

// Pages of the app render straight into the root RouterView: the layout
// around them is a component of its own, mounted by its own tests.
const Passthrough = { name: 'MountedRouteOutlet', render: () => h(RouterView) }

function routesWithout(layout) {
  return appRoutes.map(record => (record.children ? { ...record, component: layout ?? Passthrough } : record))
}

export async function flush(rounds = 5) {
  for (let i = 0; i < rounds; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }
}

export async function mountComponent(t, component, {
  path = '/__mounted__',
  props = {},
  routes,
  layout,
  pinia = createPinia(),
  plugins = [],
  confirm = () => true,
  messageBox = () => 'confirm',
  allowErrors = false,
} = {}) {
  const win = installMemoryWindow({ confirm })
  const restoreDom = installDomGlobals()
  boxes.length = 0
  answerMessageBoxes(messageBox)

  const router = createRouter({
    history: createMemoryHistory(),
    routes: routes ?? [{ path: '/__mounted__', component: { render: () => null } }, ...routesWithout(layout)],
  })
  const target = router.resolve(path).matched.at(-1)
  if (!target) throw new Error(`no route matches ${path}`)
  target.components = { ...target.components, default: component }
  target.props = { ...target.props, default: props }

  const { createApp } = createRenderer({ ...nodeOps, patchProp })
  const app = createApp({ name: 'MountedApp', render: () => h(RouterView) })
  const errors = []
  const warnings = []
  const instances = []
  app.config.errorHandler = (err, _instance, info) => { errors.push({ err, info }) }
  app.config.warnHandler = (message) => { warnings.push(message) }
  app.mixin({ created() { instances.push(this.$) } })
  app.use(pinia)
  app.use(router)
  app.use(elementPlusStubs)
  for (const plugin of plugins) app.use(plugin)

  await router.push(path)
  await router.isReady()
  const root = new MElement('div')
  memoryDocument.roots.push(root)
  app.mount(root)

  let mounted = true
  const unmount = () => {
    if (!mounted) return
    mounted = false
    app.unmount()
    restoreDom()
    win.restore()
  }
  t?.after(unmount)

  await flush()
  if (errors.length && !allowErrors) {
    unmount()
    const [{ err, info }] = errors
    throw Object.assign(new Error(`mounting ${component.__file ?? 'component'} failed in ${info}: ${err?.message ?? err}`), { cause: err })
  }
  const vm = instances.find(instance => instance.type === component)
  return {
    root,
    vm,
    setup: vm?.setupState,
    router,
    pinia,
    app,
    win,
    errors,
    warnings,
    boxes,
    flush,
    unmount,
    html: () => toHtml(root),
  }
}
