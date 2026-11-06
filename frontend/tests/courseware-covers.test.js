import './helpers/srcModules.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { defineComponent, ref, toRaw } from 'vue'
import { coursewareCoverForCard, coursewareThumbnailHtml } from '../src/courseware/covers.js'
import { COURSEWARE_MODELS } from '../src/courseware/models.js'
import { loadApiClient } from './helpers/apiClient.js'
import { installFetch } from './helpers/memoryFetch.js'
import { mountComponent } from './helpers/mountComponent.js'
import { openCourseware, previewHtml } from './helpers/v3bCourseware.js'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const read = file => fs.readFileSync(path.join(root, file), 'utf8')
const catalogue = JSON.parse(read('courseware/catalog_v4.json')).entries
const manifest = JSON.parse(read('frontend/src/courseware/covers.json'))
const sha = data => crypto.createHash('sha256').update(data).digest('hex')
const { default: CoursewareCover } = await import('../src/components/CoursewareCover.vue?sfc')

test('每份章节课件及保留演示都有各自真实教学图，来源及图像哈希可复核', () => {
  const entries = [...catalogue, { code: 'SPACE', stage: 'senior', chapter: '空间几何关系', offlineFileName: '空间几何关系.html', coursewarePath: 'courseware/高中/空间几何关系.html' }]
  assert.equal(Object.keys(manifest).length, entries.length)
  assert.deepEqual(Object.keys(manifest).sort(), entries.map(e => e.code).sort())
  const imageHashes = new Set()
  for (const entry of entries) {
    const cover = coursewareCoverForCard(entry, manifest)
    assert.ok(cover, `${entry.code}: missing cover`)
    assert.equal(cover.code, entry.code)
    assert.equal(cover.chapter, entry.chapter)
    assert.equal(cover.sourcePath, entry.coursewarePath)
    assert.equal(cover.sourceSha256, sha(fs.readFileSync(path.join(root, entry.coursewarePath))))
    assert.match(cover.src, /^\/courseware\/covers\/[A-Z0-9]+\.webp$/)
    const bytes = fs.readFileSync(path.join(root, 'frontend/public', cover.src.slice(1)))
    assert.equal(bytes.toString('ascii', 0, 4), 'RIFF')
    assert.equal(bytes.toString('ascii', 8, 12), 'WEBP')
    assert.equal(cover.sha256, sha(bytes))
    assert.ok(bytes.length > 2000, `${entry.code}: suspiciously empty image`)
    assert.equal(cover.width, 800)
    assert.equal(cover.height, 450)
    assert.ok(cover.sceneTitle && cover.sceneId, `${entry.code}: source scene missing`)
    imageHashes.add(cover.sha256)
  }
  assert.equal(imageHashes.size, entries.length, 'chapters must not share a generic cover')
})

test('API 字段与全部离线兜底模型都能定位本学段的正确封面', () => {
  for (const entry of catalogue) {
    const card = { education_stage: entry.stage, file_name: entry.offlineFileName, chapter: entry.chapter }
    assert.equal(coursewareCoverForCard(card, manifest)?.code, entry.code)
  }
  for (const model of Object.values(COURSEWARE_MODELS)) {
    assert.ok(coursewareCoverForCard(model, manifest), model.key)
  }
})

test('不跨学段、不按标题关键词套图，真实文件名优先于继承的模型文件名', () => {
  assert.equal(coursewareCoverForCard({ stage: 'junior', file_name: 'P05_分数的认识与运算.html' }, manifest), null)
  assert.equal(coursewareCoverForCard({ stage: 'primary', title: '分数' }, manifest), null)
  assert.equal(coursewareCoverForCard({ stage: 'primary', file_name: '自定义分数.html', offlineFileName: 'P05_分数的认识与运算.html', chapter: '分数的认识与运算' }, manifest), null)
  assert.equal(coursewareCoverForCard({ stage: 'primary', chapter: '分数的认识与运算' }, manifest)?.code, 'P05')
  assert.equal(coursewareCoverForCard(null, manifest), null)
})

test('未知课件缩略展示保持实际 HTML，CSP 在课件脚本前并禁止联网及表单', () => {
  for (const source of ['<!doctype html><html><head><title>图</title></head><body><svg><circle r="5"/></svg></body></html>', '<svg><rect width="4" height="4"/></svg>']) {
    const html = coursewareThumbnailHtml(source)
    assert.match(html, /Content-Security-Policy/)
    assert.match(html, /connect-src 'none'/)
    assert.match(html, /form-action 'none'/)
    assert.ok(html.indexOf('Content-Security-Policy') < html.indexOf('<script'))
    assert.match(html, /<(circle|rect)\b/)
    assert.match(html, /document\.body\.style\.visibility='hidden'/)
  }
})

