import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { coursewareCoverForCard, coursewareThumbnailHtml } from '../src/courseware/covers.js'
import { COURSEWARE_MODELS } from '../src/courseware/models.js'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const read = file => fs.readFileSync(path.join(root, file), 'utf8')
const catalogue = JSON.parse(read('courseware/catalog_v4.json')).entries
const manifest = JSON.parse(read('frontend/src/courseware/covers.json'))
const sha = data => crypto.createHash('sha256').update(data).digest('hex')

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

test('所有课件卡片统一使用图形封面，没有纯文字封面分支；预览下载不变', () => {
  const view = read('frontend/src/views/Courseware.vue')
  const template = view.slice(0, view.indexOf('<script setup>'))
  assert.match(template, /v-for="card in filteredCards"/)
  assert.match(template, /<CoursewareCover :card="card" \/>/)
  assert.doesNotMatch(template, /catalog-cover|visual-copy|v-else-if="card\.modelType/)
  assert.match(template, /@click="openPreview\(card\)"/)
  assert.match(template, /@click="downloadCourseware\(card\)"/)
})

test('封面惰性加载、图片失败回退到本课件；自定义预览仍隔离并防旧请求回写', () => {
  const component = read('frontend/src/components/CoursewareCover.vue')
  assert.match(component, /loading="lazy"/)
  assert.match(component, /decoding="async"/)
  assert.match(component, /@error="imageFailed = true; loadLiveCover\(\)"/)
  assert.match(component, /new IntersectionObserver/)
  assert.match(component, /sandbox="allow-scripts"/)
  assert.doesNotMatch(component, /allow-same-origin/)
  assert.match(component, /revision === request/)
  assert.match(component, /observer\?\.disconnect\(\)/)
  assert.match(component, /prefers-reduced-motion:reduce/)
})
