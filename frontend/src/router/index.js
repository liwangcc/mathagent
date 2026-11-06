import { createRouter, createWebHistory } from 'vue-router'
import { authorizeNavigation } from './guard.js'
import { routes } from './routes.js'

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(authorizeNavigation)

export default router
