import assert from 'node:assert/strict'
import test from 'node:test'
import { jsonResponse } from './helpers/memoryFetch.js'
import { brief, mountTemplateEditor } from './helpers/templateReviewFlow.js'
import { formFields, HEALTHY_SERVICE, liveSse, mountUpload, paperFile } from './helpers/uploadPage.js'

// The 识别模板 button of the real Upload page and the gates in front of the
// template import and of student recognition: which availability each one
// follows, and that a click explains what is missing instead of the button
// going dead.

const BATCH_AVAILABILITY = 'GET /api/batch-grade/availability'
const TEMPLATE_AVAILABILITY = 'GET /api/batch-grade/template/availability'
const TEMPLATE_RECOGNIZE = 'POST /api/batch-grade/template/recognize'
const RECOGNIZE = 'POST /api/batch-grade/recognize'
const ACCELERATOR_UNAVAILABLE = '加速节点不可用，请使用客户端'
// The page reads both availabilities again every 15 s.
const AVAILABILITY_POLL_MS = 15000

const templateState = (fields = {}) => ({ ...HEALTHY_SERVICE[TEMPLATE_AVAILABILITY], ...fields })

const isDisabled = el => el.disabled === true || el.hasAttribute('disabled')
const shown = (upload, type) => upload.client.messages.filter(message => message.type === type).map(message => message.message)

const templateCard = upload => upload.page.root.byClass('template-card')
const templateInput = upload => templateCard(upload).find(el => el.tagName === 'input' && el.type === 'file')
const recognizeButton = upload => templateCard(upload).byClass('actions')?.byTag('button')[0] ?? null
const templateAlert = upload => templateCard(upload).byClass('template-service-alert')
const alertText = alert => [alert.byTag('strong')[0].text, alert.byTag('span')[0].text]
const categorySelect = upload => templateCard(upload).byClass('batch-setup').byTag('select')[0]
const examNameInput = upload => templateCard(upload).find(el => el.tagName === 'input' && el.getAttribute('placeholder') === '如：高一月考')
const studentButton = (upload, text) => upload.page.root.byClass('grade-card').byText('button', text)
const uploadArea = upload => upload.page.root.byClass('upload-area')

// Pick template images the way the hidden file input of 导入模板 does. The
// page prepares them before they count; a test holding that preparation
// back sees the page while it runs.
async function chooseTemplate(upload, files = [paperFile('template-1.png')]) {
  const input = templateInput(upload)
  if (isDisabled(input)) throw new Error('the template file input is disabled')
  input.files = files
  input.dispatch('change').catch(err => upload.page.errors.push({ err, info: 'template change handler' }))
  await upload.settle()
}

// Hold createImageBitmap, the first step of preparing an image, until release().
// Release puts the page's stand-in back only while this hold is still
// installed: the page's unmount (registered earlier, so run earlier by
// t.after) removes the stand-in, and restoring after it would leak the
// stand-in into later tests. A test that fails while holding is released
// at its end too: images are prepared one batch at a time across the whole
// module, so a batch left waiting would hold every later test's images back.
function holdImagePreparation(t) {
  const original = globalThis.createImageBitmap
  let open
  const gate = new Promise((resolve) => { open = resolve })
  const held = async (...args) => { await gate; return original(...args) }
  globalThis.createImageBitmap = held
  const release = () => {
    open()
    if (globalThis.createImageBitmap === held) globalThis.createImageBitmap = original
  }
  t.after(release)
  return release
}

// Whether a click opens the template file chooser (the page clicks the hidden input).
function watchFileChooser(upload) {
  const opened = []
  templateInput(upload).addEventListener('click', () => opened.push('template'))
  return opened
}

