// An in-memory node tree and the nodeOps/patchProp a Vue custom renderer
// (createRenderer from 'vue') needs to mount a compiled component in Node.
// Not a DOM: no layout, no CSS, no HTML parsing. Elements keep attributes,
// DOM properties, `on*` handlers and addEventListener listeners, a style
// object, a classList and a dataset: what runtime-dom's vModel*, vShow and
// Transition touch on an element, and what a test queries.
//
// runtime-dom also names DOM globals in `instanceof` checks (vModelText:
// Document and ShadowRoot; TransitionGroup: Element), and pages read
// `document`; installDomGlobals() defines those for the test's duration.

export class MNode {
  parentNode = null
  get nextSibling() {
    const siblings = this.parentNode?.childNodes
    if (!siblings) return null
    return siblings[siblings.indexOf(this) + 1] ?? null
  }
  get ownerDocument() { return memoryDocument }
  getRootNode() {
    let node = this
    while (node.parentNode) node = node.parentNode
    return node
  }
}

export class MText extends MNode {
  nodeType = 3
  constructor(text) { super(); this.data = String(text) }
  get textContent() { return this.data }
  set textContent(value) { this.data = String(value) }
}

export class MComment extends MNode {
  nodeType = 8
  constructor(text) { super(); this.data = String(text) }
  get textContent() { return '' }
}

// A selector list for MElement's matches / closest / querySelector(All): one
// test per comma-separated compound selector, built from a tag, `#id`,
// `.class` and `[attr]` / `[attr="value"]` parts. Combinators and
// pseudo-classes are not supported and throw, rather than match nothing.
const SELECTOR_PART = /^(?:([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([\w-]+)))?\])/
function selectorTests(selector) {
  return String(selector).split(',').map((compound) => {
    let rest = compound.trim()
    const parts = []
    while (rest) {
      const match = SELECTOR_PART.exec(rest)
      if (!match) throw new SyntaxError(`memoryDom: unsupported selector "${selector}"`)
      const [text, tag, id, className, attribute, ...values] = match
      const value = values.find(item => item !== undefined)
      if (tag) parts.push(el => el.tagName === tag)
      else if (id) parts.push(el => el.id === id)
      else if (className) parts.push(el => el.classList.contains(className))
      else parts.push(el => el.hasAttribute(attribute) && (value === undefined || el.getAttribute(attribute) === value))
      rest = rest.slice(text.length)
    }
    if (!parts.length) throw new SyntaxError(`memoryDom: unsupported selector "${selector}"`)
    return el => parts.every(part => part(el))
  })
}

// The event a listener prop is for, as Vue names it: `onClickOnce` -> `click`,
// `onUpdate:modelValue` -> `update:modelValue`, `onRowClick` -> `rowClick`.
const eventName = (key) => {
  let name = key.slice(2)
  let modifier
  while ((modifier = /(?:Once|Passive|Capture)$/.exec(name))) name = name.slice(0, -modifier[0].length)
  return name[0].toLowerCase() + name.slice(1)
}