test('所有课件卡片统一使用图形封面，没有纯文字封面分支；预览下载不变', async (t) => {
  const library = await openCourseware(t, {
    items: [
      { id: 11, title: '分数的意义', chapter: '分数的认识与运算', knowledge_point: '分数的意义', stage: 'primary', file_name: 'P05_分数的认识与运算.html' },
      { id: 12, title: '长方形的面积', chapter: '长度、周长与面积', knowledge_point: '面积', stage: 'primary', file_name: 'P09_长度、周长与面积.html' },
      // A lesson of the school's own, with no chapter model and no shipped cover.
      { id: 13, title: '数学广角：搭配', chapter: '数学广角', knowledge_point: '搭配', stage: 'primary', file_name: '数学广角.html' },
      { id: 14, title: '条形统计图', chapter: '统计与数据分析', knowledge_point: '条形图', stage: 'primary', file_name: 'P12_统计与数据分析.html' },
    ],
  })
  const { page, client } = library
  // Every card leads with one graphic cover: its chapter's teaching image, or
  // for the school's own lesson a live thumbnail of that lesson itself.
  const covers = library.cards().map((card) => {
    const figures = card.children.filter(el => el.tagName === 'figure' && el.classList.contains('courseware-cover'))
    assert.equal(figures.length, 1, card.text)
    assert.equal(card.children[0], figures[0])
    const [figure] = figures
    return [figure.getAttribute('data-cover-code'), figure.byTag('img')[0]?.getAttribute('src') ?? figure.byTag('iframe')[0]?.props.srcdoc]
  })
  assert.deepEqual(covers, [
    ['P05', '/courseware/covers/P05.webp'],
    ['P09', '/courseware/covers/P09.webp'],
    ['live', coursewareThumbnailHtml(previewHtml(13))],
    ['P12', '/courseware/covers/P12.webp'],
  ])
  assert.equal(page.root.find(el => ['catalog-cover', 'visual-copy', 'card-visual'].some(name => el.classList.contains(name))), null)

  // Each card's buttons act on that card.
  await library.card('长方形的面积').byText('el-button', '预览演示').dispatch('click')
  await page.flush()
  assert.equal(library.dialog().getAttribute('title'), '长方形的面积')
  assert.equal(library.dialog().byTag('iframe')[0].props.srcdoc, previewHtml(12))
  await library.card('条形统计图').byText('el-button', '下载离线课件').dispatch('click')
  await page.flush()
  assert.deepEqual(client.requests.map(request => [request.method, request.url]), [
    ['get', '/courseware'],
    ['get', '/courseware/13/preview'],
    ['get', '/courseware/12/preview'],
    ['get', '/courseware/14/download'],
  ])
  assert.deepEqual(library.downloads().map(file => file.name), ['P12_统计与数据分析.html'])
})

// IntersectionObserver as a browser provides it; `enter()` is the cover
// scrolling near the viewport (with false: an entry that is still too far).
function installIntersectionObserver(t) {
  const observers = []
  globalThis.IntersectionObserver = class {
    constructor(callback, options) {
      Object.assign(this, { callback, options, targets: [], disconnects: 0 })
      observers.push(this)
    }
    observe(target) { this.targets.push(target) }
    disconnect() { this.disconnects += 1 }
    enter(isIntersecting = true) { this.callback(this.targets.map(target => ({ target, isIntersecting }))) }
  }
  t.after(() => { delete globalThis.IntersectionObserver })
  return observers
}

// A cover on a page that can swap the card it shows, as the list does when it reloads.
async function mountCover(t, card) {
  const Host = defineComponent({
    components: { CoursewareCover },
    setup: () => ({ card: ref(card) }),
    template: '<CoursewareCover :card="card" />',
  })
  const page = await mountComponent(t, Host)
  return { page, figure: () => page.root.byClass('courseware-cover') }
}

