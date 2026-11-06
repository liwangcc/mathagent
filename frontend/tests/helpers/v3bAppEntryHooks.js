// Module hooks registered by helpers/v3bAppEntry.js, on top of
// helpers/srcModuleHooks.js, so the app entry (main.js) can be imported as the
// Vite build imports it. They add three more of Vite's conventions:
//
// * a relative specifier without an extension ('./router',
//   './composables/useTheme') resolves to `x.js` or `x/index.js` when Node
//   finds no such file;
// * a stylesheet imported from a package ('element-plus/dist/index.css') is
//   an empty module, as Vite's CSS imports are under srcModuleHooks.js;
// * element-plus, which srcModuleHooks.js swaps for helpers/elementPlus.js,
//   also has its default export (the plugin main.js installs), through
//   helpers/v3bElementPlusEntry.js.
//
// They never read a file and never change what a module does.
const RECORDING_ELEMENT_PLUS = new URL('./elementPlus.js', import.meta.url).href
const ELEMENT_PLUS_WITH_DEFAULT = new URL('./v3bElementPlusEntry.js', import.meta.url).href
const UNRESOLVED = new Set(['ERR_MODULE_NOT_FOUND', 'ERR_UNSUPPORTED_DIR_IMPORT'])

const isRelative = specifier => specifier.startsWith('./') || specifier.startsWith('../')
const hasExtension = specifier => /\.[\w]+(?:\?.*)?$/.test(specifier.split('/').at(-1))

export async function resolve(specifier, context, nextResolve) {
  let resolved
  try {
    resolved = await nextResolve(specifier, context)
  } catch (error) {
    if (!UNRESOLVED.has(error?.code) || !isRelative(specifier) || hasExtension(specifier)) throw error
    for (const candidate of [`${specifier}.js`, `${specifier}/index.js`]) {
      try {
        return await nextResolve(candidate, context)
      } catch (retry) {
        if (!UNRESOLVED.has(retry?.code)) throw retry
      }
    }
    throw error
  }
  if (specifier === 'element-plus' && resolved.url === RECORDING_ELEMENT_PLUS && context.parentURL !== ELEMENT_PLUS_WITH_DEFAULT) {
    return { url: ELEMENT_PLUS_WITH_DEFAULT, shortCircuit: true }
  }
  return resolved
}

export async function load(url, context, nextLoad) {
  if (new URL(url).pathname.endsWith('.css')) return { format: 'module', source: '', shortCircuit: true }
  return nextLoad(url, context)
}
