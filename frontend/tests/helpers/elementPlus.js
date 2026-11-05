// element-plus as frontend/src modules see it under helpers/srcModules.js: the
// real library, except ElMessage, which needs a DOM and is recorded instead.
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
