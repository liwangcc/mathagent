import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import { COURSEWARE_MODELS } from '../src/courseware/models.js'

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

test('预览和下载走同一份离线样例，并保留 API 预览路径', () => {
  const view = fs.readFileSync(path.join(root, 'frontend', 'src', 'views', 'Courseware.vue'), 'utf8')
  assert.match(view, /async function openPreview\(card\)/)
  assert.match(view, /api\.courseware\.preview\(card\.backendId\)/)
  assert.match(view, /courseware\/samples\//)
  assert.match(view, /stageDirectoryByValue/)
  assert.match(view, /sampleUrl\(card, fileName\)/)
  assert.match(view, /chapterShortcuts = computed/)
  assert.doesNotMatch(view, /filteredCards = computed[^\n]*\.slice\(0, 3\)/)
})
