import assert from 'node:assert/strict'
import test from 'node:test'
import { nextTick } from 'vue'
import { sseResponse } from './helpers/memoryFetch.js'
import { brief } from './helpers/templateReviewFlow.js'
import { formFields, liveSse, mountUpload, paperFile, TEACHER } from './helpers/uploadPage.js'

// 已确认模板是本次批阅的唯一权威：识别完成后直接批阅，页面上没有"确认参考答案"步骤。
// 这些测试在真实的 Upload 页面上走完识别 → 批阅，防止后续改动重新引入客户端答案确认页
// 或无模板分支：批阅请求由识别完成自动发出，中间不弹出任何窗口（身份确认除外），
// 请求里也不带客户端答案。

const RECOGNIZE = 'POST /api/batch-grade/recognize'
const CONFIRM = 'POST /api/batch-grade/confirm'
const ORIENTATION_POINTER = `mathagent_orientation_batch_id:${TEACHER.id}`
const orientationRoute = batchId => `GET /api/batch-grade/${batchId}/orientation`
const resumeRoute = batchId => `POST /api/batch-grade/${batchId}/recognize/resume`
const IDENTITY_OPTIONS = 'GET /api/batch-grade/b1/identity-options'
const PAPER_IMAGE = 'GET /api/batch-grade/b1/paper/p1/image'
const ORIENTATION_CONFIRM = 'POST /api/batch-grade/b1/paper/p1/orientation-confirm'
const QUESTIONS = [{ question_no: '1', question_type: 'choice' }]

const MATCHED = { status: 'matched', student_id: 1 }
const recognizedPaper = (fields = {}) => ({ batch_id: 'b1', paper_id: 'p1', filename: 'a.png', student_name: '甲', identity: MATCHED, ...fields })
// The server's `done` for a batch whose every paper was recognized.
const COMPLETE = { batch_id: 'b1', can_grade: true, uploaded_papers: 1, total_papers: 1, questions: QUESTIONS }
const recognized = (paper = recognizedPaper(), done = COMPLETE) => [['paper', paper], ['done', done]]

// The durable batch state the server returns for a saved recognition pointer.
function batchState(fields = {}) {
  return {
    batch_id: 'b1',
    category: 'daily',
    mode: 'precise',
    source_mode: 'template',
    template_id: 'tpl-saved',
    can_grade: true,
    uploaded_papers: 1,
    total_papers: 1,
    questions: QUESTIONS,
    papers: [{ paper_id: 'p1', filename: 'a.png', student_name: '甲', identity: MATCHED }],
    ...fields,
  }
}

const UNMATCHED_PAPER = { paper_id: 'p1', filename: 'a.png', student_name: '', ocr_name: '甲某', identity: { status: 'unmatched' } }

// What the page shows when grading is requested, recorded by the confirm
// stream itself once the page has rendered what led to the request: the
// student card's step and title and every open dialog.
function gradingServer(page) {
  const atConfirm = []
  const confirm = async () => {
    await nextTick()
    const root = page().root
    const card = root.byClass('grade-card')
    atConfirm.push({
      step: card.dataset.step,
      title: card.byClass('card-title').text.split(' ')[0].replace('?', ''),
      dialogs: root.findAll(el => el.tagName === 'el-dialog' && el.attributes['data-open'] === 'true').map(el => el.attributes.title),
    })
    return sseResponse([
      ['result', { paper_id: 'p1', student_name: '甲', correct: 1, total: 1, disposition: 'active' }],
      ['done', { total: 1, avg_score: null }],
    ])
  }
  const responses = {
    'GET /api/batch-grade/review/b1': () => ({ status: 200, data: {
      batch_id: 'b1',
      status: 'review_ready',
      identity_mode: page().setup.anonymous ? 'anonymous' : 'identified',
      results: [{ paper_id: 'p1', student_name: '甲', category: 'daily', disposition: 'active', correct: 1, total: 1 }],
      summary: { total: 1 },
    } }),
    'GET /api/batch-grade/report/b1': { report: { status: 'failed', error: '报告未生成' } },
  }
  return { atConfirm, confirm, responses }
}

