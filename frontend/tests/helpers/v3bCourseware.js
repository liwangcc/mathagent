// The courseware library page (views/Courseware.vue) mounted for a signed-in
// primary-school teacher, against an in-memory backend, for
// courseware.test.js and courseware-covers.test.js.
//
//   const library = await openCourseware(t)                       // the bundled chapter cards
//   const library = await openCourseware(t, { items: [...] })      // the backend lists these
//   await library.card('分数的认识与运算：同分母分数大小比较').byText('el-button', '预览演示').dispatch('click')
//   library.backend.requests      // fetches of the bundled samples (url, init)
//   library.client.requests       // API calls (preview, download, list)
//   library.downloads()           // files the page handed to the browser to save
//
// The backend answers GET /courseware with `items` (without them, with no
// list at all, so the page keeps its bundled chapter cards), GET
// /courseware/<id>/preview with previewHtml(id) and GET
// /courseware/<id>/download with a file; the bundled samples under
// /courseware/samples/ are served as sampleHtml(url).
import './srcModules.js'
import { loadApiClient } from './apiClient.js'
import { installFetch } from './memoryFetch.js'
import { memoryDocument } from './memoryDom.js'
import { mountComponent } from './mountComponent.js'

const { default: Courseware } = await import('../../src/views/Courseware.vue?sfc')

export const PRIMARY_TEACHER = { id: 1, role: 'teacher', education_stage: 'primary', profile_complete: true }

export const previewHtml = id => `<!doctype html><html><head></head><body><svg id="s"></svg>后端课件 ${id}</body></html>`
export const sampleHtml = url => `<!doctype html><html><head></head><body><svg id="s"></svg>离线样例 ${decodeURIComponent(url)}</body></html>`

export async function openCourseware(t, { items } = {}) {
  const client = await loadApiClient({
    localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(PRIMARY_TEACHER) },
  })
  client.respond((config) => {
    if (config.url === '/courseware') {
      return { data: items ? { items } : {} }
    }
    const preview = /^\/courseware\/(\d+)\/preview$/.exec(config.url)
    if (preview) return { data: previewHtml(preview[1]) }
    const download = /^\/courseware\/(\d+)\/download$/.exec(config.url)
    if (download) return { data: new Blob([`课件 ${download[1]}`], { type: 'text/html' }) }
    return { data: {} }
  })
  const backend = installFetch(t, ({ url }) => (
    url.startsWith('/courseware/samples/') ? new Response(sampleHtml(url)) : new Response('', { status: 404 })
  ))
  const page = await mountComponent(t, Courseware, { path: '/courseware' })
  // A download hands the browser an <a download> and later revokes its object
  // URL: the link is recorded, the revoke is not scheduled on a real clock.
  page.win.URL = URL
  t.mock.method(page.win, 'setTimeout', () => 0)
  const appended = t.mock.method(memoryDocument.body, 'appendChild')

  const cards = () => page.root.allByClass('course-card')
  const card = title => cards().find(article => article.byTag('h3')[0]?.text === title)
  const dialog = () => page.root.find(el => el.tagName === 'el-dialog' && el.classList.contains('preview-dialog'))
  const downloads = () => appended.mock.calls.map(call => call.arguments[0])
    .filter(el => el.tagName === 'a')
    .map(link => ({ name: link.download, href: link.href }))
  // Ends this page early, so a test can open another one.
  const close = () => {
    page.unmount()
    backend.restore()
  }
  return { client, backend, page, cards, card, dialog, downloads, close }
}
