import { reactive } from 'vue'

export function formatDesktopBindingError(error) {
  const message = String(error?.message || error || '')
  if (message.includes('teacher login expired') || message.includes('teacher login token is invalid') || message.includes('session_changed')) return '登录状态已变化，请重新登录后管理本机绑定。'
  if (message.includes('already in progress')) return '另一个本机绑定操作正在进行，请稍候重试。'
  if (message.includes('invalid response') || message.includes('response was invalid')) return '设备绑定服务返回的数据不完整，请稍后重试。'
  if (message.includes('registration server is unavailable')) return '暂时无法连接设备绑定服务，请检查网络后重试。'
  if (message.includes('already registered')) return '设备登记未能恢复，请确认已安装最新版客户端后重试；无需先解除绑定。'
  if (message.includes('does not match')) return '本机登记与当前服务不一致，已停止操作，请联系管理员检查；不要反复解绑。'
  if (message.includes('Windows') || message.includes('stored desktop credential') || message.includes('credential storage')) return 'Windows 本机授权读取或保存失败，请稍后重试。'
  if (message.includes('sidecar') || message.includes('worker')) return '本机处理组件未正常启动，请重启客户端后重试。'
  return '本机绑定操作未完成，请稍后重试。'
}

export function desktopProcessorReady(binding, worker) {
  return binding?.configured === true
    && worker?.handoff?.configured === true
    && worker?.handoff?.running === true
    && worker?.handoff?.connected === true
    && worker?.handoff?.lastErrorCode !== 'node_auth_failed'
    && worker?.localProcessingAvailable === true
    && worker?.paused !== true
}

// One manager and one dialog own all binding writes. Dependencies are injected
// so account-switch, connection and concurrency paths can run without real IPC.
export function createDesktopBindingManager({ invoke, getSession, wait = ms => new Promise(resolve => setTimeout(resolve, ms)) }) {
  const state = reactive({
    visible: false, busy: false, source: '', binding: null, worker: null,
    error: '', verified: false, attempted: false,
  })
  let pending = null
  let resolvePending = null
  let generation = 0
  let verifiedSession = null

  const session = () => {
    const current = getSession()
    return { accountId: current?.accountId, token: current?.token }
  }
  const sameSession = (left, right) => Boolean(left?.token && left.accountId != null
    && left.accountId === right?.accountId && left.token === right?.token)
  const currentSessionVerified = () => state.verified && sameSession(verifiedSession, session())

  function open(source = '') {
    if (pending) return pending
    state.source = source
    state.visible = true
    state.error = ''
    state.attempted = false
    state.verified = false
    verifiedSession = null
    pending = new Promise(resolve => { resolvePending = resolve })
    return pending
  }

  function close(force = false) {
    if (state.busy && !force) return false
    const outcome = {
      binding: state.binding, worker: state.worker, attempted: state.attempted,
      verified: !force && currentSessionVerified(), error: state.error,
    }
    state.visible = false
    const resolve = resolvePending
    pending = null
    resolvePending = null
    resolve?.(outcome)
    return true
  }

  function invalidateSession() {
    generation += 1
    state.verified = false
    verifiedSession = null
    state.binding = null
    state.worker = null
    state.error = ''
    close(true)
  }

  async function perform(action) {
    if (state.busy || !state.visible) return false
    const expected = session()
    if (!expected.token || expected.accountId == null) {
      state.error = '登录已过期，请重新登录后绑定本机。'
      return false
    }
    const operationGeneration = generation
    const checkSession = () => {
      if (generation !== operationGeneration || !sameSession(expected, session())) throw new Error('session_changed')
    }
    state.busy = true
    state.error = ''
    try {
      await action(expected, checkSession)
      checkSession()
      return true
    } catch (error) {
      if (generation === operationGeneration && sameSession(expected, session())) {
        state.error = formatDesktopBindingError(error)
      }
      return false
    } finally {
      state.busy = false
    }
  }

  async function refresh() {
    return perform(async (_, checkSession) => {
      state.worker = null
      const binding = await invoke('desktop_binding_status')
      checkSession()
      if (binding?.nodeId !== state.binding?.nodeId || !binding?.configured) {
        state.verified = false
        verifiedSession = null
      }
      state.binding = binding
      const worker = await invoke('get_worker_status')
      checkSession()
      state.worker = worker
    })
  }

  async function bind() {
    return perform(async (expected, checkSession) => {
      state.attempted = true
      state.verified = false
      state.worker = null
      verifiedSession = null
      // Even a configured credential must be validated for the current teacher.
      // Native repair supports retrying; never automatically unbind on a conflict.
      const binding = await invoke('bind_teacher_desktop', { bearerToken: expected.token })
      checkSession()
      state.binding = binding
      state.verified = binding?.configured === true
      verifiedSession = state.verified ? expected : null
      let worker = await invoke('get_worker_status')
      checkSession()
      state.worker = worker
      if (worker?.paused === true) {
        worker = await invoke('resume_worker')
        checkSession()
        state.worker = worker
      }
      for (let attempt = 0; attempt < 6; attempt += 1) {
        if (desktopProcessorReady(state.binding, state.worker)) break
        if (attempt > 0) {
          await wait(750)
          checkSession()
        }
        worker = await invoke('get_worker_status')
        checkSession()
        state.worker = worker
      }
    })
  }

  async function unbind() {
    return perform(async (expected, checkSession) => {
      const binding = await invoke('unbind_teacher_desktop', { bearerToken: expected.token })
      checkSession()
      state.binding = binding
      state.worker = null
      state.verified = false
      verifiedSession = null
    })
  }

  return { state, open, close, refresh, bind, unbind, invalidateSession, currentSessionVerified }
}

let sharedManager

export function useDesktopBindingDialog(auth) {
  if (!sharedManager) {
    sharedManager = createDesktopBindingManager({
      getSession: () => ({ accountId: auth.user?.id ?? auth.user?.username, token: auth.token }),
      invoke: (command, args = {}) => {
        const invoke = window.__TAURI_INTERNALS__?.invoke
        if (typeof invoke !== 'function') throw new Error('desktop_runtime_unavailable')
        return invoke(command, args)
      },
    })
  }
  return sharedManager
}
