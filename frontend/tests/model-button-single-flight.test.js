import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { installFetch } from './helpers/memoryFetch.js'
import {
  HOLD,
  button,
  choose,
  chooseCommand,
  flush,
  holdingBackend,
  isBlocked,
  mountView,
  route,
  signIn,
} from './helpers/viewsStageReports.js'

// Every button that starts a model call shows that it is busy while the call
// runs, and the action it triggers ignores another call meanwhile, even one
// that does not come through the (disabled) button: one request per action.

const NAME = 'model-triggering buttons expose a busy state and guard duplicate calls'

// Streaming model calls (fetch) that stay open until the page goes away.
function holdStreams(t) {
  return installFetch(t, () => new Promise(() => {}))
}

const nativeButton = (page, pattern) => page.root.find(el => el.tagName === 'button' && pattern.test(el.text))

const UPLOAD_AVAILABLE = (config) => {
  if (route(config) === 'get /batch-grade/availability') return { data: { status: 'available' } }
  if (route(config) === 'get /batch-grade/template/availability') return { data: { status: 'available', registration_hardware: { status: 'ready' } } }
  return { data: {} }
}

async function openUpload(t, { reply = UPLOAD_AVAILABLE, localStorage } = {}) {
  const { client, pinia } = await signIn({ stage: 'senior', localStorage })
  holdingBackend(client, reply)
  const streams = holdStreams(t)
  const { default: Upload } = await import('../src/views/Upload.vue?sfc')
  const page = await mountView(t, Upload, { path: '/upload', pinia })
  return { client, streams, page }
}

const image = name => new File(['synthetic image'], name, { type: 'image/jpeg' })

test(`${NAME}: Upload template recognition`, async (t) => {
  const { streams, page } = await openUpload(t)
  // The prepared template pages (choosing files runs browser image decoding).
  page.setup.templateFiles = [image('template-1.jpg')]
  await flush()
  const recognize = () => nativeButton(page, /识别模板|识别中\.\.\./)
  assert.equal(recognize().text, '识别模板')
  assert.equal(recognize().hasAttribute('disabled'), false)

  recognize().dispatch('click')
  await flush()
  assert.equal(recognize().text, '识别中...')
  assert.equal(recognize().hasAttribute('disabled'), true)
  recognize().dispatch('click')
  page.setup.onRecognizeTemplate()
  await flush()
  assert.deepEqual(streams.requests.map(request => request.url), ['/api/batch-grade/template/recognize'])
})

test(`${NAME}: Upload student recognition`, async (t) => {
  const { streams, page } = await openUpload(t)
  // A saved template and chosen student papers start recognition.
  page.setup.confirmedTemplateId = 'template-1'
  page.setup.fileList = [{ name: 'student-1.jpg', raw: image('student-1.jpg') }]
  await flush()
  assert.deepEqual(streams.requests.map(request => request.url), ['/api/batch-grade/recognize'])
  assert.ok(page.root.allByClass('card-title').some(title => /识别中\.\.\./.test(title.text)), 'the recognition card says it is recognizing')
  page.setup.runRecognize()
  await flush()
  assert.deepEqual(streams.requests.map(request => request.url), ['/api/batch-grade/recognize'], 'a running recognition is not started again')
})

