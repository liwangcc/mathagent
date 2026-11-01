import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const uploadUrl = new URL('../src/views/Upload.vue', import.meta.url)
const anonymousUrl = new URL('../src/views/AnonymousGrade.vue', import.meta.url)
const routerUrl = new URL('../src/router/index.js', import.meta.url)
const layoutUrl = new URL('../src/views/Layout.vue', import.meta.url)

test('anonymous grading has a dedicated routed page', async () => {
  const [page, router, layout] = await Promise.all([
    readFile(anonymousUrl, 'utf8'),
    readFile(routerUrl, 'utf8'),
    readFile(layoutUrl, 'utf8'),
  ])

  assert.match(page, /<Upload anonymous\s*\/>/)
  assert.match(router, /path: 'anonymous-grade'/)
  assert.match(layout, /index="\/anonymous-grade"/)
})

test('anonymous grading sends its mode and bypasses student identity actions', async () => {
  const source = await readFile(uploadUrl, 'utf8')

  assert.match(source, /fd\.append\('anonymous', 'true'\)/)
  assert.match(source, /!anonymous\.value && !auth\.currentClassId/)
  assert.match(source, /if \(anonymous\.value\) return \[\]/)
  assert.match(source, /v-if="!anonymous"[\s\S]*v-model="identityDialogVisible"/)
  assert.match(source, /v-if="!anonymous" class="btn btn-primary"[\s\S]*archiveAllReviews/)
  assert.match(source, /payloadIsAnonymous !== anonymous\.value/)
})
