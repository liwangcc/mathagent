// The `window` members pages touch at runtime, as a plain object installed as
// globalThis.window (together with `confirm` and `alert`): location and
// storage (helpers/browser.js installs those), timers, event listeners
// (beforeunload, resize, ...), confirm/alert/open. Listeners, confirm
// questions, alerts and opened windows are recorded; `confirm` answers with
// the given function.
export function installMemoryWindow({ confirm = () => true } = {}) {
  const listeners = new Map()
  const opened = []
  const confirmations = []
  const alerts = []
  const win = {
    get location() { return globalThis.location },
    get localStorage() { return globalThis.localStorage },
    get sessionStorage() { return globalThis.sessionStorage },
    get document() { return globalThis.document },
    get innerWidth() { return 1280 },
    get innerHeight() { return 800 },
    devicePixelRatio: 1,
    setTimeout: (...args) => setTimeout(...args),
    clearTimeout: id => clearTimeout(id),
    setInterval: (...args) => setInterval(...args),
    clearInterval: id => clearInterval(id),
    requestAnimationFrame: callback => setTimeout(() => callback(performance.now()), 0),
    cancelAnimationFrame: id => clearTimeout(id),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    matchMedia: query => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }),
    addEventListener(type, fn) { (listeners.get(type) ?? listeners.set(type, new Set()).get(type)).add(fn) },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn) },
    dispatchEvent(event) {
      for (const fn of listeners.get(event.type) ?? []) fn(event)
      return !event.defaultPrevented
    },
    confirm(message) { confirmations.push(message); return confirm(message) },
    alert(message) { alerts.push(message) },
    open(...args) { opened.push(args); return null },
    listeners,
    opened,
    confirmations,
    alerts,
  }
  const previous = {}
  for (const name of ['window', 'confirm', 'alert']) {
    previous[name] = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, {
      value: name === 'window' ? win : win[name].bind(win),
      configurable: true,
      writable: true,
    })
  }
  win.restore = () => {
    for (const [name, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  }
  return win
}
