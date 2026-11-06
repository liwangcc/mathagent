import assert from 'node:assert/strict'
import test from 'node:test'
import {
  ACCELERATOR_UNAVAILABLE,
  AVAILABILITY,
  RECOGNITION_ACTIONS,
  TEMPLATE_AVAILABILITY,
  clickButton,
  clickTemplateImport,
  dropPapers,
  errorMessages,
  exactButton,
  isDisabled,
  networkFailure,
  pickersOpenedBy,
  recognitionActions,
  textOf,
} from './helpers/availabilityQuota.js'
import { mountUpload, paperFile } from './helpers/uploadPage.js'

// The student registration accelerator is an extra hardware check that the
// server can mark as advisory (registration_hardware.advisory === true). The
// real Upload page answers its two readiness probes and the teacher sees which
// recognition actions it allows: core and template model readiness stay hard
// gates whatever the accelerator reports.

// The probes' answers: the core chain (`status`), the template model
// (`templateStatus`) and the registration accelerator, advisory unless
// `hardware` says otherwise ({} leaves the flag out).
function readiness({ status = 'available', templateStatus = 'available', hardwareStatus = 'waiting_accelerator', hardware = { advisory: true } } = {}) {
  return {
    [AVAILABILITY]: { status, message: 'synthetic core readiness' },
    [TEMPLATE_AVAILABILITY]: {
      status: templateStatus,
      service_status: templateStatus,
      message: 'synthetic model readiness',
      registration_hardware: { status: hardwareStatus, message: 'synthetic registration advisory', ...hardware },
    },
  }
}

const every = disabled => Object.fromEntries(RECOGNITION_ACTIONS.map(label => [label, disabled]))
const ALL_ENABLED = every(false)
const ALL_DISABLED = every(true)

const uploadArea = upload => upload.page.root.byClass('upload-area')
const serviceLabel = upload => upload.page.root.byClass('service-state').text
// The 已选 N 个文件 line under the student upload area.
const papersChosen = upload => /已选 \d+ 个文件/.test(upload.page.root.byClass('grade-card').text)

test('explicit registration advisory never disables ready student upload, recognition or retry actions', async (t) => {
  for (const hardwareStatus of ['waiting_accelerator', 'unavailable', 'fault']) {
    const upload = await mountUpload(t, { responses: readiness({ hardwareStatus }) })
    // The page keeps the accelerator state, marked as not blocking.
    assert.equal(upload.page.setup.registrationHardwareState.advisory, true)
    assert.equal(upload.page.setup.registrationHardwareState.blocksNewWork, false)
    assert.deepEqual(await recognitionActions(upload), ALL_ENABLED, hardwareStatus)
    assert.equal(uploadArea(upload).getAttribute('aria-disabled'), null)
    assert.deepEqual(await pickersOpenedBy(upload, () => clickButton(upload, '选择文件')), ['papers'], hardwareStatus)
    assert.deepEqual(await pickersOpenedBy(upload, () => clickTemplateImport(upload)), ['template'], hardwareStatus)
    // Dropped papers are imported (a closed gate refuses the same drop).
    await dropPapers(upload)
    assert.equal(papersChosen(upload), true, hardwareStatus)
    assert.equal(textOf(upload.page.root.byClass('template-service-alert')), null)
    assert.deepEqual(errorMessages(upload), [])
    upload.unmount()
  }
})

test('only literal boolean advisory true relaxes registration, never missing false or string values', async (t) => {
  for (const advisory of [undefined, false, 'true', 1, null]) {
    const label = `advisory ${String(advisory)}`
    const upload = await mountUpload(t, { responses: readiness({ hardware: advisory === undefined ? {} : { advisory } }) })
    // The core chain is ready: only the accelerator holds student work back.
    assert.equal(serviceLabel(upload), '可用', label)
    assert.deepEqual(await recognitionActions(upload), ALL_DISABLED, label)
    assert.equal(uploadArea(upload).getAttribute('aria-disabled'), 'true', label)
    await dropPapers(upload)
    assert.deepEqual(errorMessages(upload), [ACCELERATOR_UNAVAILABLE], label)
    assert.equal(papersChosen(upload), false, label)
    upload.unmount()
  }
})

test('registration advisory cannot bypass unavailable faulty malformed or still-checking core readiness', async (t) => {
  const malformed = '批改链路状态响应异常，请稍后重试或联系管理员。'
  for (const [status, message] of [
    ['unavailable', 'synthetic core readiness'],
    ['fault', 'synthetic core readiness'],
    ['checking', malformed],
    ['unexpected', malformed],
  ]) {
    const upload = await mountUpload(t, { responses: readiness({ status }) })
    assert.deepEqual(await recognitionActions(upload), ALL_DISABLED, status)
    await dropPapers(upload)
    assert.equal(papersChosen(upload), false, status)
    // Template import follows the core chain too.
    assert.deepEqual(await pickersOpenedBy(upload, () => clickTemplateImport(upload)), [], status)
    assert.deepEqual(errorMessages(upload), [message, message], status)
    upload.unmount()
  }
})