test('template recognition button lets click validation explain missing fields', async (t) => {
  const live = liveSse(t)
  const upload = await mountUpload(t, { timers: true, streams: { [TEMPLATE_RECOGNIZE]: live.respond } })
  const sent = () => upload.fetches(TEMPLATE_RECOGNIZE)

  // Nothing to recognize yet: the button is off; its handler names what is missing.
  assert.equal(recognizeButton(upload).text, '识别模板')
  assert.equal(isDisabled(recognizeButton(upload)), true)
  await upload.page.setup.onRecognizeTemplate()
  assert.deepEqual(shown(upload, 'warning'), ['请先导入模板图片'])

  // 清空 (no template yet) also clears the batch type. A chosen template
  // still turns the button on, and its click explains the missing type.
  await upload.click('清空')
  await chooseTemplate(upload)
  assert.match(templateCard(upload).byClass('file-summary').text, /已选 1 页/)
  assert.equal(isDisabled(recognizeButton(upload)), false)
  await upload.click('识别模板')
  assert.deepEqual(shown(upload, 'warning'), ['请先导入模板图片', '请先选择本次批阅类型'])
  assert.equal(sent().length, 0)

  // An exam without its name: the button stays on and the click asks for it.
  await upload.select(categorySelect(upload), 'exam')
  assert.equal(examNameInput(upload).value, '')
  assert.equal(isDisabled(recognizeButton(upload)), false)
  await upload.click('识别模板')
  assert.deepEqual(shown(upload, 'warning').slice(2), ['请先填写考试名称'])
  assert.equal(sent().length, 0)

  await upload.type(examNameInput(upload), '期中考试')
  await upload.click('识别模板')
  assert.equal(sent().length, 1)
  const fields = formFields(sent()[0].init.body)
  assert.equal(fields.category, 'exam')
  assert.equal(fields.exam_name, '期中考试')
  // While the import runs the button is off.
  assert.equal(recognizeButton(upload).text, '识别中...')
  assert.equal(isDisabled(recognizeButton(upload)), true)
  assert.deepEqual(upload.page.errors, [])
})

test('template images being prepared keep the template import closed until they are ready', async (t) => {
  const live = liveSse(t)
  const upload = await mountUpload(t, { timers: true, streams: { [TEMPLATE_RECOGNIZE]: live.respond } })
  // A first template is ready, so the button is on.
  await chooseTemplate(upload)
  assert.equal(templateCard(upload).byClass('template-upload').getAttribute('aria-busy'), null)
  assert.equal(isDisabled(recognizeButton(upload)), false)

  // The teacher picks another template: while it is prepared the import is
  // closed, the already chosen template included (it is about to be replaced).
  const release = holdImagePreparation(t)
  await chooseTemplate(upload, [paperFile('template-2.png')])
  const area = templateCard(upload).byClass('template-upload')
  assert.equal(area.getAttribute('aria-busy'), 'true')
  assert.ok(area.classList.contains('is-disabled'))
  assert.match(area.text, /正在逐张处理模板图片/)
  assert.equal(isDisabled(templateInput(upload)), true)
  assert.equal(isDisabled(recognizeButton(upload)), true)
  // Clicking the import area while it works explains the wait instead of opening the chooser.
  const opened = watchFileChooser(upload)
  await area.dispatch('click')
  assert.deepEqual(shown(upload, 'warning'), ['模板图片正在处理中，请稍候'])
  assert.deepEqual(opened, [])

  release()
  await upload.settle()
  assert.equal(templateCard(upload).byClass('template-upload').getAttribute('aria-busy'), null)
  assert.equal(isDisabled(templateInput(upload)), false)
  assert.equal(isDisabled(recognizeButton(upload)), false)
  // What is recognized is the new template, never the one it replaced.
  await upload.click('识别模板')
  const [sent] = upload.fetches(TEMPLATE_RECOGNIZE)
  assert.deepEqual(sent.init.body.getAll('files').map(file => file.name), ['template-2.ocr-ready-v1.jpg'])
  assert.deepEqual(upload.page.errors, [])
})

