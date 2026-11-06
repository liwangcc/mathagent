import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { renderedFiles, signedInTeacherRouter } from './helpers/appRouter.js'

const uploadUrl = new URL('../src/views/Upload.vue', import.meta.url)
const anonymousUrl = new URL('../src/views/AnonymousGrade.vue', import.meta.url)
const layoutUrl = new URL('../src/views/Layout.vue', import.meta.url)

test('anonymous grading is its own route inside the teacher layout', async () => {
  const router = await signedInTeacherRouter()
  await router.push('/anonymous-grade')
  const route = router.currentRoute.value
  assert.equal(route.name, 'anonymous-grade')
  assert.equal(route.meta.title, '无身份批阅')
  assert.deepEqual(renderedFiles(route), ['views/Layout.vue', 'views/AnonymousGrade.vue'])
})

test('the anonymous grading page is Upload in anonymous mode and has a menu entry', async () => {
  const [page, layout] = await Promise.all([
    readFile(anonymousUrl, 'utf8'),
    readFile(layoutUrl, 'utf8'),
  ])

  assert.match(page, /<Upload anonymous\s*\/>/)
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
