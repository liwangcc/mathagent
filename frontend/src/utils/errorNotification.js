import { ElMessage } from 'element-plus'
import { createErrorNotifier } from './processingError.js'

let storage = null
try { storage = globalThis.sessionStorage } catch { /* storage is optional */ }
export const notifyProcessingError = createErrorNotifier(message => ElMessage.error(message), storage)
