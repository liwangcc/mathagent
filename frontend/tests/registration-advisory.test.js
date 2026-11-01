import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref, computed } from 'vue'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const declarations = source.slice(source.indexOf('const serviceState = ref('), source.indexOf('function markServiceFault('))
const names = ['markServiceFault', 'refreshAvailability', 'requireBatchService', 'requireRecognitionService', 'requireTemplateService']
const functions = names.map((name) => {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`))
  assert.ok(match, name)
  return match[0]
}).join('\n')
const actions = ['triggerFileUpload', 'triggerFolderUpload', 'onStartRecognize', 'runRecognize', 'confirmOrientationAndRetry', 'onBatchConfirm']
const buttonExpressions = [...source.matchAll(/<button\b[^]*?>/g)].flatMap(([tag]) => {
  const action = tag.match(/@click(?:\.stop)?="([^"]+)"/)?.[1]
  const expression = tag.match(/:disabled="([^"]+)"/)?.[1]
  return actions.includes(action) && expression ? [{ action, expression }] : []
})
assert.equal(buttonExpressions.length, actions.length, 'Read the six actual upload/recognition button gates')

function harness({ status = 'available', templateStatus = 'available', advisory = true, hardwareStatus = 'waiting_accelerator' } = {}) {
  const responses = {
    service: { status, message: 'synthetic core readiness' },
    template: { status: templateStatus, service_status: templateStatus, message: 'synthetic model readiness',
      registration_hardware: { status: hardwareStatus, message: 'synthetic registration advisory', advisory } },
  }
  const messages = []
  const api = { grade: {
    availability: async () => { if (responses.service instanceof Error) throw responses.service; return responses.service },
    templateAvailability: async () => { if (responses.template instanceof Error) throw responses.template; return responses.template },
  } }
  const methods = new Function('ref', 'computed', 'api', 'ElMessage', `${declarations}\n${functions}\nreturn {
    ${names.join(', ')}, serviceState, templateServiceState, registrationHardwareState,
    serviceBlocksNewWork, templateServiceBlocksNewWork, registrationHardwareBlocksNewWork, recognitionBlocksNewWork
  }`)(ref, computed, api, { error: (text) => messages.push(text) })
  return { ...methods, responses, messages, buttonDisabled(action) {
    const values = { recognitionBlocksNewWork: methods.recognitionBlocksNewWork.value,
      recognizing: false, imagePreprocessing: false, imageExtracting: false,
      fileList: [{}], confirmedTemplateId: 'synthetic-template', orientationSaving: false,
      orientationImageUrl: 'blob:synthetic-image', batchConfirmValid: true }
    const item = buttonExpressions.find((button) => button.action === action)
    return new Function(...Object.keys(values), `return Boolean(${item.expression})`)(...Object.values(values))
  } }
}

test('explicit registration advisory never disables ready student upload, recognition or retry actions', async () => {
  for (const hardwareStatus of ['waiting_accelerator', 'unavailable', 'fault']) {
    const h = harness({ hardwareStatus })
    await h.refreshAvailability()
    assert.equal(h.registrationHardwareBlocksNewWork.value, false, hardwareStatus)
    assert.equal(h.registrationHardwareState.value.advisory, true)
    assert.equal(h.registrationHardwareState.value.blocksNewWork, false)
    assert.equal(h.requireRecognitionService(), true)
    for (const action of actions) assert.equal(h.buttonDisabled(action), false, `${hardwareStatus}: ${action}`)
    assert.equal(h.requireTemplateService(), true)
    assert.deepEqual(h.messages, [])
  }
})

test('only literal boolean advisory true relaxes registration, never missing false or string values', async () => {
  for (const advisory of [undefined, false, 'true', 1, null]) {
    const h = harness({ advisory })
    if (advisory === undefined) delete h.responses.template.registration_hardware.advisory
    await h.refreshAvailability()
    assert.equal(h.registrationHardwareBlocksNewWork.value, true)
    assert.equal(h.requireRecognitionService(), false)
    for (const action of actions) assert.equal(h.buttonDisabled(action), true, action)
  }
})

test('registration advisory cannot bypass unavailable faulty malformed or still-checking core readiness', async () => {
  for (const status of ['unavailable', 'fault', 'checking', 'unexpected']) {
    const h = harness({ status })
    await h.refreshAvailability()
    assert.equal(h.requireRecognitionService(), false)
    assert.equal(h.requireTemplateService(), false)
    for (const action of actions) assert.equal(h.buttonDisabled(action), true, `${status}: ${action}`)
  }
})

test('actual template model failure stays blocked while advisory hardware does not fake model readiness', async () => {
  for (const templateStatus of ['unavailable', 'fault']) {
    const h = harness({ templateStatus })
    await h.refreshAvailability()
    assert.equal(h.templateServiceBlocksNewWork.value, true)
    assert.equal(h.requireTemplateService(), false)
    assert.equal(h.templateServiceState.value.status, templateStatus)
  }
})

test('only an explicit server blocksNewWork=true without advisory blocks new work', async () => {
  const h = harness()
  // The server's literal advisory bypass wins over every other flag.
  h.registrationHardwareState.value = { status: 'unavailable', label: '', message: '', advisory: true, blocksNewWork: true }
  assert.equal(h.registrationHardwareBlocksNewWork.value, false)
  // A blocked registration needs the explicit server instruction...
  h.registrationHardwareState.value = { status: 'unavailable', label: '', message: '', advisory: false, blocksNewWork: true }
  assert.equal(h.registrationHardwareBlocksNewWork.value, true)
  // ...and an unstated flag never blocks on its own.
  h.registrationHardwareState.value = { status: 'unavailable', label: '', message: '', advisory: false }
  assert.equal(h.registrationHardwareBlocksNewWork.value, false)
  h.registrationHardwareState.value = { status: 'unavailable', label: '', message: '', advisory: false, blocksNewWork: false }
  assert.equal(h.registrationHardwareBlocksNewWork.value, false)
})

test('new readiness polls replace advisory state and never retain an old bypass', async () => {
  const h = harness()
  await h.refreshAvailability()
  assert.equal(h.requireRecognitionService(), true)
  delete h.responses.template.registration_hardware.advisory
  await h.refreshAvailability()
  assert.equal(h.requireRecognitionService(), false)
  assert.equal(h.registrationHardwareBlocksNewWork.value, true)
})

test('availability transport failure remains fail closed after an advisory response', async () => {
  for (const key of ['service', 'template']) {
    const h = harness()
    await h.refreshAvailability()
    assert.equal(h.requireRecognitionService(), true)
    h.responses[key] = new Error('synthetic transport failure')
    await h.refreshAvailability()
    assert.equal(h.requireRecognitionService(), false)
    for (const action of actions) assert.equal(h.buttonDisabled(action), true)
  }
})

test('queued core service remains usable and a recent real fault remains held despite advisory hardware', async () => {
  const queued = harness({ status: 'queued' })
  await queued.refreshAvailability()
  assert.equal(queued.requireRecognitionService(), true)
  const fault = harness({ status: 'fault' })
  await fault.refreshAvailability()
  fault.responses.service.status = 'available'
  await fault.refreshAvailability()
  assert.equal(fault.requireRecognitionService(), false)
  assert.equal(fault.serviceState.value.status, 'fault')
})