export class MElement extends MNode {
  nodeType = 1
  childNodes = []
  attributes = {}
  props = {}
  handlers = {}
  listeners = new Map()
  style = {}
  dataset = {}
  className = ''
  value = ''
  constructor(tag, namespace) {
    super()
    this.tagName = tag
    this.namespace = namespace
  }
  get type() { return this.attributes.type ?? this.props.type }
  get id() { return this.attributes.id ?? '' }
  // <select>: what vModelSelect reads and writes.
  get options() { return this.findAll(el => el.tagName === 'option') }
  get selectedIndex() { return this.options.findIndex(option => option.selected) }
  set selectedIndex(index) { this.options.forEach((option, i) => { option.selected = i === index }) }
  get children() { return this.childNodes.filter(node => node instanceof MElement) }
  get textContent() { return this.childNodes.map(node => node.textContent).join('') }
  set textContent(value) {
    for (const child of this.childNodes) child.parentNode = null
    this.childNodes = value === '' || value == null ? [] : [Object.assign(new MText(value), { parentNode: this })]
  }
  get classList() {
    const el = this
    const list = () => el.className.split(/\s+/).filter(Boolean)
    return {
      add: (...names) => { el.className = [...new Set([...list(), ...names])].join(' ') },
      remove: (...names) => { el.className = list().filter(name => !names.includes(name)).join(' ') },
      contains: name => list().includes(name),
      toggle: (name, force) => {
        const on = force ?? !list().includes(name)
        if (on) el.classList.add(name)
        else el.classList.remove(name)
        return on
      },
      get length() { return list().length },
      [Symbol.iterator]: () => list()[Symbol.iterator](),
    }
  }
  setAttribute(name, value) { this.attributes[name] = String(value) }
  getAttribute(name) { return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name) }
  removeAttribute(name) { delete this.attributes[name] }
  addEventListener(type, fn) {
    const set = this.listeners.get(type) ?? this.listeners.set(type, new Set()).get(type)
    set.add(fn)
  }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn) }
  getBoundingClientRect() { return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 } }
  get offsetHeight() { return 0 }
  get offsetWidth() { return 0 }
  get clientWidth() { return 0 }
  get clientHeight() { return 0 }
  scrollIntoView() {}
  scrollTo() {}
  focus() { memoryDocument.activeElement = this }
  blur() { if (memoryDocument.activeElement === this) memoryDocument.activeElement = null }
  click() { return this.dispatch('click') }
  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true
    return false
  }
  // The element queries pages call, for simple selectors only (see selectorTests).
  matches(selector) {
    return selectorTests(selector).some(test => test(this))
  }
  closest(selector) {
    const tests = selectorTests(selector)
    for (let node = this; node instanceof MElement; node = node.parentNode) if (tests.some(test => test(node))) return node
    return null
  }
  querySelectorAll(selector) {
    const tests = selectorTests(selector)
    return this.findAll(el => tests.some(test => test(el)))
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null }
  cloneNode() {
    const clone = new MElement(this.tagName, this.namespace)
    Object.assign(clone.attributes, this.attributes)
    clone.className = this.className
    clone.style = { ...this.style }
    return clone
  }
  appendChild(child) { insert(child, this, null); return child }
  removeChild(child) { remove(child); return child }
  remove() { remove(this) }

  // Dispatch an event as a browser would, for a test: the target's `on*`
  // handlers and listeners run, then each ancestor's, until
  // stopPropagation(). Resolves with the handlers' results, so a test can
  // await an async handler.
  dispatch(type, init = {}) {
    let stopped = false
    const event = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      timeStamp: performance.now(),
      preventDefault() { this.defaultPrevented = true },
      stopPropagation() { stopped = true },
      stopImmediatePropagation() { stopped = true },
      ...init,
    }
    const results = []
    for (let node = this; node && !stopped; node = node.parentNode) {
      if (!(node instanceof MElement)) continue
      event.currentTarget = node
      for (const fn of [node.handlers[type]].flat().filter(Boolean)) results.push(fn(event))
      for (const fn of node.listeners.get(type) ?? []) results.push(fn(event))
    }
    return Promise.all(results)
  }

  // Queries for tests.
  *walk() {
    for (const child of this.childNodes) {
      if (child instanceof MElement) {
        yield child
        yield* child.walk()
      }
    }
  }
  findAll(predicate) { return [...this.walk()].filter(predicate) }
  find(predicate) { return this.findAll(predicate)[0] ?? null }
  byTag(tag) { return this.findAll(el => el.tagName === tag) }
  byClass(name) { return this.find(el => el.classList.contains(name)) }
  allByClass(name) { return this.findAll(el => el.classList.contains(name)) }
  // The innermost `tag` element whose text includes `text`.
  byText(tag, text) {
    const matches = this.findAll(el => (tag === '*' || el.tagName === tag) && el.textContent.includes(text))
    return matches.find(el => !matches.some(other => other !== el && el.contains(other))) ?? null
  }
  get text() { return this.textContent.replace(/\s+/g, ' ').trim() }
}

// The tree as HTML-like text, for failure messages and debugging.
export function toHtml(node) {
  if (node instanceof MText) return node.data
  if (node instanceof MComment) return `<!--${node.data}-->`
  const attrs = { ...node.attributes }
  if (node.className) attrs.class = node.className
  const style = Object.entries(node.style).filter(([, value]) => value !== '' && value != null)
    .map(([key, value]) => `${key}:${value}`).join(';')
  if (style) attrs.style = style
  const open = [node.tagName, ...Object.entries(attrs).map(([key, value]) => (value === '' ? key : `${key}="${value}"`))].join(' ')
  return `<${open}>${node.childNodes.map(toHtml).join('')}</${node.tagName}>`
}