// The real Upload page with a server that recognizes and grades one paper.
async function gradingPage(t, { recognize = recognized(), responses = {}, streams = {}, ...options } = {}) {
  let upload
  const server = gradingServer(() => upload.page)
  upload = await mountUpload(t, {
    timers: true,
    ...options,
    streams: { [RECOGNIZE]: recognize, [CONFIRM]: server.confirm, ...streams },
    responses: { ...server.responses, ...responses },
  })
  return Object.assign(upload, {
    atConfirm: server.atConfirm,
    confirmed: () => upload.fetches(CONFIRM).map(request => JSON.parse(request.init.body)),
    gradeCard: () => upload.page.root.byClass('grade-card'),
    dialog: title => upload.page.root.find(el => el.tagName === 'el-dialog' && el.attributes.title === title),
    openDialogs: () => upload.page.root.findAll(el => el.tagName === 'el-dialog' && el.attributes['data-open'] === 'true').map(el => el.attributes.title),
    alerts: () => upload.page.root.findAll(el => el.getAttribute('role') === 'alert').map(el => el.text),
  })
}

// The template editor saves the teacher-confirmed template and leaves its id
// here (the editor's own tests drive that save).
async function saveTemplate(upload, id = 'tpl-1') {
  upload.page.setup.confirmedTemplateId = id
  await upload.settle()
}

// Where grading is asked for: on the recognition card (03) with no dialog
// open. The stream that recognized the papers hands over to grading itself,
// so the card still reads 识别中...; a finished batch reads 识别完成.
const WHILE_RECOGNIZING = { step: '03', title: '识别中...', dialogs: [] }
const RECOGNIZED = { step: '03', title: '识别完成', dialogs: [] }

const stepLabels = upload => upload.page.root.byClass('workflow-steps').byTag('li').map(li => li.byTag('strong')[0].text)
const reviewTitle = upload => upload.page.root.byClass('progress-card')?.byClass('card-title').text ?? ''
const precedes = (upload, first, second) => {
  const order = [...upload.page.root.walk()]
  return order.indexOf(first) >= 0 && order.indexOf(first) < order.indexOf(second)
}

test('the upload flow has no answer-confirmation step or no-template branch', async (t) => {
  const upload = await gradingPage(t)
  assert.deepEqual(stepLabels(upload), ['答案依据', '学生试卷', '识别与确认', '批改审阅'])

  // Without a saved template nothing can start: there is no other way to grade.
  await upload.choosePapers([paperFile('a.png')])
  assert.equal(upload.fetches(RECOGNIZE).length, 0)
  assert.equal(upload.button('开始批阅').disabled, true)
  upload.page.setup.onStartRecognize()
  assert.ok(upload.client.messages.some(m => m.type === 'warning' && m.message === '请先识别并保存本次模板'))
  assert.deepEqual(upload.openDialogs(), [])

  // With it, recognition runs against that template and grading follows at
  // once: the card is still on recognition (03) and nothing asks to confirm answers.
  await saveTemplate(upload)
  assert.equal(upload.fetches(RECOGNIZE).length, 1)
  assert.equal(formFields(upload.fetches(RECOGNIZE)[0].init.body).template_id, 'tpl-1')
  assert.deepEqual(upload.atConfirm, [WHILE_RECOGNIZING])
  assert.equal(upload.confirmed().length, 1)

  // Graded: the card is back at the upload form, the review is shown.
  assert.equal(upload.gradeCard().dataset.step, '02')
  assert.ok(upload.button('选择文件'))
  assert.match(reviewTitle(upload), /^批改结果审阅/)
  assert.deepEqual(upload.openDialogs(), [])
  assert.deepEqual(stepLabels(upload), ['答案依据', '学生试卷', '识别与确认', '批改审阅'])
  assert.deepEqual(upload.page.errors, [])
})