test('封面惰性加载、图片失败回退到本课件；自定义预览仍隔离并防旧请求回写', async (t) => {
  await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  const observers = installIntersectionObserver(t)
  const sample = '<!doctype html><html><head></head><body><svg id="s"></svg>分数样例</body></html>'
  const backend = installFetch(t, () => new Response(sample))
  const { page, figure } = await mountCover(t, COURSEWARE_MODELS.fractions)

  // The shipped teaching image loads lazily and decodes off the main thread.
  const image = figure().byTag('img')[0]
  assert.equal(image.getAttribute('src'), '/courseware/covers/P05.webp')
  assert.equal(image.getAttribute('loading'), 'lazy')
  assert.equal(image.getAttribute('decoding'), 'async')
  const [observer] = observers
  assert.equal(observer.targets.length, 1)
  // (the component holds its element in a ref, which wraps a memory element in a proxy)
  assert.equal(toRaw(observer.targets[0]), figure(), 'the observer watches the cover itself')
  assert.deepEqual(observer.options, { rootMargin: '180px' })

  // If the image fails, the cover falls back to this lesson's own HTML, but
  // fetches it only once the cover comes near the viewport.
  await image.dispatch('error')
  await page.flush()
  assert.equal(figure().byTag('img').length, 0)
  assert.equal(figure().byClass('cover-loading').text, '正在提取课件教学图…')
  observer.enter(false)
  await page.flush()
  assert.deepEqual(backend.requests, [])
  observer.enter()
  await page.flush()
  assert.deepEqual(backend.requests.map(request => decodeURIComponent(request.url)), ['/courseware/samples/小学/P05_分数的认识与运算.html'])
  assert.equal(observer.disconnects, 1, 'one look is enough')
  const frame = figure().byTag('iframe')[0]
  // The thumbnail runs the lesson's scripts in an opaque origin, without network or forms.
  assert.equal(frame.props.srcdoc, coursewareThumbnailHtml(sample))
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts')
  assert.equal(frame.getAttribute('tabindex'), '-1')

  page.unmount()
  assert.equal(observer.disconnects, 2, 'unmounting stops observing')
})

// The usual order is the other way round: the cover is already in view when
// it mounts, the observer reports it at once and, the image being fine, stops
// watching. When the image fails after that, only its error event is left to
// fetch this lesson's HTML.
test('封面已在视口内、图片之后才加载失败时，由图片的 error 回退到本课件', async (t) => {
  await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  const observers = installIntersectionObserver(t)
  const sample = '<!doctype html><html><head></head><body><svg id="s"></svg>分数样例</body></html>'
  const backend = installFetch(t, () => new Response(sample))
  const { page, figure } = await mountCover(t, COURSEWARE_MODELS.fractions)

  const [observer] = observers
  observer.enter()
  await page.flush()
  assert.deepEqual(backend.requests, [], 'a working teaching image needs no fallback')
  assert.equal(observer.disconnects, 1, 'one look is enough')
  assert.equal(figure().byTag('img')[0].getAttribute('src'), '/courseware/covers/P05.webp')

  await figure().byTag('img')[0].dispatch('error')
  await page.flush()
  assert.deepEqual(backend.requests.map(request => decodeURIComponent(request.url)), ['/courseware/samples/小学/P05_分数的认识与运算.html'])
  assert.equal(figure().byTag('img').length, 0)
  const frame = figure().byTag('iframe')[0]
  assert.equal(frame.props.srcdoc, coursewareThumbnailHtml(sample))
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts')
  assert.equal(figure().byClass('live-cover').props['aria-busy'], false)
})

test('换了课件的封面不会被上一份课件迟到的预览覆盖', async (t) => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  const observers = installIntersectionObserver(t)
  const pending = new Map()
  client.respond(config => new Promise((resolve) => { pending.set(config.url, html => resolve({ data: html })) }))
  const lesson = id => ({ id: `api-${id}`, backendId: id, stage: 'primary', chapter: '数学广角', title: `自编课件 ${id}`, file_name: `自编${id}.html` })
  const { page, figure } = await mountCover(t, lesson(21))
  observers[0].enter()
  await page.flush()
  assert.deepEqual([...pending.keys()], ['/courseware/21/preview'])
  assert.equal(figure().getAttribute('data-cover-code'), 'live')

  // The list now shows another lesson in this place, before the first answered.
  page.setup.card = lesson(22)
  await page.flush()
  assert.deepEqual([...pending.keys()], ['/courseware/21/preview', '/courseware/22/preview'])
  pending.get('/courseware/22/preview')('<svg>课件 22</svg>')
  await page.flush()
  pending.get('/courseware/21/preview')('<svg>课件 21</svg>')
  await page.flush()
  const frame = figure().byTag('iframe')[0]
  assert.equal(frame.props.srcdoc, coursewareThumbnailHtml('<svg>课件 22</svg>'))
  assert.equal(frame.getAttribute('title'), '自编课件 22图形封面')
  assert.equal(figure().byClass('live-cover').props['aria-busy'], false)
})

// Style only: reduced motion turning off the cover image's hover zoom has no
// outcome without a browser's style engine, so the rule is checked as written.
test('封面的悬停缩放在减少动态效果时关闭', () => {
  const component = read('frontend/src/components/CoursewareCover.vue')
  assert.match(component, /prefers-reduced-motion:reduce/)
})