test(`${NAME}: Upload grading`, async (t) => {
  // A recognized batch restored on opening the page goes on to grading.
  const batch = {
    batch_id: 'batch-1', status: 'recognized', can_grade: true, uploaded_papers: 1, total_papers: 1,
    papers: [{ paper_id: 'p1', filename: 'p1.jpg', identity: { status: 'matched', student_id: 5, name: '张三' } }],
    source_mode: 'template', template_id: 'template-1', teaching_context: { education_stage: 'senior' },
  }
  const { streams, page } = await openUpload(t, {
    localStorage: { 'mathagent_orientation_batch_id:1': 'batch-1' },
    reply: config => (route(config) === 'get /batch-grade/batch-1/orientation' ? { data: batch } : UPLOAD_AVAILABLE(config)),
  })
  assert.deepEqual(streams.requests.map(request => request.url), ['/api/batch-grade/confirm'])
  // Asked again while grading runs, both entry points refuse at once (a call
  // that went on would wait on the grading stream, which never ends here).
  const refused = call => Promise.race([call(), flush().then(() => 'still running')])
  assert.equal(await refused(() => page.setup.onConfirmGrade()), false, 'grading again is refused while grading runs')
  assert.equal(await refused(() => page.setup.onConfirmIdentitiesAndGrade()), false)
  // The identity dialog's confirm button, were it open now, says it is grading.
  page.setup.identityDialogVisible = true
  await flush()
  const confirm = nativeButton(page, /批改中\.\.\.|确认身份并开始批改/)
  assert.equal(confirm.text, '批改中...')
  assert.equal(confirm.hasAttribute('disabled'), true)
  confirm.dispatch('click')
  await flush()
  assert.deepEqual(streams.requests.map(request => request.url), ['/api/batch-grade/confirm'])
})

test(`${NAME}: AnswerSheet grading draft`, async (t) => {
  const session = {
    session_id: 's1', status: 'review', teaching_context: { education_stage: 'senior' },
    student_pages: [{ paper_id: 'page-1', filename: '张三.jpg', orientation_status: 'auto_passed', answers: [] }],
  }
  for (const uploading of [false, true]) {
    const { client, pinia } = await signIn({ stage: 'senior' })
    holdingBackend(client, (config) => {
      if (route(config) === 'get /answer-sheet/availability') return { data: { status: 'available' } }
      if (route(config) === 'get /answer-sheet/sessions/s1') {
        return { data: uploading ? { ...session, student_upload: { status: 'uploading', uploaded_count: 1 } } : session }
      }
      return { data: {} }
    })
    const streams = holdStreams(t)
    const { default: AnswerSheet } = await import('../src/views/AnswerSheet.vue?sfc')
    const page = await mountView(t, AnswerSheet, { path: '/answer-sheet?session=s1', pinia })
    const recognizeStudents = () => button(page, '执行识别')
    if (uploading) {
      // An unfinished upload batch blocks recognition even when nothing runs.
      assert.equal(recognizeStudents().hasAttribute('disabled'), true)
      page.unmount()
      streams.restore()
      continue
    }
    assert.equal(recognizeStudents().hasAttribute('disabled'), false)
    const draft = button(page, '生成统一批阅草稿')
    const build = draft.handlers.click
    build()
    await flush()
    assert.equal(button(page, '生成中...').hasAttribute('disabled'), true)
    assert.equal(button(page, '识别中...').hasAttribute('disabled'), true, 'other model buttons wait too')
    assert.match(page.root.byClass('busy-veil').text, /正在生成批阅草稿/)
    build()
    await flush()
    assert.deepEqual(streams.requests.map(request => request.url), ['/api/answer-sheet/sessions/s1/grading/draft/stream'])
    page.unmount()
    streams.restore()
  }
})

test(`${NAME}: Exercise generation`, async (t) => {
  const { client, pinia } = await signIn({ stage: 'senior' })
  holdingBackend(client, config => (route(config) === 'post /exercises/generate' ? HOLD : { data: {} }))
  const { default: Exercise } = await import('../src/views/Exercise.vue?sfc')
  const page = await mountView(t, Exercise, { path: '/exercise', pinia })
  const generate = button(page, '按题型从题库抽题')
  const click = generate.handlers.click
  click()
  await flush()
  const busy = button(page, '生成中...')
  assert.equal(busy.hasAttribute('disabled'), true)
  assert.equal(busy.hasAttribute('loading'), true)
  click()
  await flush()
  assert.equal(client.requests.filter(config => route(config) === 'post /exercises/generate').length, 1)
})

