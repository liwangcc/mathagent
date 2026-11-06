import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  BROWSE_MODES,
  answerState,
  buildPastPaperQuery,
  downloadName,
  editionLabel,
  filePageLabel,
  groupByYear,
  listViewState,
  paperHeadline,
  printNote,
  regionSummary,
  studentPageLabel,
  verificationState,
} from '../src/utils/pastPapers.js'
import { renderedFiles, signedInTeacherRouter } from './helpers/appRouter.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const src = (rel) => fs.readFileSync(path.resolve(here, '..', 'src', rel), 'utf8')

const paper = (over = {}) => ({
  paper_id: 'gk2024-xgk1', year: 2024, session: 'summer', series_name: '新课标Ⅰ卷', track: null, track_name: '',
  regions: [], answer_available: false, ...over,
})

test('query sends only the filters that belong to the current browse mode', () => {
  assert.deepEqual(buildPastPaperQuery({ mode: BROWSE_MODES.region, region: 'henan', seriesKey: 'national_jia', year: '', track: '' }),
    { region: 'henan' })
  assert.deepEqual(buildPastPaperQuery({ mode: BROWSE_MODES.series, region: 'henan', seriesKey: 'national_jia', year: 2023, track: 'science' }),
    { series_key: 'national_jia', year: 2023, track: 'science' })
  assert.deepEqual(buildPastPaperQuery({ mode: BROWSE_MODES.series }), {})
})

test('view states never overlap and region browsing asks for a region first', () => {
  const data = { stage_supported: true, items: [paper()], facets: { years: [2024] } }
  assert.equal(listViewState({ loading: true, data }), 'loading')
  assert.equal(listViewState({ error: '网络错误', data }), 'error')
  assert.equal(listViewState({ data: { stage_supported: false, items: [] } }), 'stage')
  assert.equal(listViewState({ data: { stage_supported: true, items: [], facets: { years: [] } }, mode: 'region' }), 'library-empty')
  assert.equal(listViewState({ data, mode: BROWSE_MODES.region, region: '' }), 'choose-region')
  assert.equal(listViewState({ data, mode: BROWSE_MODES.region, region: 'henan' }), 'results')
  assert.equal(listViewState({ data: { ...data, items: [] }, mode: BROWSE_MODES.series }), 'filtered-empty')
})

test('unavailable answers are explicit and never look downloadable', () => {
  assert.deepEqual(answerState(paper()).available, false)
  assert.match(answerState(paper()).tooltip, /试卷仍可下载/)
  assert.equal(answerState(paper({ answer_available: true })).available, true)
  assert.match(answerState(paper({ answer_available: true })).tooltip, /非官方/)
})

test('labels keep historical names, tracks and spring sessions distinct', () => {
  assert.equal(paperHeadline(paper({ year: 2019, series_name: '全国Ⅰ卷', track: 'science', track_name: '理科' })), '2019年 全国Ⅰ卷（理科）')
  assert.equal(paperHeadline(paper({ session: 'spring', series_name: '上海卷' })), '2024年春季 上海卷')
  assert.equal(downloadName(paper({ year: 2022, series_name: '全国甲卷', track_name: '文科' }), 'answer'), '2022高考数学全国甲卷（文科）_参考答案.pdf')
  assert.equal(downloadName(paper({ series_name: 'a/b:c' }), 'student'), '2024高考数学a_b_c_试卷.pdf')
  const many = Array.from({ length: 11 }, (_, i) => ({ name: `省${i}` }))
  assert.equal(regionSummary(many), '省0、省1、省2、省3、省4、省5 等 11 地')
  assert.deepEqual(groupByYear([paper({ year: 2023 }), paper({ year: 2024 }), paper({ year: 2023, paper_id: 'b' })]).map(g => [g.year, g.papers.length]),
    [[2024, 1], [2023, 2]])
})

test('purchased source-paired solutions never claim independent answer verification', () => {
  const state = answerState(paper({ answer_available: true, answer: {
    status: 'source_paired', kind: 'solutions', note: '购买资料配套解析，非官方答案。',
  } }))
  assert.equal(state.available, true)
  assert.match(state.label, /解析/)
  assert.match(state.tooltip, /未独立逐题核验/)
  assert.doesNotMatch(state.tooltip, /仅列最终答案|经多个独立来源核对/)
})

test('generated answer keys say AI-made and unofficial, never source-paired or verified', () => {
  const state = answerState(paper({ answer_available: true, answer: {
    status: 'generated', kind: 'answers_and_solutions', note: 'AI 解答生成的参考答案（含简要解析）。',
  } }))
  assert.equal(state.available, true)
  assert.equal(state.label, '含 AI 参考答案')
  assert.match(state.tooltip, /AI 解答生成.*非官方答案.*未经人工逐题审核.*使用前请核对/)
  assert.doesNotMatch(state.tooltip, /配套|多个独立来源|仅列最终答案/)
  assert.equal(answerState(paper({ answer_available: false, answer: { status: 'generated', available: true } })).available, false)
})