const documentListeners = new Map()
export const memoryDocument = {
  body: new MElement('body'),
  head: new MElement('head'),
  documentElement: new MElement('html'),
  activeElement: null,
  compatMode: 'CSS1Compat',
  visibilityState: 'visible',
  hidden: false,
  title: '',
  roots: [],
  listeners: documentListeners,
  createElement: tag => new MElement(tag),
  createElementNS: (namespace, tag) => new MElement(tag, namespace),
  createTextNode: text => new MText(text),
  getElementById: id => memoryDocument.roots.flatMap(root => root.findAll(el => el.id === id))[0] ?? null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener(type, fn) { (documentListeners.get(type) ?? documentListeners.set(type, new Set()).get(type)).add(fn) },
  removeEventListener(type, fn) { documentListeners.get(type)?.delete(fn) },
}

function insert(child, parent, anchor) {
  if (child.parentNode) remove(child)
  const index = anchor ? parent.childNodes.indexOf(anchor) : -1
  if (index >= 0) parent.childNodes.splice(index, 0, child)
  else parent.childNodes.push(child)
  child.parentNode = parent
}

function remove(child) {
  const parent = child.parentNode
  if (!parent) return
  const index = parent.childNodes.indexOf(child)
  if (index >= 0) parent.childNodes.splice(index, 1)
  child.parentNode = null
}

const DOM_PROPS = new Set(['checked', 'selected', 'disabled', 'innerHTML', 'textContent', 'indeterminate', 'muted', 'multiple', 'readOnly', 'srcObject'])

function parseStyle(text) {
  const style = {}
  for (const declaration of String(text).split(';')) {
    const at = declaration.indexOf(':')
    if (at > 0) style[declaration.slice(0, at).trim()] = declaration.slice(at + 1).trim()
  }
  return style
}

export const nodeOps = {
  insert,
  remove,
  createElement: (tag, namespace) => new MElement(tag, namespace),
  createText: text => new MText(text),
  createComment: text => new MComment(text),
  setText: (node, text) => { node.data = String(text) },
  setElementText: (el, text) => { el.textContent = text },
  parentNode: node => node.parentNode,
  nextSibling: node => node.nextSibling,
  querySelector: () => null,
  setScopeId: (el, id) => { el.setAttribute(id, '') },
  cloneNode: el => el.cloneNode(),
  insertStaticContent() {
    throw new Error('memoryDom: static content needs an HTML parser; compile templates with hoistStatic: false')
  },
}

export function patchProp(el, key, prev, next) {
  el.props[key] = next
  if (key === 'class') {
    el.className = next == null ? '' : String(next)
  } else if (key === 'style') {
    el.style = next == null ? {} : typeof next === 'string' ? parseStyle(next) : { ...next }
    // vShow owns `display` while it hides the element (runtime-dom marks the
    // element with its private Symbol('_vsh')).
    const hidden = Object.getOwnPropertySymbols(el).find(symbol => symbol.description === '_vsh')
    if (hidden && el[hidden]) el.style.display = 'none'
  } else if (/^on[^a-z]/.test(key)) {
    const name = eventName(key)
    if (next == null) delete el.handlers[name]
    else el.handlers[name] = next
  } else if (key === 'value') {
    el._value = next
    el.value = next == null ? '' : String(next)
  } else if (key === 'true-value' || key === 'false-value') {
    el[key === 'true-value' ? '_trueValue' : '_falseValue'] = next
  } else if (key.startsWith('data-')) {
    el.dataset[key.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = next == null ? undefined : String(next)
    if (next == null || next === false) el.removeAttribute(key)
    else el.setAttribute(key, next === true ? '' : next)
  } else if (DOM_PROPS.has(key)) {
    el[key] = next
    if (key === 'disabled' || key === 'readOnly') {
      if (next == null || next === false) el.removeAttribute(key.toLowerCase())
      else el.setAttribute(key.toLowerCase(), '')
    }
  } else if (next == null || next === false) {
    el.removeAttribute(key)
  } else {
    el.setAttribute(key, next === true ? '' : next)
  }
}

const GLOBALS = {
  Element: MElement,
  Node: MNode,
  Document: class MemoryDocument {},
  ShadowRoot: class MemoryShadowRoot {},
  document: memoryDocument,
}

export function installDomGlobals() {
  const previous = {}
  for (const [name, value] of Object.entries(GLOBALS)) {
    previous[name] = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
  }
  return () => {
    for (const [name, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
    memoryDocument.roots.length = 0
    memoryDocument.activeElement = null
    documentListeners.clear()
  }
}
