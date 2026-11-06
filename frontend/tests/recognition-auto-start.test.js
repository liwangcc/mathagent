import assert from 'node:assert/strict'
import test from 'node:test'
import { sseResponse } from './helpers/memoryFetch.js'
import { formFields, HEALTHY_SERVICE, liveSse, mountUpload, paperFile } from './helpers/uploadPage.js'

// Student recognition starts by itself once the saved template and the
// prepared student pages are both there: the real Upload page, its own
// watcher and runRecognize, and the recognize request it sends.

const RECOGNIZE = 'POST /api/batch-grade/recognize'
const AVAILABILITY = 'GET /api/batch-grade/availability'
// The server answers a recognition run with an unfinished batch: the run ends
// (the page stops recognizing) and nothing else is started for it.
const UNFINISHED_RUN = [['done', { batch_id: 'b-auto', can_grade: false, uploaded_papers: 1, total_papers: 0, unresolved_papers: 1 }]]

// Every recognize request the page sends, recorded the way it is sent: whether
// auto start was still armed at that moment, the grading mode and the files.
async function harness(t, { recognize = () => sseResponse(UNFINISHED_RUN), ...options } = {}) {
  const launches = []
  const upload = await mountUpload(t, {
    timers: true,
    ...options,
    streams: {
      [RECOGNIZE]: (request) => {
        const fields = formFields(request.init.body)
        launches.push({ armed: upload.page.setup.recognitionAutoArmed, mode: fields.mode, files: [fields.files].flat().length })
        return recognize(request)
      },
    },
  })
  const launchFields = () => upload.fetches(RECOGNIZE).map(request => formFields(request.init.body))
  // Warnings and errors the page showed (choosing files reports its own success).
  const notices = () => upload.client.messages.filter(message => message.type !== 'success')
  return { upload, launches, launchFields, notices }
}

// The template editor saves the teacher-confirmed template and leaves its id
// here (the editor's own tests drive that save).
async function saveTemplate(upload, id = 'synthetic-template') {
  upload.page.setup.confirmedTemplateId = id
  await upload.settle()
}

function templateEditor(upload) {
  return upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '编辑本次模板')
}

// The dialog's close button.
async function closeTemplateEditor(upload) {
  await templateEditor(upload).handlers['update:modelValue'](false)
  await upload.settle()
}

for (const order of ['template-first', 'files-first']) {
  test('starts exactly once when both inputs become ready: ' + order, async (t) => {
    const { upload, launches, launchFields } = await harness(t)
    if (order === 'template-first') await saveTemplate(upload)
    else await upload.choosePapers([paperFile('a.png')])
    assert.equal(launches.length, 0)
    if (order === 'template-first') await upload.choosePapers([paperFile('a.png')])
    else await saveTemplate(upload)
    assert.deepEqual(launches, [{ armed: false, mode: 'precise', files: 1 }])
    const [fields] = launchFields()
    assert.equal(fields.template_id, 'synthetic-template')
    assert.equal(fields.files.name, 'a.ocr-ready-v1.jpg')
    assert.equal(upload.page.setup.recognizing, false, 'the run has ended')

    upload.page.setup.grading = true
    await upload.settle()
    upload.page.setup.grading = false
    await upload.settle()
    upload.answer(AVAILABILITY, { status: 'unavailable', label: '暂不可用', message: '维护中' })
    await upload.tick(15000)
    assert.match(upload.page.root.byClass('service-alert-global').text, /暂不可用/)
    upload.answer(AVAILABILITY, HEALTHY_SERVICE[AVAILABILITY])
    await upload.tick(15000)
    assert.equal(upload.page.root.byClass('service-alert-global'), null)
    assert.equal(launches.length, 1, 'completion and readiness polling must not restart the same upload')
  })
}

test('prepared student pages and a closed correction dialog still wait for saved template confirmation', async (t) => {
  const { upload, launches } = await harness(t)
  // Template recognition opens the correction dialog before anything is saved.
  upload.page.setup.templateDialogVisible = true
  await upload.settle()
  await upload.choosePapers([paperFile('a.png')])
  await closeTemplateEditor(upload)
  assert.equal(templateEditor(upload).attributes['data-open'], 'false')
  assert.equal(launches.length, 0, 'finishing correction alone must not start comparison or grading')
  await saveTemplate(upload, 'answers-and-geometry-confirmed')
  assert.equal(launches.length, 1)
})

// Each gate is set the way the page itself sets it while that work is going on.
const GATES = {
  templateDialogVisible: {
    block: async upload => { upload.page.setup.templateDialogVisible = true },
    clear: closeTemplateEditor,
  },
  imagePreprocessing: {
    block: async upload => { upload.page.setup.imagePreprocessing = true },
    clear: async upload => { upload.page.setup.imagePreprocessing = false },
  },
  imageExtracting: {
    block: async upload => { upload.page.setup.imageExtracting = true },
    clear: async upload => { upload.page.setup.imageExtracting = false },
  },
  recognizing: {
    block: async upload => { upload.page.setup.recognizing = true },
    clear: async upload => { upload.page.setup.recognizing = false },
  },
  grading: {
    block: async upload => { upload.page.setup.grading = true },
    clear: async upload => { upload.page.setup.grading = false },
  },
  // The service gate follows the page's 15 s availability poll.
  recognitionBlocksNewWork: {
    block: async (upload) => {
      upload.answer(AVAILABILITY, { status: 'unavailable', label: '暂不可用', message: '维护中' })
      await upload.tick(15000)
      assert.equal(upload.page.setup.recognitionBlocksNewWork, true)
    },
    clear: async (upload) => {
      upload.answer(AVAILABILITY, HEALTHY_SERVICE[AVAILABILITY])
      await upload.tick(15000)
    },
  },
}