test('the template gate follows the template model status, not the batch service or the registration hardware', async (t) => {
  const upload = await mountUpload(t, { timers: true, streams: { [TEMPLATE_RECOGNIZE]: () => jsonResponse({ detail: '服务器繁忙' }, { status: 503 }) } })
  // The page asks the template service for its own availability.
  assert.equal(upload.requests(TEMPLATE_AVAILABILITY).length, 1)
  await chooseTemplate(upload)
  assert.equal(isDisabled(recognizeButton(upload)), false)
  const opened = watchFileChooser(upload)
  const refresh = async (answer) => {
    upload.answer(TEMPLATE_AVAILABILITY, answer)
    await upload.tick(AVAILABILITY_POLL_MS)
  }

  // The template model is down although the service answers: the import closes.
  await refresh(templateState({ status: 'available', service_status: 'unavailable', label: '模型暂不可用', message: '模板模型维护中' }))
  assert.equal(isDisabled(recognizeButton(upload)), true)
  assert.equal(isDisabled(templateInput(upload)), true)
  const area = templateCard(upload).byClass('template-upload')
  assert.ok(area.classList.contains('is-disabled'))
  assert.equal(area.getAttribute('aria-disabled'), 'true')
  assert.equal(templateAlert(upload).getAttribute('role'), 'alert')
  assert.deepEqual(alertText(templateAlert(upload)), ['模型暂不可用', '模板模型维护中'])
  await area.dispatch('click')
  assert.deepEqual(shown(upload, 'error'), ['模板模型维护中'])
  assert.deepEqual(opened, [])
  // The recognize handler holds the same gate itself (the template is chosen,
  // the batch type set): it explains instead of sending the import.
  await upload.page.setup.onRecognizeTemplate()
  await upload.settle()
  assert.equal(upload.fetches(TEMPLATE_RECOGNIZE).length, 0)
  assert.deepEqual(shown(upload, 'error'), ['模板模型维护中', '模板模型维护中'])

  // The model status decides over the service status the other way round too.
  await refresh(templateState({ status: 'unavailable', service_status: 'available' }))
  assert.equal(isDisabled(recognizeButton(upload)), false)
  assert.equal(brief(templateAlert(upload)), null)
  await templateCard(upload).byClass('template-upload').dispatch('click')
  assert.deepEqual(opened, ['template'])

  // An answer the page cannot read closes the import with its own message.
  await refresh({ status: 'unknown' })
  assert.equal(isDisabled(recognizeButton(upload)), true)
  assert.deepEqual(alertText(templateAlert(upload)), ['模板链路故障', '模板识别链路当前不可用，请稍后再试。'])

  // The student registration accelerator (and any other hardware it reports)
  // closes student recognition only; the template import stays open.
  await refresh(templateState({ registration_hardware: { status: 'waiting_accelerator' }, hardware: { status: 'unavailable' } }))
  assert.equal(isDisabled(recognizeButton(upload)), false)
  assert.equal(isDisabled(templateInput(upload)), false)
  assert.equal(brief(templateAlert(upload)), null)
  assert.equal(uploadArea(upload).getAttribute('aria-disabled'), 'true')
  assert.equal(isDisabled(studentButton(upload, '选择文件')), true)

  // A failed import marks the batch service faulty, which closes student
  // uploads; the template import has its own gate, so the teacher can retry.
  await refresh(templateState())
  await upload.click('识别模板')
  assert.equal(upload.fetches(TEMPLATE_RECOGNIZE).length, 1)
  assert.match(upload.page.root.byClass('service-alert-global').text, /链路故障/)
  assert.equal(isDisabled(studentButton(upload, '选择文件')), true)
  assert.equal(isDisabled(recognizeButton(upload)), false)
  await upload.click('识别模板')
  assert.equal(upload.fetches(TEMPLATE_RECOGNIZE).length, 2)
})

test('template import branch is rendered directly instead of hidden in a native template element', async (t) => {
  // No saved template: the import area and its button are on the page.
  const fresh = await mountUpload(t)
  let card = templateCard(fresh)
  assert.match(card.byClass('template-upload').text, /导入模板 N 页/)
  assert.ok(templateInput(fresh))
  assert.equal(recognizeButton(fresh).text, '识别模板')
  assert.equal(brief(card.byClass('tpl-confirmed')), null)
  assert.deepEqual(fresh.page.root.byTag('template').map(brief), [])
  fresh.unmount()

  // A saved template takes the import's place; 重新导入模板 brings it back.
  const editor = await mountTemplateEditor(t, {
    pages: [{ page_index: 0, questions: [{ question_no: '1', question_type: 'fill', blank_count: 1, standard_answer: '7', question_text: '解方程 x + 3 = 10' }] }],
  })
  await editor.save()
  card = templateCard(editor)
  assert.match(card.byClass('tpl-confirmed').text, /本次模板已保存（1 页）/)
  assert.equal(brief(card.byClass('template-upload')), null)
  assert.equal(brief(recognizeButton(editor)), null)
  await editor.click('重新导入模板')
  assert.equal(brief(card.byClass('tpl-confirmed')), null)
  assert.match(card.byClass('template-upload').text, /导入模板 N 页/)
  assert.equal(recognizeButton(editor).text, '识别模板')
  assert.deepEqual(editor.page.root.byTag('template').map(brief), [])
  assert.deepEqual(editor.page.errors, [])
})