test('versions and check scope remain distinct for legacy, purchased and unknown records', () => {
  const legacy = paper({ edition: { kind: 'retypeset', label: '忠实重排版' },
    verification: { status: 'verified', summary: '多个独立来源逐题核对；另经独立复核。' },
    student: { pages: 3, page_size: 'A4' }, answer_available: true,
    answer: { status: 'verified', kind: 'final_answers', note: '非官方参考答案：仅列最终答案，经多个独立来源核对。' } })
  assert.equal(editionLabel(legacy), '忠实重排版')
  assert.equal(studentPageLabel(legacy), 'A4 共 3 页')
  assert.match(verificationState(legacy).description, /逐题核对.*独立复核/)
  assert.match(answerState(legacy).tooltip, /仅列最终答案.*多个独立来源核对/)
  assert.equal(printNote(legacy), '')
  const purchased = paper({ edition: { kind: 'purchased_pdf' }, verification: { status: 'resource_checked' } })
  assert.equal(editionLabel(purchased), '网络原版 PDF')
  assert.match(verificationState(purchased).description, /未独立逐题核验数学内容.*不代表官方认证/)
  assert.match(printNote(purchased), /水印.*打印.*缩放/)
  assert.equal(editionLabel(paper()), '版本未说明')
  assert.equal(verificationState(paper()).label, '检查范围未说明')
  assert.doesNotMatch(answerState(paper({ answer_available: true })).tooltip, /逐题核对|仅列最终答案|多个独立来源/)
  assert.equal(answerState(paper({ answer_available: false, answer: { status: 'source_paired', available: true } })).available, false)
})

test('page size is measured, never inferred from page count or a purchased A4 label', () => {
  const resource = (student) => paper({ student, verification: { status: 'resource_checked' } })
  assert.equal(studentPageLabel(resource({ pages: 2, page_size: 'A4', page_dimensions_mm: [
    { width: 215.9, height: 279.4 }, { width: 215.9, height: 279.4 },
  ] })), '215.9 × 279.4 mm 共 2 页')
  assert.equal(studentPageLabel(resource({ pages: 1, page_dimensions_mm: [{ width: 297, height: 210 }] })), 'A4 共 1 页')
  assert.match(studentPageLabel(resource({ pages: 2, page_dimensions_mm: [
    { width: 210, height: 297 }, { width: 215.9, height: 279.4 },
  ] })), /混合尺寸.*210 × 297.*215.9 × 279.4.*共 2 页/)
  for (const dims of [undefined, [], [{ width: NaN, height: 297 }], [{ width: true, height: 297 }]]) {
    assert.equal(studentPageLabel(resource({ pages: 1, page_size: 'A4', page_dimensions_mm: dims })), '尺寸未登记 共 1 页')
  }
  assert.equal(filePageLabel({ pages: null }), '')
})