test('recognition completion routes straight to grading', async (t) => {
  // can_grade: grading starts by itself, while the page is still at recognition.
  const complete = await gradingPage(t)
  await saveTemplate(complete)
  await complete.choosePapers([paperFile('a.png')])
  assert.equal(complete.confirmed().length, 1, 'no click is needed to grade')
  assert.deepEqual(complete.atConfirm, [WHILE_RECOGNIZING])
  assert.equal(complete.page.setup.summary.total, 1)
  complete.unmount()

  // Incomplete recognition stays on the recognition step and grades nothing.
  const incomplete = await gradingPage(t, {
    recognize: recognized(recognizedPaper(), { batch_id: 'b1', can_grade: false, uploaded_papers: 1, total_papers: 1, questions: QUESTIONS }),
  })
  await saveTemplate(incomplete)
  await incomplete.choosePapers([paperFile('a.png')])
  assert.equal(incomplete.confirmed().length, 0)
  assert.equal(incomplete.gradeCard().dataset.step, '03')
  assert.match(incomplete.gradeCard().byClass('card-title').text, /^识别未完成/)
  assert.equal(brief(incomplete.button('继续开始批阅')), null)
  assert.ok(incomplete.button('重新识别全部'))
  incomplete.unmount()

  // A complete batch without gradable questions fails instead of grading.
  const empty = await gradingPage(t, {
    recognize: recognized(recognizedPaper(), { ...COMPLETE, questions: [] }),
    responses: { [orientationRoute('b1')]: batchState({ questions: [] }) },
  })
  await saveTemplate(empty)
  await empty.choosePapers([paperFile('a.png')])
  assert.equal(empty.confirmed().length, 0)
  assert.ok(empty.alerts().includes('未识别到可用于批改的题目，请检查视觉模型服务、网络或试卷图片'), empty.alerts().join(' | '))
  assert.equal(empty.gradeCard().dataset.step, '03')
})

