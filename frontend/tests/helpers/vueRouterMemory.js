// vue-router as frontend/src modules see it under helpers/srcModules.js: the real
// library, except that the browser history (which needs window and document) is
// the in-memory one, so src/router/index.js can be imported as it is.
export * from 'vue-router'
export { createMemoryHistory as createWebHistory } from 'vue-router'
