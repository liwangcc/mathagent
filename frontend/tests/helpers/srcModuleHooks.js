// Module hooks registered by helpers/srcModules.js.
//
// * `@/x` resolves into frontend/src like the `@` alias in vite.config.js,
//   trying `x`, `x.js` and `x/index.js`;
// * every frontend/src module sees an empty `import.meta.env`, as in a build
//   without VITE_* variables, unless it was imported with `?env=<JSON>`: that
//   module instance (and only it) sees those variables;
// * a single-file component cannot run under Node, so importing a `.vue`
//   file (a lazy route, App.vue) yields an empty component that only names
//   its file in `__file`, relative to src/ (@vitejs/plugin-vue sets the same
//   property in development, to the absolute path);
// * `element-plus` imported from frontend/src is helpers/elementPlus.js, whose
//   ElMessage records messages instead of rendering them;
// * `vue-router` imported from frontend/src is helpers/vueRouterMemory.js,
//   whose createWebHistory is the in-memory history.
import { statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = new URL('../../src/', import.meta.url)
const SUBSTITUTES = {
  'element-plus': new URL('./elementPlus.js', import.meta.url).href,
  'vue-router': new URL('./vueRouterMemory.js', import.meta.url).href,
}

const isFile = url => statSync(fileURLToPath(url), { throwIfNoEntry: false })?.isFile() ?? false

export async function resolve(specifier, context, nextResolve) {
  if (Object.hasOwn(SUBSTITUTES, specifier) && context.parentURL?.startsWith(SRC.href)) {
    return { url: SUBSTITUTES[specifier], shortCircuit: true }
  }
  if (!specifier.startsWith('@/')) return nextResolve(specifier, context)
  const target = new URL(specifier.slice(2), SRC)
  const candidates = [target, new URL(`${target.href}.js`), new URL(`${target.href}/index.js`)]
  return nextResolve((candidates.find(isFile) ?? target).href, context)
}

export async function load(url, context, nextLoad) {
  if (!url.startsWith(SRC.href)) return nextLoad(url, context)
  if (url.endsWith('.vue')) {
    const file = decodeURIComponent(new URL(url).pathname.slice(SRC.pathname.length))
    return { format: 'module', source: `export default { __file: ${JSON.stringify(file)} }`, shortCircuit: true }
  }
  const result = await nextLoad(url, context)
  if (result.format !== 'module') return result
  const env = JSON.stringify(JSON.parse(new URL(url).searchParams.get('env') ?? '{}'))
  return { ...result, source: `import.meta.env = ${env};\n${result.source}` }
}