test('recovered and re-checked batches go straight to grading', async (t) => {
  // 恢复识别完成后直接批阅。
  const unfinished = batchState({ status: 'recognizing_remaining', can_grade: false, papers: [{ paper_id: 'p1', filename: 'a.png', ocr_status: 'queued' }] })
  const resumed = await gradingPage(t, {
    localStorage: { [ORIENTATION_POINTER]: 'b1' },
    responses: { [orientationRoute('b1')]: unfinished },
    streams: { [resumeRoute('b1')]: recognized() },
  })
  assert.equal(resumed.confirmed().length, 0)
  await resumed.click('继续处理未完成试卷')
  assert.equal(resumed.fetches(resumeRoute('b1')).length, 1)
  assert.equal(resumed.confirmed().length, 1)
  assert.deepEqual(resumed.atConfirm, [WHILE_RECOGNIZING])
  resumed.unmount()

  // A resume that brings back no questions is an error, not a grading run.
  const empty = await gradingPage(t, {
    localStorage: { [ORIENTATION_POINTER]: 'b1' },
    responses: { [orientationRoute('b1')]: unfinished },
    streams: { [resumeRoute('b1')]: recognized(recognizedPaper(), { ...COMPLETE, questions: [] }) },
  })
  await empty.click('继续处理未完成试卷')
  assert.equal(empty.confirmed().length, 0)
  assert.ok(empty.alerts().includes('恢复识别后未得到可批阅题目'), empty.alerts().join(' | '))
  empty.unmount()

  // 单卷方向确认成功后直接批阅。
  const rotated = { paper_id: 'p1', filename: 'a.png', student_name: '甲', identity: MATCHED, orientation_status: 'needs_review', orientation_reason: 'rotated' }
  const orientation = await gradingPage(t, {
    localStorage: { [ORIENTATION_POINTER]: 'b1' },
    responses: {
      [orientationRoute('b1')]: batchState({ status: 'orientation_review', can_grade: false, papers: [rotated] }),
      [PAPER_IMAGE]: () => ({ status: 200, data: paperFile('a.png') }),
      [ORIENTATION_CONFIRM]: batchState({ papers: [{ ...rotated, orientation_status: 'teacher_confirmed' }] }),
    },
  })
  assert.equal(orientation.confirmed().length, 0)
  await orientation.click('旋转并确认')
  const preview = orientation.page.root.find(el => el.tagName === 'img' && el.getAttribute('alt') === 'a.png方向预览')
  // The preview's object URL is what the page reads back to rotate.
  orientation.stream(`GET ${preview.getAttribute('src')}`, () => new Response(paperFile('a.png')))
  await orientation.click('顺时针 90°')
  await orientation.click('确认文字正立并重新识别此卷')
  const [sent] = orientation.requests(ORIENTATION_CONFIRM)
  assert.equal(sent.data.get('quarter_turns_clockwise'), '1')
  assert.equal(orientation.confirmed().length, 1)
  assert.deepEqual(orientation.atConfirm, [RECOGNIZED])
  orientation.unmount()

  // 方向复核批次恢复后直接批阅：a refresh with a gradable saved batch grades it.
  const restored = await gradingPage(t, {
    localStorage: { [ORIENTATION_POINTER]: 'b1' },
    responses: { [orientationRoute('b1')]: batchState() },
  })
  assert.equal(restored.requests(orientationRoute('b1')).length, 1)
  assert.equal(restored.confirmed().length, 1)
  assert.equal(restored.confirmed()[0].batch_id, 'b1')
  assert.deepEqual(restored.page.errors, [])
})

test('recognition exposes new-batch and continuation controls before the long paper list', async (t) => {
  // While recognizing, the papers are listed but neither control is offered.
  const live = liveSse(t)
  const running = await gradingPage(t, { recognize: live.respond })
  await saveTemplate(running)
  await running.choosePapers([paperFile('a.png')])
  live.send('paper', recognizedPaper())
  await running.settle()
  assert.ok(running.gradeCard().byClass('result-table'))
  assert.equal(brief(running.button('开始新批次')), null)
  assert.equal(brief(running.button('继续开始批阅')), null)
  running.unmount()

  // A gradable batch that waits for the teacher (an unconfirmed student).
  const upload = await gradingPage(t, {
    localStorage: { [ORIENTATION_POINTER]: 'b1' },
    responses: {
      [orientationRoute('b1')]: batchState({ papers: [UNMATCHED_PAPER] }),
      [IDENTITY_OPTIONS]: { students: [{ student_id: 5, name: '甲', student_no: '01' }] },
    },
  })
  await upload.click('暂不批改')
  const table = upload.gradeCard().byClass('result-table')
  const proceed = upload.button('继续开始批阅')
  const restart = upload.button('开始新批次')
  assert.ok(table && proceed && restart)
  assert.ok(precedes(upload, proceed, table), '继续开始批阅 comes before the paper list')
  assert.ok(precedes(upload, restart, table), '开始新批次 comes before the paper list')

  // Continuing goes through the normal grading entry (its identity gate).
  await upload.click('继续开始批阅')
  assert.deepEqual(upload.openDialogs(), ['确认学生身份'])
  assert.equal(upload.confirmed().length, 0)
  await upload.click('暂不批改')

  // 开始新批次 returns to the upload form and forgets the batch pointer.
  await upload.click('开始新批次')
  assert.equal(upload.gradeCard().dataset.step, '02')
  assert.ok(upload.button('选择文件'))
  assert.equal(brief(upload.gradeCard().byClass('result-table')), null)
  assert.equal(upload.client.localStorage.getItem(ORIENTATION_POINTER), null)
  assert.equal(upload.confirmed().length, 0)
})

