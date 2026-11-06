// element-plus as frontend/src modules see it under helpers/srcModules.js: the
// real library, except ElMessage and ElMessageBox, which need a DOM and are
// recorded instead.
export * from 'element-plus'

export const messages = []

const show = type => (options) => {
  messages.push({ type, message: typeof options === 'string' ? options : options?.message })
  return { close() {} }
}

export const ElMessage = Object.assign(show('info'), {
  success: show('success'),
  warning: show('warning'),
  info: show('info'),
  error: show('error'),
  closeAll() {},
})

// ElMessageBox.confirm / alert / prompt (and ElMessageBox(options)) record
// each box in `boxes` and answer with what answerMessageBoxes() returns for
// it: 'confirm' (the default), 'cancel' or 'close' (the promise rejects with
// that action, as the real one does), or { value } for a prompt.
export const boxes = []
let answer = () => 'confirm'

export function answerMessageBoxes(next) {
  answer = next
}

const box = kind => async (message, title, options = {}) => {
  const entry = { kind, message, title, options }
  boxes.push(entry)
  const result = await answer(entry)
  if (result === 'cancel' || result === 'close') throw result
  return kind === 'prompt' ? { value: result?.value ?? '', action: 'confirm' } : 'confirm'
}

export const ElMessageBox = Object.assign(
  options => box('box')(options?.message, options?.title, options),
  { confirm: box('confirm'), alert: box('alert'), prompt: box('prompt'), close() {} },
)
