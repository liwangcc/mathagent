import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import 'katex/dist/katex.min.css'

import App from './App.vue'
import router from './router'
import './styles/global.css'
import './styles/patterns.css'
import './styles/motion.css'
import { initTheme } from './composables/useTheme'

// Apply persisted brand palette and color mode before mount to avoid a flash of the wrong theme.
initTheme()

const app = createApp(App)

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

app.mount('#app')

// The startup plate grades one sample page with CSS that starts at first paint and
// lands its stamp at about 2s. Fade once that page is stamped, counted from navigation
// start, so a slow load never adds a fixed wait on top; with reduced motion the plate
// is static and leaves as soon as the app has mounted.
const STARTUP_PLATE_MS = 2100
const startupScreen = document.getElementById('mathagent-startup')
if (startupScreen) {
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const remaining = reducedMotion ? 0 : Math.max(0, STARTUP_PLATE_MS - performance.now())
  window.setTimeout(() => {
    startupScreen.classList.add('is-leaving')
    window.setTimeout(() => startupScreen.remove(), 400)
  }, remaining)
}
