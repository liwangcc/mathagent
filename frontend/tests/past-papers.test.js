import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/srcModules.js'
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
import { messages } from './helpers/elementPlus.js'
import { mountComponent } from './helpers/mountComponent.js'
import {
  button,
  captureDownloads,
  choose,
  flush,
  holdingBackend,
  installTeleportToBody,
  mountView,
  route,
  signIn,
} from './helpers/viewsStageReports.js'

const { default: PastPapers } = await import('../src/views/PastPapers.vue?sfc')

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

// The library page as a teacher sees it: the list answered with `items` (or
// with what `list` returns), a paper's detail with `detail`, preview pages as
// images and downloads as PDFs.
async function openLibrary(t, { items = [], years = [2024], detail = paperId => items.find(item => item.paper_id === paperId), list } = {}) {
  const { client, pinia } = await signIn({ stage: 'senior' })
  const backend = holdingBackend(client, (config) => {
    if (route(config) === 'get /past-papers') {
      return list?.(config) ?? { data: { stage_supported: true, items, facets: { years, series: [], regions: [] } } }
    }
    const match = /^get \/past-papers\/([^/]+)$/.exec(route(config))
    if (match) return { data: detail(decodeURIComponent(match[1])) }
    if (/\/preview\//.test(config.url)) return { data: new Blob(['page image'], { type: 'image/png' }) }
    return { data: new Blob(['%PDF-1.7'], { type: 'application/pdf' }) }
  })
  const page = await mountView(t, PastPapers, { path: '/bank/past-papers', pinia })
  return { client, backend, page }
}

// Series mode lists every paper (region mode asks for a region first).
const showSeries = async (page) => {
  await choose(page.root.find(el => el.tagName === 'el-radio-group' && el.getAttribute('aria-label') === '浏览方式'), 'series')
  await flush()
}

test('actual Vue list and detail templates render truthful purchased and legacy metadata', async (t) => {
  const item = paper({ pages: 2, preview_pages: 2, answer_available: true,
    edition: { kind: 'purchased_pdf', label: '网络原版 PDF', description: '网络原版 PDF，保留原文件。' },
    verification: { status: 'resource_checked', summary: '身份、角色与全页机械渲染已检查。' },
    student: { pages: 2, page_size: 'A4', page_dimensions_mm: [{ width: 215.9, height: 279.4 }, { width: 215.9, height: 279.4 }],
      print_note: '原文件水印保留，打印时按纸张缩放。' },
    answer: { available: true, status: 'source_paired', kind: 'solutions', note: '来源配套完整解析。', pages: 1, preview_pages: 1,
      page_dimensions_mm: [{ width: 215.9, height: 279.4 }] }, rights: { notice: '运营决定不是出版社许可。' }, notes: [], attribution: [] })
  // The library page listing `value` (its detail answered with `detail`), its
  // preview opened on the paper tab and then on the answer tab: the card's and
  // the preview's text, the card's answer flag, the page counter of each tab,
  // and the whole tree with its attributes (tooltips, help texts).
  async function renderPaper(value, detail = value) {
    const { page } = await openLibrary(t, { items: [value], detail: () => detail })
    await showSeries(page)
    const card = page.root.byClass('paper-card')
    assert.ok(card, 'the paper is listed')
    const cardText = card.text
    const flag = card.byClass('answer-flag').text
    await card.byText('el-button', '预览').dispatch('click')
    await flush()
    const dialog = page.root.find(el => el.tagName === 'el-dialog' && el.dataset.open === 'true')
    const pageNav = () => dialog.byClass('page-nav').text
    const rendered = { card: cardText, details: dialog.byClass('paper-details').text, html: page.html(), studentPages: pageNav() }
    const tabs = dialog.find(el => el.tagName === 'el-tabs')
    await choose(tabs, 'answer')
    await tabs.handlers['tab-change']?.('answer')
    await flush()
    rendered.answerPages = pageNav()
    rendered.html += page.html()
    page.unmount()
    return { ...rendered, flag, text: `${rendered.card} ${rendered.details}` }
  }
  // The list entry claims 3 answer pages; the paper's detail, which the
  // answer tab pages through, says 1.
  const purchased = await renderPaper({ ...item, answer: { ...item.answer, preview_pages: 3 } }, item)
  assert.match(purchased.card, /网络原版 PDF/)
  assert.match(purchased.text, /215.9 × 279.4 mm 共 2 页/)
  assert.match(purchased.text, /资料检查.*身份、角色与全页机械渲染已检查/s)
  assert.match(purchased.text, /未独立逐题核验数学内容/)
  assert.match(purchased.text, /原文件水印保留，打印时按纸张缩放/)
  assert.match(purchased.card, /含配套解析/)
  assert.match(purchased.details, /来源配套完整解析/)
  assert.match(purchased.details, /运营决定不是出版社许可/)
  assert.doesNotMatch(purchased.html, /A4 共 2 页|仅列最终答案|经多个独立来源核对|试卷均为/)
  // The answer tab pages through the answer file's own preview as the
  // paper's detail describes it, not the paper's pages or the list entry.
  assert.match(purchased.studentPages, /第 1 \/ 2 页/)
  assert.match(purchased.answerPages, /第 1 \/ 1 页/)
  const unpaged = await renderPaper(item, { ...item, answer: { ...item.answer, preview_pages: undefined } })
  assert.match(unpaged.answerPages, /第 1 \/ — 页/)

  const legacy = await renderPaper({ ...item, edition: { kind: 'retypeset', label: '忠实重排版' },
    student: { pages: 2, page_size: 'A4' }, verification: { status: 'verified', summary: '逐题来源核对；另经独立复核。' },
    answer: { available: true, status: 'verified', kind: 'final_answers', note: '非官方参考答案：仅列最终答案，经多个独立来源核对。' } })
  assert.match(legacy.card, /忠实重排版/)
  assert.match(legacy.text, /A4 共 2 页/)
  assert.match(legacy.text, /逐题来源核对；另经独立复核/)
  assert.match(legacy.html, /仅列最终答案，经多个独立来源核对/)
  const generated = await renderPaper({ ...item, answer: { ...item.answer, status: 'generated',
    kind: 'answers_and_solutions', note: 'AI 解答生成的参考答案（含简要解析）。' } })
  assert.match(generated.card, /含 AI 参考答案/)
  assert.match(generated.details, /AI 解答生成的参考答案（含简要解析）。 非官方答案，未经人工逐题审核/)
  const unanswered = await renderPaper({ ...item, answer_available: false, answer: { available: false } })
  assert.match(unanswered.card, /暂无答案/)

  // Records that say less: what the page falls back to.
  const bare = over => paper({ preview_pages: 1, answer_available: true, notes: [], attribution: [], ...over })
  const unknown = await renderPaper(bare({ verification: { status: 'pending' }, answer: { available: true, status: 'unknown', preview_pages: 1 } }))
  assert.equal(unknown.flag, '含答案/解析')
  assert.match(unknown.card, /版本未说明/)
  assert.match(unknown.text, /检查范围未说明：未提供检查范围说明。/)
  assert.match(unknown.details, /非官方参考资料，具体性质与检查范围请查看详情。/)
  assert.match(unknown.html, /content="非官方参考资料，具体性质与检查范围请查看详情。"/, 'the card\'s answer tooltip')
  const bareResource = await renderPaper(bare({ verification: { status: 'resource_checked' }, answer: { available: true, status: 'source_paired', kind: 'final_answers', preview_pages: 1 } }))
  assert.equal(bareResource.flag, '含配套答案')
  assert.match(bareResource.card, /网络原版 PDF/)
  assert.match(bareResource.text, /身份、文件角色、全页机械渲染及分层视觉抽样已检查。 未独立逐题核验数学内容/)
  assert.match(bareResource.text, /保留来源 PDF 版式及原有水印/)
  assert.match(bareResource.details, /来源配套答案\/解析。 未独立逐题核验数学答案，非官方答案。/)
  const bareVerified = await renderPaper(bare({ verification: { status: 'verified' }, answer: { available: true, status: 'verified', preview_pages: 1 } }))
  assert.equal(bareVerified.flag, '含参考答案')
  assert.match(bareVerified.text, /逐题来源核对并经独立复核；不代表官方认证。/)
  assert.match(bareVerified.details, /非官方参考答案，经多个独立来源核对并独立复核。/)
  const bareGenerated = await renderPaper(bare({ answer: { available: true, status: 'generated', preview_pages: 1 } }))
  assert.equal(bareGenerated.flag, '含 AI 参考答案')
  assert.match(bareGenerated.details, /AI 解答生成的参考答案。 非官方答案，未经人工逐题审核/)

  // Every list state the page can be in, as the teacher reaches it.
  const henan = { key: 'henan', name: '河南', years: [2024, 2023] }
  const withheld = { rights_pending: 1, not_verified: 2 }
  const listing = (over = {}) => ({ data: { stage_supported: true, items: [item], ...over, facets: { years: [2024], series: [], regions: [], ...over.facets } } })
  const states = {}
  {
    // Browsing by region (the default) first asks for a region, then lists
    // what that region actually sat.
    const { page } = await openLibrary(t, { list: () => listing({ withheld, facets: { regions: [henan] } }) })
    const picker = page.root.byClass('region-picker')
    assert.ok(picker, 'region browsing asks for a region first')
    assert.match(picker.text, /请选择省份或直辖市，查看该地历年实际使用的数学试卷/)
    assert.match(picker.byText('el-button', '河南').text, /河南\s*2 年/)
    states.region = page.html()
    await picker.byText('el-button', '河南').dispatch('click')
    await flush()
    assert.match(page.root.byClass('year-title').text, /2024年 河南实际使用的试卷/)
    assert.match(page.root.byClass('withheld').text, /另有1 套标记为旧 verified 的未开放资料.*2 套其他未开放资料.*未向教师开放/)
    states.regionResults = page.html()
    page.unmount()
  }
  {
    const { page } = await openLibrary(t, { list: () => listing() })
    assert.ok(page.root.find(el => el.tagName === 'el-empty' && el.getAttribute('description') === '现有试卷的适用地区尚在核实中，请先按卷型浏览'))
    assert.ok(page.root.byText('el-button', '按卷型浏览'))
    states.noRegions = page.html()
    page.unmount()
  }
  {
    const { page } = await openLibrary(t, { list: () => listing({ items: [], withheld, facets: { years: [] } }) })
    const empty = page.root.find(el => el.tagName === 'el-empty' && el.getAttribute('description') === '试卷库暂未开放可下载的整卷真题')
    assert.ok(empty, 'an empty library says so')
    assert.match(empty.byClass('withheld').text, /管理员提示：另有1 套.*，2 套.*，未向教师开放。/)
    states.libraryEmpty = page.html()
    page.unmount()
  }
  {
    const message = '整卷真题目前只提供高中数学，请在个人资料中切换学段。'
    const { page } = await openLibrary(t, { list: () => ({ data: { stage_supported: false, items: [], message } }) })
    assert.ok(page.root.find(el => el.tagName === 'el-empty' && el.getAttribute('description') === message))
    assert.ok(page.root.byText('el-button', '前往个人资料'))
    states.stage = page.html()
    page.unmount()
  }
  {
    const { page } = await openLibrary(t, { list: () => listing({ items: [] }) })
    await showSeries(page)
    assert.ok(page.root.find(el => el.tagName === 'el-empty' && el.getAttribute('description') === '没有符合条件的试卷'))
    assert.ok(page.root.byText('el-button', '清除筛选'))
    states.filteredEmpty = page.html()
    page.unmount()
  }
  {
    const { page } = await openLibrary(t, { list: () => ({ status: 503, data: { detail: '目录暂不可用' } }) })
    assert.equal(page.root.find(el => el.tagName === 'el-result')?.getAttribute('title'), '整卷真题加载失败')
    states.error = page.html()
    page.unmount()
  }

  // Nothing the page shows, tooltips and help included, calls a paper bought or paid for.
  for (const shown of [purchased, unpaged, legacy, generated, unanswered, unknown, bareResource, bareVerified, bareGenerated]) {
    assert.doesNotMatch(shown.html, /购买|付费/)
  }
  for (const [state, html] of Object.entries(states)) assert.doesNotMatch(html, /购买|付费/, state)
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

test('entry points and identity-only API calls are wired', async (t) => {
  // The sidebar lists 整卷真题 and highlights it (not 题库) on its page.
  {
    const { client, pinia } = await signIn({ stage: 'senior' })
    holdingBackend(client, config => (route(config) === 'get /auth/classes'
      ? { data: { classes: [{ id: 1, class_name: '高三（1）班', student_count: 1 }], current_class_id: 1 } }
      : { data: {} }))
    const { default: Layout } = await import('../src/views/Layout.vue?sfc')
    installTeleportToBody(t)
    const page = await mountComponent(t, { render: () => null }, { path: '/bank/past-papers', layout: Layout, pinia })
    const menu = page.root.find(el => el.tagName === 'el-menu')
    const entry = menu.find(el => el.tagName === 'el-menu-item' && el.getAttribute('index') === '/bank/past-papers')
    assert.ok(entry, 'the sidebar has an entry for the past-paper library')
    assert.match(entry.text, /整卷真题/)
    assert.equal(menu.getAttribute('default-active'), '/bank/past-papers')
    assert.equal(page.root.byClass('crumb-group').text, '教学资源')
    assert.equal(page.root.byClass('page-title').text, '整卷真题')
    await page.router.push('/bank/photo-import')
    await flush()
    assert.equal(menu.getAttribute('default-active'), '/bank', 'other bank pages still highlight 题库')
    page.unmount()
  }
  // The question bank links to it.
  {
    const { client, pinia } = await signIn({ stage: 'senior' })
    holdingBackend(client, () => ({ data: {} }))
    const { default: Bank } = await import('../src/views/Bank.vue?sfc')
    const page = await mountView(t, Bank, { path: '/bank', pinia })
    await button(page, '整卷真题').dispatch('click')
    await flush()
    assert.equal(page.router.currentRoute.value.fullPath, '/bank/past-papers')
    page.unmount()
  }
  // Papers are addressed by their catalogue id and file kind only.
  {
    const listed = paper({ paper_id: 'gk/2024 甲', preview_pages: 1, answer_available: true,
      answer: { available: true, status: 'generated', preview_pages: 1 }, notes: [], attribution: [], regions: [] })
    const { client, page } = await openLibrary(t, { items: [listed] })
    const downloads = captureDownloads(t, page)
    for (const text of ['按地区', '按卷型']) assert.ok(page.root.byText('el-radio-button', text), text)
    await showSeries(page)
    await page.root.byClass('paper-card').byText('el-button', '预览').dispatch('click')
    await flush()
    const card = page.root.byClass('paper-card')
    await card.byText('el-button', '下载试卷').dispatch('click')
    await card.byText('el-button', '下载答案/解析').dispatch('click')
    await flush()
    const id = encodeURIComponent('gk/2024 甲')
    const fileRequests = client.requests.filter(config => config.url.startsWith(`/past-papers/${id}`))
    assert.deepEqual(fileRequests.map(config => config.url), [
      `/past-papers/${id}/preview/student/1`,
      `/past-papers/${id}`,
      `/past-papers/${id}/download/student`,
      `/past-papers/${id}/download/answer`,
    ])
    for (const config of fileRequests) {
      assert.equal(config.params, undefined, `${config.url} sends no path or url`)
      assert.equal(config.data, undefined, `${config.url} sends no body`)
    }
    for (const config of client.requests.filter(config => config.url === '/past-papers')) {
      assert.ok(Object.keys(config.params ?? {}).every(key => ['region', 'series_key', 'year', 'track'].includes(key)), JSON.stringify(config.params))
    }
    assert.deepEqual(downloads.map(download => download.filename), ['2024高考数学新课标Ⅰ卷_试卷.pdf', '2024高考数学新课标Ⅰ卷_参考答案.pdf'])
    page.unmount()
  }
  // Each list state says what it is.
  {
    const { page } = await openLibrary(t, { list: () => ({ status: 503, data: { detail: '目录暂不可用' } }) })
    const failed = page.root.find(el => el.tagName === 'el-result')
    assert.equal(failed.getAttribute('title'), '整卷真题加载失败')
    assert.equal(failed.getAttribute('sub-title'), '目录暂不可用')
    assert.deepEqual(messages, [], 'the page reports the failure itself, without a second toast')
    page.unmount()
  }
  for (const [years, description] of [[[2024], '没有符合条件的试卷'], [[], '试卷库暂未开放可下载的整卷真题']]) {
    const { page } = await openLibrary(t, { items: [], years })
    await showSeries(page)
    assert.ok(page.root.find(el => el.tagName === 'el-empty' && el.getAttribute('description') === description), description)
    page.unmount()
  }
})
