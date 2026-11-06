import './helpers/srcModules.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import { COURSEWARE_MODELS } from '../src/courseware/models.js'
import { openCourseware, previewHtml, sampleHtml } from './helpers/v3bCourseware.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..', '..')

test('章节模型使用各自的数学状态渲染器', () => {
  const fraction = COURSEWARE_MODELS.fractions.render({ denominator: 8, aNumerator: 3, bNumerator: 5 })
  assert.equal(fraction.result, '<')
  assert.equal(fraction.aFill, 3 / 8)

  const linear = COURSEWARE_MODELS.linear.render({ k: -1, b: 2 })
  assert.equal(linear.intercept.x, linear.sx(0))
  assert.equal(linear.intercept.y, linear.sy(2))
  assert.ok(linear.line.y1 < linear.line.y2, '负斜率应在 SVG 中从左上走向右下')

  const circle = COURSEWARE_MODELS.unitCircle.render({ angle: 150 })
  assert.ok(circle.cos < 0 && circle.sin > 0, '150° 应位于第二象限')
  assert.ok(Math.abs(circle.cos ** 2 + circle.sin ** 2 - 1) < 1e-12)

  const area = COURSEWARE_MODELS.area.render({ width: 8, height: 5 })
  assert.equal(area.area, 40)
  assert.equal(area.perimeter, 26)

  const quadratic = COURSEWARE_MODELS.quadratic.render({ a: -1, h: 2, k: 3 })
  assert.equal(quadratic.vertex.x, 2)
  assert.equal(quadratic.vertex.y, 3)
  assert.equal(quadratic.axis, 2)
  assert.equal(quadratic.yIntercept, -1)

  const vectors = COURSEWARE_MODELS.vectors.render({ x: 3, y: -4 })
  assert.equal(vectors.length, 5)
  assert.equal(vectors.quadrant, '第四象限')

  const transformations = COURSEWARE_MODELS.transformations.render({ transform: 'reflectY', dx: 2, dy: 1 })
  assert.equal(transformations.transform, 'reflectY')
  assert.equal(transformations.dx, 2)
  assert.equal(transformations.dy, 1)

  const views = COURSEWARE_MODELS.views3d.render({ length: 3, width: 1, height: 2, view: 'top' })
  assert.equal(views.frontArea, 6)
  assert.equal(views.topArea, 3)
  assert.equal(views.leftArea, 2)
  assert.equal(views.view, 'top')
})