test('actual template model failure stays blocked while advisory hardware does not fake model readiness', async (t) => {
  for (const templateStatus of ['unavailable', 'fault']) {
    const upload = await mountUpload(t, { responses: readiness({ templateStatus }) })
    const alert = upload.page.root.byClass('template-service-alert')
    assert.ok(alert.classList.contains(`service-alert-${templateStatus}`), alert.className)
    assert.equal(alert.getAttribute('role'), 'alert')
    assert.match(alert.text, /synthetic model readiness/)
    assert.equal(upload.page.root.byClass('template-upload').getAttribute('aria-disabled'), 'true')
    // Template pages are chosen: only the model gate keeps 识别模板 disabled.
    upload.page.setup.templateFiles = [paperFile('template.png')]
    await upload.settle()
    assert.equal(isDisabled(exactButton(upload, '识别模板')), true, templateStatus)
    assert.deepEqual(await pickersOpenedBy(upload, () => clickTemplateImport(upload)), [], templateStatus)
    assert.deepEqual(errorMessages(upload), ['synthetic model readiness'])
    upload.unmount()
  }
})

test('only an explicit server blocksNewWork=true without advisory blocks new work', async (t) => {
  const upload = await mountUpload(t, { responses: readiness() })
  for (const [flags, blocked] of [
    // The server's literal advisory bypass wins over every other flag.
    [{ advisory: true, blocksNewWork: true }, false],
    // A blocked registration needs the explicit server instruction...
    [{ advisory: false, blocksNewWork: true }, true],
    // ...and an unstated flag never blocks on its own.
    [{ advisory: false }, false],
    [{ advisory: false, blocksNewWork: false }, false],
  ]) {
    upload.page.setup.registrationHardwareState = { status: 'unavailable', label: '', message: '', ...flags }
    await upload.settle()
    assert.deepEqual(await recognitionActions(upload), every(blocked), JSON.stringify(flags))
    assert.equal(uploadArea(upload).getAttribute('aria-disabled'), blocked ? 'true' : null, JSON.stringify(flags))
  }
})

test('new readiness polls replace advisory state and never retain an old bypass', async (t) => {
  const upload = await mountUpload(t, { timers: true, responses: readiness() })
  assert.deepEqual(await pickersOpenedBy(upload, () => clickButton(upload, '选择文件')), ['papers'])
  upload.answer(TEMPLATE_AVAILABILITY, readiness({ hardware: {} })[TEMPLATE_AVAILABILITY])
  await upload.tick(15000)
  assert.deepEqual(await recognitionActions(upload), ALL_DISABLED)
  await dropPapers(upload)
  assert.deepEqual(errorMessages(upload), [ACCELERATOR_UNAVAILABLE])
})

test('availability transport failure remains fail closed after an advisory response', async (t) => {
  for (const [route, message] of [
    [AVAILABILITY, '无法获取批改链路状态，请检查网络或联系管理员。'],
    // Without the template probe the accelerator state is unknown.
    [TEMPLATE_AVAILABILITY, ACCELERATOR_UNAVAILABLE],
  ]) {
    const upload = await mountUpload(t, { timers: true, responses: readiness() })
    assert.deepEqual(await recognitionActions(upload), ALL_ENABLED, route)
    upload.answer(route, networkFailure)
    await upload.tick(15000)
    assert.deepEqual(await recognitionActions(upload), ALL_DISABLED, route)
    await dropPapers(upload)
    assert.deepEqual(errorMessages(upload), [message], route)
    upload.unmount()
  }
})

test('queued core service remains usable and a recent real fault remains held despite advisory hardware', async (t) => {
  const queued = await mountUpload(t, { timers: true, responses: readiness({ status: 'queued' }) })
  assert.equal(serviceLabel(queued), '需排队')
  assert.deepEqual(await recognitionActions(queued), ALL_ENABLED)
  assert.deepEqual(await pickersOpenedBy(queued, () => clickButton(queued, '选择文件')), ['papers'])
  queued.unmount()

  const fault = await mountUpload(t, { timers: true, responses: readiness({ status: 'fault' }) })
  fault.answer(AVAILABILITY, readiness()[AVAILABILITY])
  await fault.tick(15000)
  assert.equal(fault.requests(AVAILABILITY).length, 2, 'the page polled again and heard available')
  assert.equal(serviceLabel(fault), '链路故障')
  assert.ok(fault.page.root.byClass('service-state').classList.contains('service-state-fault'))
  assert.deepEqual(await recognitionActions(fault), ALL_DISABLED)
  await dropPapers(fault)
  assert.deepEqual(errorMessages(fault), ['synthetic core readiness'])
})