test('actual Vue list and detail templates render truthful purchased and legacy metadata', async () => {
  const Vue = await import('vue')
  const { parse } = await import('@vue/compiler-sfc')
  const { compile } = await import('@vue/compiler-dom')
  const { renderToString } = await import('@vue/server-renderer')
  const template = parse(src('views/PastPapers.vue')).descriptor.template.content
  const render = new Function('Vue', compile(template, { mode: 'function', prefixIdentifiers: true }).code)(Vue)
  const item = paper({ pages: 2, preview_pages: 2, answer_available: true,
    edition: { kind: 'purchased_pdf', label: '网络原版 PDF', description: '网络原版 PDF，保留原文件。' },
    verification: { status: 'resource_checked', summary: '身份、角色与全页机械渲染已检查。' },
    student: { pages: 2, page_size: 'A4', page_dimensions_mm: [{ width: 215.9, height: 279.4 }, { width: 215.9, height: 279.4 }],
      print_note: '原文件水印保留，打印时按纸张缩放。' },
    answer: { available: true, status: 'source_paired', kind: 'solutions', note: '来源配套完整解析。', pages: 1, preview_pages: 1,
      page_dimensions_mm: [{ width: 215.9, height: 279.4 }] }, rights: { notice: '运营决定不是出版社许可。' }, notes: [], attribution: [] })
  async function renderPaper(value) {
    const noop = () => {}
    const app = Vue.createSSRApp({ render, setup: () => ({
      BROWSE_MODES, TRACK_OPTIONS: [], mode: 'series', region: '', seriesKey: '', year: '', track: '',
      router: { push: noop }, facets: { regions: [], series: [], years: [] }, viewState: 'results',
      groups: groupByYear([value]), regionName: '', withheldText: '', downloading: '', previewVisible: true,
      previewItem: value, previewKind: 'student', narrowScreen: false, detail: value, detailLoading: false,
      page: 1, pageCount: 2, pageLoading: false, pageUrl: '', pageError: '', detailError: '',
      onModeChange: noop, load: noop, pickRegion: noop, clearFilters: noop, openPreview: noop, download: noop,
      loadPage: noop, closePreview: noop, paperHeadline, answerState, editionLabel, filePageLabel,
      studentPageLabel, verificationState, printNote, regionSummary,
    }) })
    for (const name of ['el-card', 'el-tag', 'el-button', 'el-radio-group', 'el-radio-button', 'el-select',
      'el-option', 'el-result', 'el-empty', 'el-tooltip', 'el-dialog', 'el-tabs', 'el-tab-pane', 'HelpButton']) {
      app.component(name, { inheritAttrs: false, setup: (_, { slots, attrs }) => () => Vue.h('div',
        { 'data-component': name, 'data-content': attrs.content, 'data-disabled': attrs.disabled },
        [slots.header?.(), slots.default?.(), slots.extra?.(), slots.footer?.()]) })
    }
    app.directive('loading', { getSSRProps: () => ({}) })
    return renderToString(app)
  }
  const purchasedHtml = await renderPaper(item)
  assert.match(purchasedHtml, /网络原版 PDF/)
  assert.doesNotMatch(purchasedHtml, /购买|付费/)
  assert.match(purchasedHtml, /215.9 × 279.4 mm 共 2 页/)
  assert.match(purchasedHtml, /资料检查.*身份、角色与全页机械渲染已检查/s)
  assert.match(purchasedHtml, /未独立逐题核验数学内容/)
  assert.match(purchasedHtml, /原文件水印保留，打印时按纸张缩放/)
  assert.match(purchasedHtml, /含配套解析/)
  assert.match(purchasedHtml, /来源配套完整解析/)
  assert.doesNotMatch(purchasedHtml, /A4 共 2 页|仅列最终答案|经多个独立来源核对|试卷均为/)
  const legacyHtml = await renderPaper({ ...item, edition: { kind: 'retypeset', label: '忠实重排版' },
    student: { pages: 2, page_size: 'A4' }, verification: { status: 'verified', summary: '逐题来源核对；另经独立复核。' },
    answer: { available: true, status: 'verified', kind: 'final_answers', note: '非官方参考答案：仅列最终答案，经多个独立来源核对。' } })
  assert.match(legacyHtml, /忠实重排版/)
  assert.match(legacyHtml, /A4 共 2 页/)
  assert.match(legacyHtml, /逐题来源核对；另经独立复核/)
  assert.match(legacyHtml, /仅列最终答案，经多个独立来源核对/)
  const generatedHtml = await renderPaper({ ...item, answer: { ...item.answer, status: 'generated',
    kind: 'answers_and_solutions', note: 'AI 解答生成的参考答案（含简要解析）。' } })
  assert.match(generatedHtml, /含 AI 参考答案/)
  assert.match(generatedHtml, /AI 解答生成的参考答案（含简要解析）。 非官方答案，未经人工逐题审核/)
  const view = src('views/PastPapers.vue')
  assert.match(view, /detail\.value\?\.answer\?\.preview_pages \|\| 0/)
  assert.doesNotMatch(view + src('utils/pastPapers.js'), /购买|付费/)
})

test('the past-paper library opens at /bank/past-papers in the teacher layout', async () => {
  const router = await signedInTeacherRouter()
  const record = router.resolve('/bank/past-papers').matched.at(-1)
  assert.equal(typeof record.components.default, 'function', 'the page loads only when opened')
  await router.push('/bank/past-papers')
  const route = router.currentRoute.value
  assert.equal(route.name, 'past-papers')
  assert.equal(route.meta.title, '整卷真题')
  assert.deepEqual(renderedFiles(route), ['views/Layout.vue', 'views/PastPapers.vue'])
})

test('entry points and identity-only API calls are wired', () => {
  const layout = src('views/Layout.vue')
  assert.match(layout, /index="\/bank\/past-papers"[\s\S]*?整卷真题/)
  assert.match(layout, /startsWith\('\/bank\/past-papers'\)\) return '\/bank\/past-papers'/)
  assert.match(src('views/Bank.vue'), /goPastPapers[\s\S]*整卷真题|整卷真题[\s\S]*goPastPapers/)
  const api = src('api/index.js')
  const block = api.slice(api.indexOf('pastPapers: {'), api.indexOf('pastPapers: {') + 1400)
  assert.match(block, /\/past-papers\/\$\{encodeURIComponent\(paperId\)\}\/download\/\$\{encodeURIComponent\(kind\)\}/)
  assert.doesNotMatch(block, /path|url\s*:\s*[a-z]+Url/i)
  const view = src('views/PastPapers.vue')
  for (const text of ['下载试卷', '下载答案/解析', '按地区', '按卷型', '整卷真题加载失败', '没有符合条件的试卷', '试卷库暂未开放可下载的整卷真题']) {
    assert.ok(view.includes(text), text)
  }
})