for (const gate of ['templateDialogVisible', 'imagePreprocessing', 'imageExtracting', 'recognizing', 'grading', 'recognitionBlocksNewWork']) {
  test('waits silently for ' + gate + ', then starts', async (t) => {
    const { upload, launches, notices } = await harness(t)
    await upload.choosePapers([paperFile('a.png')])
    await GATES[gate].block(upload)
    await upload.settle()
    await saveTemplate(upload)
    assert.equal(launches.length, 0)
    assert.deepEqual(notices(), [])
    await GATES[gate].clear(upload)
    await upload.settle()
    assert.equal(launches.length, 1)
  })
}

test('waits for class and exam settings, preserving the selected grading mode', async (t) => {
  const { upload, launches, launchFields } = await harness(t, { classId: null })
  const root = upload.page.root
  await upload.select(root.byClass('batch-setup').find(el => el.tagName === 'select'), 'exam')
  await upload.select(root.find(el => el.tagName === 'select' && el.id === 'batch-grade-mode'), 'simple')
  await saveTemplate(upload)
  await upload.choosePapers([paperFile('a.png')])
  // The top navigation selects the class.
  upload.auth.currentClassId = 2
  await upload.settle()
  assert.equal(launches.length, 0)
  await upload.type(root.find(el => el.tagName === 'input' && el.getAttribute('placeholder') === '如：高一月考'), 'Synthetic exam')
  assert.deepEqual(launches, [{ armed: false, mode: 'simple', files: 1 }])
  const [fields] = launchFields()
  assert.equal(fields.category, 'exam')
  assert.equal(fields.exam_name, 'Synthetic exam')
})

test('anonymous mode can start without assigning a class', async (t) => {
  const identified = await harness(t, { classId: null })
  await saveTemplate(identified.upload)
  await identified.upload.choosePapers([paperFile('a.png')])
  assert.equal(identified.launches.length, 0)
  assert.deepEqual(identified.notices(), [], 'auto start waits for a class without a warning')
  identified.upload.unmount()

  const { upload, launches, launchFields } = await harness(t, { classId: null, props: { anonymous: true } })
  await saveTemplate(upload)
  await upload.choosePapers([paperFile('a.png')])
  assert.equal(launches.length, 1)
  assert.equal(launchFields()[0].anonymous, 'true')
})

test('manual confirmation and the queued watcher share one launch', async (t) => {
  const { upload, launches } = await harness(t)
  await upload.choosePapers([paperFile('a.png')])
  // The template is saved and 开始识别 is pressed before the watcher runs.
  upload.page.setup.confirmedTemplateId = 'synthetic-template'
  upload.page.setup.onBatchConfirm()
  await upload.settle()
  assert.equal(launches.length, 1)
  assert.equal(upload.page.setup.recognitionAutoArmed, false)
  const batchDialog = upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === '确认批次信息')
  assert.equal(batchDialog.attributes['data-open'], 'false')
})

test('a new prepared upload rearms once and waits for archive extraction', async (t) => {
  const { upload, launches, launchFields } = await harness(t)
  await saveTemplate(upload)
  await upload.choosePapers([paperFile('first.png')])
  assert.equal(launches.length, 1)
  assert.equal(upload.page.setup.recognizing, false)
  // An archive is still being extracted when its first pages are prepared.
  upload.page.setup.imageExtracting = true
  assert.equal(await upload.page.setup.processFiles([paperFile('second.png')], { fromExtraction: true }), true)
  await upload.settle()
  assert.equal(launches.length, 1)
  upload.page.setup.imageExtracting = false
  await upload.settle()
  assert.equal(launches.length, 2)
  assert.equal(launchFields()[1].files.name, 'second.ocr-ready-v1.jpg')

  // 开始新批次 clears the student pages: armed again, nothing to start yet.
  await upload.click('开始新批次')
  assert.equal(launches.length, 2)
  assert.equal(upload.page.setup.recognitionAutoArmed, true)
  assert.deepEqual(upload.page.setup.fileList, [])
  await upload.choosePapers([paperFile('third.png')])
  assert.equal(launches.length, 3)
})

test('a per-paper result appears while recognition is still in progress', async (t) => {
  const live = liveSse(t)
  const { upload } = await harness(t, { recognize: live.respond })
  await saveTemplate(upload)
  await upload.choosePapers([paperFile('a.png')])
  live.send('result', { paper_id: 'fast-paper', student_name: '快速试卷', correct: 1, total: 1 })
  await upload.settle()
  assert.equal(live.open, true, 'the recognition stream has not finished')
  assert.equal(upload.page.setup.recognizing, true)
  assert.deepEqual(upload.page.setup.progressList, [{ paper_id: 'fast-paper', student_name: '快速试卷', correct: 1, total: 1 }])
  const rows = upload.page.root.allByClass('review-row')
  assert.equal(rows.length, 1)
  assert.match(rows[0].text, /快速试卷/)
  assert.match(rows[0].text, /正确 1 \/ 1/)
})
