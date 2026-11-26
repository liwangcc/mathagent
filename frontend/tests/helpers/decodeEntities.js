import { memoryDocument } from './memoryDom.js'

// renderLatex decodes HTML entities the browser's way, through a <textarea>
// (innerHTML in, value out); helpers/memoryDom.js parses no HTML, so this
// stand-in does that one decoding while the test runs.
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' }
export function decodeEntitiesThroughTextareas(t) {
  const createElement = memoryDocument.createElement
  const withTextareas = (tag) => {
    const el = createElement(tag)
    if (tag !== 'textarea') return el
    let html = ''
    Object.defineProperty(el, 'innerHTML', { get: () => html, set: (value) => { html = String(value) }, configurable: true })
    Object.defineProperty(el, 'value', {
      configurable: true,
      get: () => html.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (entity, name) => {
        if (name[0] === '#') return String.fromCodePoint(Number.parseInt(name.slice(name[1].toLowerCase() === 'x' ? 2 : 1), name[1].toLowerCase() === 'x' ? 16 : 10))
        return ENTITIES[name.toLowerCase()] ?? entity
      }),
    })
    return el
  }
  memoryDocument.createElement = withTextareas
  t.after(() => { if (memoryDocument.createElement === withTextareas) memoryDocument.createElement = createElement })
}
