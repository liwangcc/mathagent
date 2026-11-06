import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { createPinia, setActivePinia } from 'pinia'
import { renderedFiles, signedInTeacherRouter } from './helpers/appRouter.js'
import { assertAbsent, dialogTitled, signedInTeacher, teleportToBody } from './helpers/homeGuideLayout.js'
import { installFetch, sseResponse } from './helpers/memoryFetch.js'
import { mountComponent } from './helpers/mountComponent.js'

const { useAuthStore } = await import('../src/stores/auth.js')

test('anonymous grading is its own route inside the teacher layout', async () => {
  const router = await signedInTeacherRouter()
  await router.push('/anonymous-grade')
  const route = router.currentRoute.value
  assert.equal(route.name, 'anonymous-grade')
  assert.equal(route.meta.title, '无身份批阅')
  assert.deepEqual(renderedFiles(route), ['views/Layout.vue', 'views/AnonymousGrade.vue'])
})

// The grading backend a freshly opened page talks to: service available, no
// saved batch to resume.
function gradingBackend(client, reviews = {}) {
  client.respond(({ url }) => {
    if (url === '/batch-grade/availability') return { data: { status: 'available' } }
    if (url === '/batch-grade/template/availability') {
      return { data: { status: 'available', registration_hardware: { status: 'ready' } } }
    }
    const review = /^\/batch-grade\/review\/(.+)$/.exec(url)
    if (review) return reviews[decodeURIComponent(review[1])] ?? { status: 404, data: { detail: 'not found' } }
    if (url.startsWith('/batch-grade/report/')) return { data: { report: { status: 'failed', error: '无报告' } } }
    return { data: {} }
  })
}

test('the anonymous grading page is Upload in anonymous mode and has a menu entry', async (t) => {
  const client = await signedInTeacher()
  gradingBackend(client)
  const { default: AnonymousGrade } = await import('../src/views/AnonymousGrade.vue?sfc')
  const page = await mountComponent(t, AnonymousGrade, { path: '/anonymous-grade' })
  // The real grading workbench, in its anonymous mode.
  assert.equal(page.root.byClass('hero-band__eyebrow').text, '无身份批阅 · 批改工作台')
  assert.match(page.root.byClass('grade-card').text, /无身份试卷页面/)
  assertAbsent(page.root.byText('button', '学生上传'), 'no student submission links without identities')
  assertAbsent(page.root.byClass('identity-inherit-option'))
  page.unmount()

  teleportToBody(t)
  const layoutClient = await signedInTeacher({ storage: { disclaimer_agreed: 'true', 'mathagent_usage_guide_v3:7': 'true' } })
  layoutClient.respond(() => ({ data: {} }))
  const { default: Layout } = await import('../src/views/Layout.vue?sfc')
  const layout = await mountComponent(t, Layout, { path: '/anonymous-grade' })
  const entry = layout.root.find(el => el.tagName === 'el-menu-item' && el.getAttribute('index') === '/anonymous-grade')
  assert.ok(entry, 'the sidebar links to /anonymous-grade')
  assert.equal(entry.byTag('el-slot').find(slot => slot.getAttribute('name') === 'title').text, '无身份批阅')
  assert.equal(entry.parentNode.getAttribute('title'), '批阅', 'listed with the other grading entries')
  assert.equal(layout.root.find(el => el.tagName === 'el-menu').getAttribute('default-active'), '/anonymous-grade')
  assert.equal(layout.root.byClass('crumb-group').text, '批阅')
})

// A grading page whose template is saved and whose student image is chosen;
// the backend answers the recognition stream with `recognition` and records
// it. `beforeImages` runs on the page first, as a teacher sets options before
// choosing images; `storage` is what this browser kept from earlier visits.
async function chooseImages(t, { anonymous, classId = null, recognition, beforeImages, storage }) {
  const client = await signedInTeacher({ storage })
  gradingBackend(client)
  const backend = installFetch(t, recognition)
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().currentClassId = classId
  const { default: Upload } = await import('../src/views/Upload.vue?sfc')
  const page = await mountComponent(t, Upload, { path: anonymous ? '/anonymous-grade' : '/upload', props: { anonymous }, pinia })
  await beforeImages?.(page)
  // Image preparation (canvas compression) and template confirmation need a
  // browser; the page holds their results the way it does after both, which
  // is when it starts recognition on its own.
  page.setup.form.category = 'daily'
  page.setup.confirmedTemplateId = 'tpl-1'
  page.setup.fileList = [new File(['jpeg'], '第1页.jpg', { type: 'image/jpeg' })]
  await page.flush()
  return { client, backend, page }
}

const recognizeRequest = backend => backend.requests.find(({ url }) => url === '/api/batch-grade/recognize')
const never = () => new Promise(() => {})

