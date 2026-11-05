// Load frontend/src modules in a test the way the Vite build resolves them.
//
// Import this helper before anything else, then load the module under test
// with a dynamic import() so it resolves after the hooks are in place:
//
//   import './helpers/srcModules.js'
//   const { default: api } = await import('../src/api/index.js')
//
// The hooks add only what vite.config.js and Vite itself provide: the `@/`
// alias and `import.meta.env`. They never change what a module does.
import { register } from 'node:module'

register('./srcModuleHooks.js', import.meta.url)
