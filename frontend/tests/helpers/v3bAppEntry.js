// Load the app entry (frontend/src/main.js) in a test, the way the Vite build
// loads it. Import this helper first; then each import of the entry with its
// own query string runs it once more, against the globals the test has set up
// by then (document, window, localStorage):
//
//   import './helpers/v3bAppEntry.js'
//   await import('../src/main.js?cold-start')
//
// See helpers/v3bAppEntryHooks.js for what it adds to helpers/srcModules.js.
//
// The libraries the entry uses are loaded here, before any test defines
// `window` or `document`: element-plus and vue-router look for a browser once,
// when they are first evaluated, and must keep their server-side behaviour, as
// they do under helpers/mountComponent.js.
import { register } from 'node:module'
import './srcModules.js'
import 'element-plus'
import '@element-plus/icons-vue'
import 'pinia'
import 'vue'
import 'vue-router'
import './elementPlus.js'

register('./v3bAppEntryHooks.js', import.meta.url)