test(`${NAME}: Reports generation`, async (t) => {
  for (const classId of [1, null]) {
    const { client, pinia } = await signIn({ stage: 'senior', classId })
    holdingBackend(client, config => (route(config) === 'post /class/1/report' ? HOLD : { data: {} }))
    const { default: Reports } = await import('../src/views/Reports.vue?sfc')
    const page = await mountView(t, Reports, { path: '/reports', pinia })
    const classReport = button(page, '生成并下载')
    if (!classId) {
      // Without a current class nothing can be generated.
      for (const text of ['生成并下载', '准备并复制链接', '重新生成']) assert.equal(button(page, text).hasAttribute('disabled'), true, text)
      await classReport.dispatch('click')
      await flush()
      assert.deepEqual(client.requests.map(route), [])
      page.unmount()
      continue
    }
    const click = classReport.handlers.click
    click()
    await flush()
    const busy = button(page, '生成中...')
    assert.equal(busy.hasAttribute('loading'), true)
    assert.equal(busy.hasAttribute('disabled'), true)
    for (const text of ['准备并复制链接', '重新生成']) assert.equal(button(page, text).hasAttribute('disabled'), true, text)
    click()
    await flush()
    assert.equal(client.requests.filter(config => route(config) === 'post /class/1/report').length, 1)
    page.unmount()
  }
})

test(`${NAME}: StudentDetail report`, async (t) => {
  const { client, pinia } = await signIn({ stage: 'senior' })
  holdingBackend(client, (config) => {
    if (route(config) === 'post /student/1/report') return HOLD
    if (route(config) === 'get /student/1/detail') return { data: { student: { id: 1, name: '张三' } } }
    return { data: {} }
  })
  const { default: StudentDetail } = await import('../src/views/StudentDetail.vue?sfc')
  const page = await mountView(t, StudentDetail, { path: '/student/1/detail', pinia })
  chooseCommand(page, '学生个人报告')
  await flush()
  const busy = button(page, '生成中...')
  assert.equal(busy.hasAttribute('disabled'), true)
  assert.equal(busy.hasAttribute('loading'), true)
  assert.equal(page.root.find(el => el.tagName === 'el-dropdown').hasAttribute('disabled'), true, 'the report menu does not open meanwhile')
  chooseCommand(page, '致家长信', { force: true })
  await flush()
  assert.equal(client.requests.filter(config => route(config) === 'post /student/1/report').length, 1)
})

test('StudentDetail lists wrong questions by their original number and keeps the last snapshot when a reload fails', async (t) => {
  const { client, pinia } = await signIn({ stage: 'senior' })
  let failing = false
  holdingBackend(client, (config) => {
    if (route(config) !== 'get /student/1/detail') return { data: {} }
    if (failing) return { status: 503, data: { detail: '服务暂不可用' } }
    return { data: { student: { id: 1, name: '张三' }, wrong_questions: [{ question_no: '17(2)', question: '求函数的单调区间', wrong_count: 1, status: 'pending' }] } }
  })
  const { default: StudentDetail } = await import('../src/views/StudentDetail.vue?sfc')
  const page = await mountView(t, StudentDetail, { path: '/student/1/detail', pinia })
  const column = page.root.find(el => el.tagName === 'el-table-column' && el.getAttribute('label') === '原题号')
  assert.ok(column, 'wrong questions are listed by their original number')
  assert.equal(column.byTag('el-cell')[0].text, '17(2)')

  failing = true
  await button(page, '刷新').dispatch('click')
  await flush()
  const alert = () => page.root.byClass('load-error')
  assert.match(alert().text, /学生详情加载失败，当前数据可能不是最新，请重试。/)
  assert.equal(page.root.byTag('h2')[0].text, '张三', 'the last loaded data stays visible')

  failing = false
  await alert().byText('el-button', '重试').dispatch('click')
  await flush()
  assert.equal(alert() === null, true, 'a successful reload clears the error')
})