test('anonymous grading sends its mode and bypasses student identity actions', async (t) => {
  // Anonymous: no class needed, the recognition request says anonymous and
  // carries no identity option, and grading sends no identity resolutions.
  // The teacher last chose to inherit identities on the identified page; the
  // anonymous page restores that choice but must not send it.
  const { backend, page } = await chooseImages(t, {
    anonymous: true,
    storage: { 'mathagent_inherit_identity:7': 'true' },
    recognition: ({ url }) => (url === '/api/batch-grade/recognize'
      ? sseResponse([
          ['paper', { batch_id: 'b-anon', paper_id: 'p1', filename: '第1页.jpg', identity: { status: 'unmatched', ocr_name: '张三' } }],
          ['done', { batch_id: 'b-anon', uploaded_papers: 1, total_papers: 1, succeeded_papers: 1, can_grade: true, questions: [{ question_no: '1' }] }],
        ])
      : never()),
  })
  assertAbsent(dialogTitled(page.root, '确认学生身份'), 'anonymous mode has no identity dialog at all')
  assert.equal(page.setup.inheritIdentity, true, 'the earlier choice was restored (it has no control on this page)')
  const recognize = recognizeRequest(backend)
  assert.ok(recognize, 'recognition starts without a class')
  const form = recognize.init.body
  assert.equal(form.get('anonymous'), 'true')
  assert.equal(form.get('inherit_identity'), null)
  assert.equal(form.get('template_id'), 'tpl-1')
  assert.equal(form.get('files').name, '第1页.jpg')

  const confirm = backend.requests.find(({ url }) => url === '/api/batch-grade/confirm')
  assert.ok(confirm, 'an unmatched name clue does not hold anonymous grading back')
  const payload = JSON.parse(confirm.init.body)
  assert.equal(payload.batch_id, 'b-anon')
  assert.deepEqual(payload.identity_resolutions, [])
  assertAbsent(dialogTitled(page.root, '确认学生身份'))
})

test('identified grading needs a class and sends no anonymous mode', async (t) => {
  const withoutClass = await chooseImages(t, { anonymous: false, recognition: never })
  assert.ok(dialogTitled(withoutClass.page.root, '确认学生身份'), 'identified grading keeps its identity dialog')
  assert.equal(recognizeRequest(withoutClass.backend), undefined, 'no class, no recognition')
  await withoutClass.page.root.find(el => el.getAttribute('data-tour') === 'start-grading').dispatch('click')
  await withoutClass.page.flush()
  assert.deepEqual(withoutClass.client.messages.map(({ message }) => message), ['请先在顶部选择或创建班级'])
  assert.equal(dialogTitled(withoutClass.page.root, '确认批次信息').getAttribute('data-open'), 'false')
  assert.deepEqual(withoutClass.backend.requests, [])
  withoutClass.page.unmount()

  const withClass = await chooseImages(t, {
    anonymous: false,
    classId: 3,
    recognition: never,
    beforeImages: async (page) => {
      const inherit = page.root.byClass('identity-inherit-option').find(el => el.tagName === 'input')
      inherit.checked = true
      await inherit.dispatch('change')
    },
  })
  const form = recognizeRequest(withClass.backend).init.body
  assert.equal(form.get('anonymous'), null)
  assert.equal(form.get('inherit_identity'), 'true')
})

const reviewPayload = (batchId, identityMode) => ({
  data: {
    batch_id: batchId,
    status: 'review_ready',
    identity_mode: identityMode,
    results: [{ paper_id: 'p1', review_id: 'r1', filename: '第1页.jpg', disposition: 'active', category: 'daily', result: { correct_count: 4, total_count: 5, correct_rate: 0.8 } }],
    summary: { total: 1 },
    report: { status: 'failed', error: '无报告' },
  },
})

async function reopenReview(t, { anonymous, savedBatch, payload }) {
  const key = anonymous ? 'mathagent_review_batch_id:anonymous:7' : 'mathagent_review_batch_id:7'
  const client = await signedInTeacher({ storage: { [key]: savedBatch } })
  gradingBackend(client, { [savedBatch]: payload })
  const { default: Upload } = await import('../src/views/Upload.vue?sfc')
  const page = await mountComponent(t, Upload, { path: anonymous ? '/anonymous-grade' : '/upload', props: { anonymous } })
  return { client, page }
}

test('a reopened review must come from the same grading mode', async (t) => {
  const anonymousReview = await reopenReview(t, { anonymous: true, savedBatch: 'b-anon', payload: reviewPayload('b-anon', 'anonymous') })
  assert.match(anonymousReview.page.root.byClass('summary-bar').text, /总计 1 页/)
  assert.ok(anonymousReview.page.root.byText('button', '排除全部页面'))
  assertAbsent(anonymousReview.page.root.byText('button', '归档已完成结果'), 'anonymous results are never archived to student records')
  anonymousReview.page.unmount()

  const identifiedInAnonymous = await reopenReview(t, { anonymous: true, savedBatch: 'b-id', payload: reviewPayload('b-id', 'identified') })
  assertAbsent(identifiedInAnonymous.page.root.byClass('summary-bar'), 'an identified batch does not open on the anonymous page')
  identifiedInAnonymous.page.unmount()

  const anonymousInIdentified = await reopenReview(t, { anonymous: false, savedBatch: 'b-anon', payload: reviewPayload('b-anon', 'anonymous') })
  assertAbsent(anonymousInIdentified.page.root.byClass('summary-bar'), 'an anonymous batch does not open on the identified page')
  anonymousInIdentified.page.unmount()

  const identified = await reopenReview(t, { anonymous: false, savedBatch: 'b-id', payload: reviewPayload('b-id', 'identified') })
  assert.match(identified.page.root.byClass('summary-bar').text, /总计 1 张/)
  assert.ok(identified.page.root.byText('button', '归档已完成结果'))
})
