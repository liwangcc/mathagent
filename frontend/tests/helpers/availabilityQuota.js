// What the real 批改 page (helpers/uploadPage.js mounts it) lets a teacher do
// while its readiness probes report a given state: the student recognition
// actions it enables or disables, the file pickers it opens, the papers it
// accepts from a drop, and the errors it shows.
//
//   import { AVAILABILITY, TEMPLATE_AVAILABILITY, recognitionActions, ... } from './helpers/availabilityQuota.js'
//   const upload = await mountUpload(t, { responses: { [AVAILABILITY]: { status: 'queued' } } })
//   await recognitionActions(upload)   // { 选择文件: false, ..., 重新识别全部: false } (true = disabled)
//   await pickersOpenedBy(upload, () => clickButton(upload, '选择文件'))   // ['papers']: it opened that picker
//   await dropPapers(upload)            // drag a student page onto the upload area
//   errorMessages(upload)               // the ElMessage.error texts shown so far
//
// Routes match helpers/uploadPage.js ('METHOD /api/path').
import axios from 'axios'
import { paperFile } from './uploadPage.js'

export const AVAILABILITY = 'GET /api/batch-grade/availability'
export const TEMPLATE_AVAILABILITY = 'GET /api/batch-grade/template/availability'
export const ACCELERATOR_UNAVAILABLE = '加速节点不可用，请使用客户端'

// A probe answer that fails the way a browser request does when the server
// cannot be reached (axios' network error, no response).
export function networkFailure(config) {
  throw new axios.AxiosError('Network Error', axios.AxiosError.ERR_NETWORK, config)
}

export const isDisabled = el => el.disabled === true || el.hasAttribute('disabled')

// The one <button> whose whole text is `text` ('选择文件' is not '选择文件夹').
export function exactButton(upload, text) {
  const matches = upload.page.root.findAll(el => el.tagName === 'button' && el.text === text)
  if (matches.length !== 1) throw new Error(`expected one button "${text}", found ${matches.length}`)
  return matches[0]
}

// Click a button as a user would: a disabled button does nothing.
export async function clickButton(upload, text) {
  const button = exactButton(upload, text)
  if (isDisabled(button)) throw new Error(`button "${text}" is disabled`)
  button.dispatch('click').catch(err => upload.page.errors.push({ err, info: 'click handler' }))
  await upload.settle()
}

// The six buttons that start or continue student recognition, each shown in
// the state where nothing but the readiness gates could hold it back: papers
// chosen, the template saved, nothing being prepared or recognized, the batch
// form valid, an original image loaded for the orientation check. Auto start
// is disarmed (as after the teacher's own start) so that the ready page waits
// for a click. Returns { label: disabled } and puts the page back as it was.
export const RECOGNITION_ACTIONS = ['选择文件', '选择文件夹', '开始批阅', '开始识别', '确认文字正立并重新识别此卷', '重新识别全部']

export async function recognitionActions(upload) {
  const { setup } = upload.page
  const { recognitionAutoArmed: armed, activeStep: step, fileList: files, confirmedTemplateId: template } = setup
  const disabled = {}
  const read = (...labels) => { for (const label of labels) disabled[label] = isDisabled(exactButton(upload, label)) }

  setup.recognitionAutoArmed = false
  setup.activeStep = 0
  setup.fileList = [paperFile('ready.png')]
  setup.confirmedTemplateId = 'synthetic-template'
  await upload.settle()
  read('选择文件', '选择文件夹', '开始批阅')

  setup.batchDialogVisible = true // what 开始批阅 opens
  await upload.settle()
  read('开始识别')
  setup.batchDialogVisible = false

  setup.orientationImageUrl = 'blob:synthetic-image'
  setup.orientationDialogVisible = true // what 旋转并确认 opens
  await upload.settle()
  read('确认文字正立并重新识别此卷')
  setup.orientationDialogVisible = false
  await upload.settle()

  setup.activeStep = 1 // an unfinished recognition run
  await upload.settle()
  read('重新识别全部')

  setup.activeStep = step
  setup.fileList = files
  setup.confirmedTemplateId = template
  setup.recognitionAutoArmed = armed
  await upload.settle()
  return disabled
}

// The page's hidden file inputs: the template's, the student files' and the
// student folder's.
export function filePickers(upload) {
  const { root } = upload.page
  const inputs = root.findAll(el => el.tagName === 'input' && el.type === 'file')
  const card = root.byClass('grade-card')
  return {
    template: inputs.find(el => !card.contains(el)) ?? null,
    papers: inputs.find(el => card.contains(el) && !el.hasAttribute('webkitdirectory')) ?? null,
    folder: inputs.find(el => card.contains(el) && el.hasAttribute('webkitdirectory')) ?? null,
  }
}

// Which pickers the page opens (it clicks the hidden input) while `act` runs.
// The inputs are looked up first: re-rendering a step replaces them.
export async function pickersOpenedBy(upload, act) {
  const opened = []
  for (const [name, input] of Object.entries(filePickers(upload))) {
    input?.addEventListener('click', () => opened.push(name))
  }
  await act()
  return opened
}

// Click the template import area (a <div>, not a button), as a user would.
export async function clickTemplateImport(upload) {
  const area = upload.page.root.byClass('template-upload')
  if (!area) throw new Error('no template import area on the page')
  area.dispatch('click').catch(err => upload.page.errors.push({ err, info: 'click handler' }))
  await upload.settle()
}

// Drag student pages onto the upload area and drop them.
export async function dropPapers(upload, files = [paperFile('dropped.png')]) {
  const area = upload.page.root.byClass('upload-area')
  area.dispatch('drop', { dataTransfer: { files, items: [] } })
    .catch(err => upload.page.errors.push({ err, info: 'drop handler' }))
  await upload.settle()
}

// An element's text, or null when the page does not render it. Assert on
// this, not on the element: a failing assertion prints its value, and an
// element prints the whole page through its parent links.
export const textOf = el => (el ? el.text : null)

export const errorMessages = upload => upload.client.messages
  .filter(message => message.type === 'error')
  .map(message => message.message)
