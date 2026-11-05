// In-memory stand-ins for the two browser globals the API client and stores
// touch at runtime: Web Storage and `location`. Not a DOM: `window` and
// `document` stay undefined, so libraries keep their server-side behaviour.

export function memoryStorage(entries = {}) {
  const data = new Map(Object.entries(entries).map(([key, value]) => [key, String(value)]))
  return {
    getItem: key => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(key, String(value)) },
    removeItem: key => { data.delete(key) },
    clear: () => { data.clear() },
    key: index => [...data.keys()][index] ?? null,
    get length() { return data.size },
  }
}

function define(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
  return value
}

// A `location` whose navigations (`href =`, `assign`, `replace`) are recorded
// in `navigations` instead of leaving the page.
export function memoryLocation(url = 'http://localhost/') {
  let current = new URL(url)
  const navigations = []
  const navigate = (target) => {
    navigations.push(String(target))
    current = new URL(target, current)
  }
  const location = {
    navigations,
    get href() { return current.href },
    set href(target) { navigate(target) },
    get origin() { return current.origin },
    get protocol() { return current.protocol },
    get host() { return current.host },
    get hostname() { return current.hostname },
    get pathname() { return current.pathname },
    get search() { return current.search },
    get hash() { return current.hash },
    assign: navigate,
    replace: navigate,
    reload() {},
  }
  return location
}

// Installs fresh localStorage, sessionStorage and location globals.
export function installBrowserGlobals({ url, localStorage = {}, sessionStorage = {} } = {}) {
  return {
    localStorage: define('localStorage', memoryStorage(localStorage)),
    sessionStorage: define('sessionStorage', memoryStorage(sessionStorage)),
    location: define('location', memoryLocation(url)),
  }
}
