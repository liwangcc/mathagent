// Module hooks registered by helpers/srcModules.js.
//
// * `@/x` resolves into frontend/src like the `@` alias in vite.config.js,
//   trying `x`, `x.js` and `x/index.js`;
// * every frontend/src module sees an empty `import.meta.env`, as in a build
//   without VITE_* variables, unless it was imported with `?env=<JSON>`: that
//   module instance (and only it) sees those variables;
// * a single-file component imported as it is (a lazy route, App.vue) yields
//   an empty component that only names its file in `__file`, relative to src/
//   (@vitejs/plugin-vue sets the same property in development, to the
//   absolute path);
// * a single-file component imported with `?sfc` is compiled for real, the
//   way @vitejs/plugin-vue compiles it for the development server: its
//   <script setup> is not inlined into the render function, so a mounted
//   instance keeps every binding in `setupState`; static hoisting is off (a
//   test renderer would have to parse the pre-rendered HTML); <style> blocks
//   are left out, a scoped one still gives the component its scope id. The
//   .vue files it imports are compiled too. Node's loader reads the file;
//   these hooks never touch the disk;
// * the other files Vite turns into modules: `.json` gives its value as the
//   default export, `?url` the file's URL under /src/, `.css` nothing;
// * `element-plus` imported from frontend/src is helpers/elementPlus.js, whose
//   ElMessage and ElMessageBox record calls instead of rendering them;
// * `vue-router` imported from frontend/src is helpers/vueRouterMemory.js,
//   whose createWebHistory is the in-memory history.
import { createHash } from 'node:crypto'
import { statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { compileScript, compileTemplate, parse } from 'vue/compiler-sfc'

const SRC = new URL('../../src/', import.meta.url)
const SUBSTITUTES = {
  'element-plus': new URL('./elementPlus.js', import.meta.url).href,
  'vue-router': new URL('./vueRouterMemory.js', import.meta.url).href,
}

const isFile = url => statSync(fileURLToPath(url), { throwIfNoEntry: false })?.isFile() ?? false
const inSrc = url => url.startsWith(SRC.href)
const isCompiled = url => Boolean(url) && inSrc(url) && new URL(url).searchParams.has('sfc')
const srcPath = url => decodeURIComponent(new URL(url).pathname.slice(SRC.pathname.length))

export async function resolve(specifier, context, nextResolve) {
  if (Object.hasOwn(SUBSTITUTES, specifier) && context.parentURL && inSrc(context.parentURL)) {
    return { url: SUBSTITUTES[specifier], shortCircuit: true }
  }
  let resolved
  if (specifier.startsWith('@/')) {
    const target = new URL(specifier.slice(2), SRC)
    const candidates = [target, new URL(`${target.href}.js`), new URL(`${target.href}/index.js`)]
    resolved = await nextResolve((candidates.find(isFile) ?? target).href, context)
  } else {
    resolved = await nextResolve(specifier, context)
  }
  // A compiled component's own components are compiled as well.
  const url = new URL(resolved.url)
  if (isCompiled(context.parentURL) && inSrc(resolved.url) && url.pathname.endsWith('.vue')) {
    url.searchParams.set('sfc', '')
    return { ...resolved, url: url.href }
  }
  return resolved
}

const module = source => ({ format: 'module', source, shortCircuit: true })

export async function load(url, context, nextLoad) {
  if (!inSrc(url)) return nextLoad(url, context)
  const { pathname, searchParams } = new URL(url)
  const env = `import.meta.env = ${JSON.stringify(JSON.parse(searchParams.get('env') ?? '{}'))};\n`
  if (searchParams.has('url')) return module(`export default ${JSON.stringify(`/src/${srcPath(url)}`)}`)
  if (pathname.endsWith('.css')) return module('')
  if (pathname.endsWith('.json')) {
    const { source } = await nextLoad(url, { ...context, format: 'module' })
    return module(`export default ${String(source)}`)
  }
  if (pathname.endsWith('.vue')) {
    const file = srcPath(url)
    if (!searchParams.has('sfc')) return module(`export default { __file: ${JSON.stringify(file)} }`)
    const { source } = await nextLoad(url, { ...context, format: 'module' })
    return module(env + compileSfc(String(source), fileURLToPath(url), file))
  }
  const result = await nextLoad(url, context)
  if (result.format !== 'module') return result
  return { ...result, source: env + result.source }
}

function compileSfc(text, filename, file) {
  const id = createHash('sha256').update(file).digest('hex').slice(0, 8)
  const { descriptor, errors } = parse(text, { filename, sourceMap: false })
  if (errors.length) throw new SyntaxError(`${file}: ${errors.map(error => error.message).join('; ')}`)
  const scoped = descriptor.styles.some(style => style.scoped)
  const scopeId = `data-v-${id}`
  let code = 'const _sfc_main = {}'
  let bindings
  if (descriptor.script || descriptor.scriptSetup) {
    const script = compileScript(descriptor, {
      id,
      isProd: false,
      inlineTemplate: false,
      genDefaultAs: '_sfc_main',
      sourceMap: false,
    })
    bindings = script.bindings
    code = script.content
  }
  if (descriptor.template) {
    const template = compileTemplate({
      source: descriptor.template.content,
      ast: descriptor.template.ast,
      filename,
      id,
      scoped,
      slotted: descriptor.slotted,
      isProd: false,
      transformAssetUrls: false,
      compilerOptions: {
        bindingMetadata: bindings,
        scopeId: scoped ? scopeId : undefined,
        hoistStatic: false,
      },
    })
    if (template.errors.length) {
      throw new SyntaxError(`${file} template: ${template.errors.map(error => error.message ?? error).join('; ')}`)
    }
    code += `\n${template.code.replace(/\bexport function render\(/, 'function _sfc_render(')}`
    code += '\n_sfc_main.render = _sfc_render'
  }
  if (scoped) code += `\n_sfc_main.__scopeId = ${JSON.stringify(scopeId)}`
  code += `\n_sfc_main.__file = ${JSON.stringify(file)}`
  return `${code}\nexport default _sfc_main\n`
}