test('student recognition requires the confirmed template and registration hardware', async (t) => {
  const live = liveSse(t)
  const upload = await mountUpload(t, { timers: true, streams: { [RECOGNIZE]: live.respond } })
  const start = () => studentButton(upload, '开始批阅')
  const refresh = async (answer) => {
    upload.answer(TEMPLATE_AVAILABILITY, answer)
    await upload.tick(AVAILABILITY_POLL_MS)
  }
  assert.equal(isDisabled(start()), true, 'no papers yet')
  await upload.choosePapers([paperFile('a.png')])
  assert.equal(isDisabled(start()), true, 'no saved template yet')

  // The registration accelerator goes away (not just advisory): student uploads close.
  await refresh(templateState({ registration_hardware: { status: 'waiting_accelerator' } }))
  assert.equal(uploadArea(upload).getAttribute('aria-disabled'), 'true')
  assert.equal(isDisabled(studentButton(upload, '选择文件')), true)
  // Saving the template does not start recognition, and nothing else can.
  upload.page.setup.confirmedTemplateId = 'tpl-1'
  await upload.settle()
  assert.equal(isDisabled(start()), true)
  await upload.page.setup.runRecognize()
  upload.page.setup.onStartRecognize()
  await upload.settle()
  assert.deepEqual(shown(upload, 'error'), [ACCELERATOR_UNAVAILABLE, ACCELERATOR_UNAVAILABLE])
  assert.equal(upload.fetches(RECOGNIZE).length, 0)
  assert.equal(brief(upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes['data-open'] === 'true')), null)
  // The page has no separate registration advisory to show.
  assert.equal(brief(upload.page.root.byClass('registration-service-alert')), null)
  assert.doesNotMatch(upload.text(), /配准提示|加速节点状态仅供参考，不影响本次识别/)

  // The accelerator is back: the papers are recognized against the template.
  await refresh(templateState())
  assert.equal(upload.fetches(RECOGNIZE).length, 1)
  assert.equal(formFields(upload.fetches(RECOGNIZE)[0].init.body).template_id, 'tpl-1')
})

test('an advisory registration state never holds student recognition back', async (t) => {
  const live = liveSse(t)
  const upload = await mountUpload(t, {
    timers: true,
    responses: { [TEMPLATE_AVAILABILITY]: templateState({ registration_hardware: { status: 'waiting_accelerator', advisory: true } }) },
    streams: { [RECOGNIZE]: live.respond },
  })
  assert.equal(uploadArea(upload).getAttribute('aria-disabled'), null)
  await upload.choosePapers([paperFile('a.png')])
  upload.page.setup.confirmedTemplateId = 'tpl-1'
  await upload.settle()
  assert.equal(upload.fetches(RECOGNIZE).length, 1)
  assert.deepEqual(shown(upload, 'error'), [])
  assert.equal(brief(upload.page.root.byClass('registration-service-alert')), null)
  assert.doesNotMatch(upload.text(), /配准提示|加速节点状态仅供参考，不影响本次识别/)
})

test('the start button opens once papers, the saved template and both services are ready', async (t) => {
  // Without a selected class the page does not start by itself, so the
  // button's own state shows.
  const upload = await mountUpload(t, { timers: true, classId: null })
  const start = () => studentButton(upload, '开始批阅')
  upload.page.setup.confirmedTemplateId = 'tpl-1'
  await upload.settle()
  assert.equal(isDisabled(start()), true, 'no papers yet')

  await upload.choosePapers([paperFile('a.png')])
  assert.equal(uploadArea(upload).getAttribute('aria-busy'), null)
  assert.equal(isDisabled(start()), false, 'papers, template and services are ready')

  // Papers chosen again are prepared before they replace the earlier ones;
  // meanwhile the button is off although the earlier papers are still listed.
  const release = holdImagePreparation(t)
  await upload.choosePapers([paperFile('b.png')])
  assert.equal(uploadArea(upload).getAttribute('aria-busy'), 'true')
  assert.match(upload.page.root.byClass('grade-card').byClass('file-summary').text, /已选 1 个文件/)
  assert.equal(isDisabled(start()), true, 'papers still being prepared')
  release()
  await upload.settle()
  assert.equal(uploadArea(upload).getAttribute('aria-busy'), null)
  assert.equal(isDisabled(start()), false)
  await upload.click('开始批阅')
  assert.deepEqual(shown(upload, 'warning'), ['请先在顶部选择或创建班级'])

  // The batch service going down closes it as well, with the service's message.
  upload.answer(BATCH_AVAILABILITY, { status: 'unavailable', label: '暂不可用', message: '批改服务维护中' })
  await upload.tick(AVAILABILITY_POLL_MS)
  assert.equal(isDisabled(start()), true)
  await upload.page.setup.runRecognize()
  assert.deepEqual(shown(upload, 'error'), ['批改服务维护中'])
  assert.equal(upload.fetches(RECOGNIZE).length, 0)
})
