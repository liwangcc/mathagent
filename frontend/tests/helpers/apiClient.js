// Drive frontend/src/api/index.js against an in-memory HTTP backend.
//
//   const client = await loadApiClient()
//   client.respond(config => ({ status: 409, data: { detail: '…' } }))
//   await client.api.batch.excludePaper('b1', 'p1')
//   client.requests[0]   // the axios request config the client sent
//
// Every request goes through the client's real axios instance and
// interceptors; only the network adapter is replaced. Each loadApiClient()
// call installs fresh storage and location globals and clears `messages`,
// the ElMessage toasts the client has shown (see helpers/elementPlus.js).
// `env` sets the client's `import.meta.env` (e.g. VITE_API_ORIGIN, as in the
// desktop build); each distinct env gets its own client module instance.
import './srcModules.js'
import axios from 'axios'
import { installBrowserGlobals } from './browser.js'
import { messages } from './elementPlus.js'

const requests = []
const ok = () => ({ status: 200, data: {} })
let handler = ok

// Set before the client module calls axios.create(), which copies defaults.
axios.defaults.adapter = async (config) => {
  requests.push(config)
  const { status = 200, data = {}, headers = {} } = (await handler(config)) ?? {}
  const response = { data, status, statusText: String(status), headers, config, request: {} }
  const accepted = config.validateStatus ? config.validateStatus(status) : status < 400
  if (accepted) return response
  throw new axios.AxiosError(
    `Request failed with status code ${status}`,
    axios.AxiosError.ERR_BAD_RESPONSE,
    config,
    {},
    response,
  )
}

const clientModules = new Map()

export async function loadApiClient({ env = {}, ...globals } = {}) {
  const browser = installBrowserGlobals(globals)
  const key = JSON.stringify(env)
  if (!clientModules.has(key)) {
    const query = Object.keys(env).length ? `?env=${encodeURIComponent(key)}` : ''
    clientModules.set(key, import(`../../src/api/index.js${query}`))
  }
  const module = await clientModules.get(key)
  requests.length = 0
  messages.length = 0
  handler = ok
  return {
    ...browser,
    api: module.default,
    module,
    requests,
    messages,
    respond(next) { handler = next },
  }
}