test(`${NAME}: BankPhotoImport recognition`, async (t) => {
  const { client, pinia } = await signIn({ stage: 'senior' })
  holdingBackend(client, config => (route(config) === 'post /bank/photo-import/recognize' ? HOLD : { data: [] }))
  const { default: BankPhotoImport } = await import('../src/views/BankPhotoImport.vue?sfc')
  const page = await mountView(t, BankPhotoImport, { path: '/bank/photo-import', pinia })
  const recognize = () => button(page, '识别并配对')
  assert.equal(recognize().hasAttribute('disabled'), true, 'nothing to recognize yet')

  const file = { name: 'q1.jpg', raw: image('q1.jpg') }
  page.root.find(el => el.tagName === 'el-upload' && el.classList.contains('dropper')).props['on-change'](file, [file])
  await flush()
  assert.equal(recognize().hasAttribute('disabled'), false)
  const click = recognize().handlers.click
  click()
  await flush()
  assert.equal(recognize().hasAttribute('disabled'), true)
  assert.equal(recognize().hasAttribute('loading'), true)
  click()
  await flush()
  assert.equal(client.requests.filter(config => route(config) === 'post /bank/photo-import/recognize').length, 1)
})

test(`${NAME}: ClassTable roster recognition`, async (t) => {
  const { client, pinia } = await signIn({ stage: 'senior' })
  holdingBackend(client, config => (route(config) === 'post /class/1/students/import-image' ? HOLD : { data: {} }))
  const { default: ClassTable } = await import('../src/views/ClassTable.vue?sfc')
  const page = await mountView(t, ClassTable, { path: '/class/1/students', pinia })
  await button(page, '添加学生').dispatch('click')
  await flush()
  await choose(page.root.find(el => el.tagName === 'el-tabs'), 'image')
  await flush()
  const recognize = () => button(page, '识别名单')
  assert.equal(recognize().hasAttribute('disabled'), true, 'no roster image yet')
  page.root.find(el => el.tagName === 'el-upload' && el.getAttribute('accept') === 'image/*')
    .props['on-change']({ name: 'roster.jpg', raw: image('roster.jpg') })
  await flush()
  assert.equal(recognize().hasAttribute('disabled'), false)
  const click = recognize().handlers.click
  click()
  await flush()
  assert.equal(isBlocked(recognize()), true)
  assert.equal(recognize().hasAttribute('loading'), true)
  click()
  await flush()
  assert.equal(client.requests.filter(config => route(config) === 'post /class/1/students/import-image').length, 1)
})

test('ClassTable more menu leads to reports and parent letters', async (t) => {
  const { client, pinia } = await signIn({ stage: 'senior' })
  holdingBackend(client, () => ({ data: {} }))
  const { default: ClassTable } = await import('../src/views/ClassTable.vue?sfc')
  const page = await mountView(t, ClassTable, { path: '/class/1/students', pinia })
  await chooseCommand(page, '报告与家长信')
  await flush()
  assert.equal(page.router.currentRoute.value.fullPath, '/reports')
})

test(`${NAME}: Settings connection test`, async (t) => {
  const { client, pinia } = await signIn({ stage: 'senior', user: { role: 'admin' } })
  holdingBackend(client, config => (route(config) === 'post /settings/test' ? HOLD : { data: {} }))
  const { default: Settings } = await import('../src/views/Settings.vue?sfc')
  const page = await mountView(t, Settings, { path: '/settings', pinia })
  // The chat interface's own test, beside its 保存 and 重新加载.
  const actions = () => button(page, '重新加载').parentNode
  const click = actions().byText('el-button', '测试连接').handlers.click
  click()
  await flush()
  const busy = actions().byText('el-button', '测试中...')
  assert.equal(busy.hasAttribute('disabled'), true)
  assert.equal(busy.hasAttribute('loading'), true)
  click()
  await flush()
  assert.equal(client.requests.filter(config => route(config) === 'post /settings/test').length, 1)
})