test('template grading submits no client answers', async (t) => {
  const upload = await gradingPage(t)
  await upload.select(upload.page.root.byClass('batch-setup').find(el => el.tagName === 'select'), 'exam')
  await upload.type(upload.page.root.find(el => el.tagName === 'input' && el.getAttribute('placeholder') === '如：高一月考'), '期中考试')
  await saveTemplate(upload)
  await upload.choosePapers([paperFile('a.png')])
  // 冻结答案由服务端从模板读取，前端不回传答案。
  assert.deepEqual(upload.confirmed(), [{
    batch_id: 'b1',
    mode: 'precise',
    category: 'exam',
    exam_name: '期中考试',
    identity_resolutions: [],
  }])
})

test('identity confirmation is the only dialog a template batch can still stop on', async (t) => {
  const upload = await gradingPage(t, {
    localStorage: { [ORIENTATION_POINTER]: 'b1' },
    responses: {
      [orientationRoute('b1')]: batchState({ papers: [UNMATCHED_PAPER] }),
      [IDENTITY_OPTIONS]: { students: [{ student_id: 5, name: '甲', student_no: '01' }] },
    },
  })
  // 身份闸门只打开身份弹窗，不切换步骤，也不批阅。
  assert.deepEqual(upload.openDialogs(), ['确认学生身份'])
  assert.equal(upload.requests(IDENTITY_OPTIONS).length, 1)
  assert.equal(upload.gradeCard().dataset.step, '03')
  assert.match(upload.gradeCard().byClass('card-title').text, /^识别完成/)
  assert.equal(upload.confirmed().length, 0)

  // Asking again reopens it with the identities already prepared.
  await upload.click('暂不批改')
  await upload.click('继续开始批阅')
  assert.deepEqual(upload.openDialogs(), ['确认学生身份'])
  assert.equal(upload.requests(IDENTITY_OPTIONS).length, 1)

  // 身份确认后走同一条批阅入口（跳过重复的身份闸门）。
  const dialog = upload.dialog('确认学生身份')
  assert.equal(upload.button('确认身份并开始批改').disabled, true)
  await upload.select(dialog.find(el => el.tagName === 'select'), 5)
  await upload.click('确认身份并开始批改')
  assert.deepEqual(upload.confirmed(), [{
    batch_id: 'b1',
    mode: 'precise',
    category: 'daily',
    exam_name: '',
    identity_resolutions: [{ paper_id: 'p1', action: 'existing', student_id: 5, confirmed_name: '', confirmed_student_no: '' }],
  }])
  assert.deepEqual(upload.atConfirm, [RECOGNIZED])
  assert.deepEqual(upload.openDialogs(), [])
  assert.match(reviewTitle(upload), /^批改结果审阅/)
})

test('mode selection is independent of the template authority path', async (t) => {
  // 简单与精准模式共用同一批阅提交。
  for (const [mode, label] of [['simple', '简单模式'], ['precise', '精准模式']]) {
    const upload = await gradingPage(t)
    await upload.select(upload.page.root.find(el => el.tagName === 'select' && el.id === 'batch-grade-mode'), mode)
    await saveTemplate(upload)
    await upload.choosePapers([paperFile('a.png')])
    assert.equal(formFields(upload.fetches(RECOGNIZE)[0].init.body).mode, mode)
    assert.equal(upload.confirmed().length, 1, mode)
    assert.equal(upload.confirmed()[0].mode, mode)
    assert.deepEqual(upload.atConfirm, [WHILE_RECOGNIZING], mode)
    assert.equal(upload.gradeCard().dataset.step, '02', mode)
    assert.equal(upload.page.root.byClass('progress-card').byClass('mode-pill').text, label)
    assert.match(reviewTitle(upload), /^批改结果审阅/)
    upload.unmount()
  }
})