test('章节离线文件是无外部依赖的可交互 HTML', () => {
  // v4 完整章节是课件库唯一的离线课件来源（旧示例卡已于 2026-09-14 下线）。
  const expected = [
    ['小学', 'P05_分数的认识与运算.html'], ['小学', 'P09_长度、周长与面积.html'],
    ['小学', 'P11_位置、方向与图形运动.html'], ['小学', 'P10_立体图形与体积.html'],
    ['小学', 'P12_统计与数据分析.html'],
    ['初中', 'J08_函数基础与一次函数.html'], ['初中', 'J10_二次函数.html'],
    ['高中', 'H05_三角函数与恒等变换.html'], ['高中', 'H06_平面向量及其应用.html'],
    ['高中', 'H12_空间向量与立体几何.html'],
  ]
  for (const [stageDirectory, fileName] of expected) {
    const file = path.join(root, 'courseware', stageDirectory, fileName)
    const html = fs.readFileSync(file, 'utf8')
    assert.match(html, /<(svg|canvas)/)
    assert.match(html, /aria-live="polite"/)
    assert.match(html, /prefers-reduced-motion/)
    // An inline library's license/documentation URLs are not network dependencies.
    assert.doesNotMatch(html, /<(?:script|link|img)[^>]*(?:src|href)\s*=\s*["'](?:https?:)?\/\//i)
    for (const script of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(script[1])
    assert.equal(html, fs.readFileSync(path.join(root, 'frontend/public/courseware/samples', stageDirectory, fileName), 'utf8'))
  }
  // 保留项：用户自制的 WebGL 三维「空间几何关系」演示（与 H09 章节并存）。
  const keep = path.join(root, 'courseware', '高中', '空间几何关系.html')
  const keepHtml = fs.readFileSync(keep, 'utf8')
  assert.match(keepHtml, /WebGLRenderer/)
  assert.match(keepHtml, /data-rel="ll-intersect"[\s\S]*data-rel="pp-parallel"/)
  assert.doesNotMatch(keepHtml, /点模型移动|恢复标准位置|id="px"/)
  assert.equal(keepHtml, fs.readFileSync(path.join(root, 'frontend/public/courseware/samples', '高中', '空间几何关系.html'), 'utf8'))
})

// The bundled chapter cards of a primary-school teacher's stage.
const PRIMARY_CARDS = [
  '分数的认识与运算：同分母分数大小比较',
  '长度、周长与面积：长方形与正方形的面积',
  '位置、方向与图形运动：平移',
  '立体图形：正方体、圆柱体的面棱顶点',
  '简单几何图形与测量：从不同方向观察物体',
  '统计与数据分析：条形图与平均数',
]
const titles = library => library.cards().map(card => card.byTag('h3')[0].text)

test('预览和下载走同一份离线样例，并保留 API 预览路径', async (t) => {
  const bundled = await openCourseware(t)
  const card = bundled.card('分数的认识与运算：同分母分数大小比较')
  await card.byText('el-button', '预览演示').dispatch('click')
  await bundled.page.flush()
  // The preview loads the chapter's bundled sample, by its stage folder and file name.
  const [previewed] = bundled.backend.requests
  assert.equal(decodeURIComponent(previewed.url), '/courseware/samples/小学/P05_分数的认识与运算.html')
  assert.match(previewed.url, /^[\x21-\x7e]+$/, 'the folder and file name are URL-encoded')
  assert.equal(previewed.init.cache, 'no-store')
  const frame = bundled.dialog().byTag('iframe')[0]
  assert.equal(frame.props.srcdoc, sampleHtml(previewed.url))
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts')

  // Downloading the card saves that very sample.
  await card.byText('el-button', '下载离线课件').dispatch('click')
  await bundled.page.flush()
  assert.deepEqual(bundled.backend.requests.map(request => request.url), [previewed.url, previewed.url])
  assert.deepEqual(bundled.downloads().map(file => file.name), ['P05_分数的认识与运算.html'])
  assert.match(bundled.downloads()[0].href, /^blob:/)
  assert.deepEqual(bundled.client.messages.filter(message => message.type === 'success'), [{ type: 'success', message: '已下载离线课件' }])
  bundled.close()

  // A card the backend lists previews through the backend, not a bundled sample.
  const listed = await openCourseware(t, {
    items: [{ id: 5, title: '分数的意义', chapter: '分数的认识与运算', knowledge_point: '分数的意义', stage: 'primary', file_name: 'P05_分数的认识与运算.html' }],
  })
  await listed.card('分数的意义').byText('el-button', '预览演示').dispatch('click')
  await listed.page.flush()
  assert.deepEqual(listed.client.requests.map(request => [request.method, request.url]), [
    ['get', '/courseware'],
    ['get', '/courseware/5/preview'],
  ])
  assert.deepEqual(listed.backend.requests, [])
  assert.equal(listed.dialog().byTag('iframe')[0].props.srcdoc, previewHtml(5))
})

test('课件库列出本学段全部章节课件和常用章节捷径，不截成 3 个', async (t) => {
  const library = await openCourseware(t)
  assert.deepEqual(titles(library), PRIMARY_CARDS)
  assert.equal(library.page.root.byClass('recommend-heading').byTag('span').at(-1).text, '6 个')
  const shortcuts = () => library.page.root.byClass('topic-strip').byTag('el-button')
  assert.deepEqual(shortcuts().map(button => button.text), [
    '分数的认识与运算', '长度、周长与面积', '位置、方向与图形运动', '立体图形', '简单几何图形与测量', '统计与数据分析',
  ])
  // A chapter shortcut narrows the list to that chapter.
  await shortcuts().find(button => button.text === '统计与数据分析').dispatch('click')
  await library.page.flush()
  assert.deepEqual(titles(library), ['统计与数据分析：条形图与平均数'])
  assert.equal(shortcuts().find(button => button.text === '统计与数据分析').getAttribute('type'), 'primary')
  library.close()

  // From the backend's list: a chapter with several lessons gets one
  // shortcut, and another stage's chapters get none.
  const listed = await openCourseware(t, {
    items: [
      { id: 31, title: '分数的意义', chapter: '分数的认识与运算', knowledge_point: '分数的意义', stage: 'primary', file_name: 'P05_分数的认识与运算.html' },
      { id: 32, title: '同分母分数加减', chapter: '分数的认识与运算', knowledge_point: '分数加减', stage: 'primary', file_name: '分数加减.html' },
      { id: 33, title: '长方形的面积', chapter: '长度、周长与面积', knowledge_point: '面积', stage: 'primary', file_name: 'P09_长度、周长与面积.html' },
      { id: 34, title: '一次函数的图像', chapter: '函数基础与一次函数', knowledge_point: '一次函数', stage: 'junior', file_name: 'J08_函数基础与一次函数.html' },
    ],
  })
  const listedShortcuts = () => listed.page.root.byClass('topic-strip').byTag('el-button')
  assert.deepEqual(listedShortcuts().map(button => button.text), ['分数的认识与运算', '长度、周长与面积'])
  assert.deepEqual(titles(listed), ['分数的意义', '同分母分数加减', '长方形的面积'])
  await listedShortcuts().find(button => button.text === '分数的认识与运算').dispatch('click')
  await listed.page.flush()
  assert.deepEqual(titles(listed), ['分数的意义', '同分母分数加减'])
})
