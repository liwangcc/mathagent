import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8080'

// The 批改 page (src/views/Upload.vue) renders parts of itself with components in src/views/upload/. They share
// the page's scope id, so the page's scoped styles reach their elements exactly as they did before the split.
// Every other component keeps the plugin's default id.
const UPLOAD_PAGE_PARTS = /^src\/views\/(Upload\.vue|upload\/[^/]+\.vue)$/
function componentId(path, source, isProduction, getHash) {
  if (UPLOAD_PAGE_PARTS.test(path)) return getHash('src/views/Upload.vue')
  return getHash(path + (isProduction ? source : ''))
}

export default defineConfig({
  plugins: [vue({ features: { componentIdGenerator: componentId } })],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('element-plus') || id.includes('@element-plus')) {
              return 'element-plus'
            }
            if (id.includes('katex')) {
              return 'katex'
            }
            if (id.includes('vue') || id.includes('pinia')) {
              return 'vue-vendor'
            }
            return 'vendor'
          }
        },
      },
    },
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      '/uploads': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
})
