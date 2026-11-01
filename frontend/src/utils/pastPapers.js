// 整卷真题：纯函数（筛选参数、分组、标签、文件名），供视图与测试共用。

export const BROWSE_MODES = Object.freeze({ region: 'region', series: 'series' })

export const TRACK_OPTIONS = Object.freeze([
  { value: '', label: '全部文理' },
  { value: 'science', label: '理科' },
  { value: 'liberal', label: '文科' },
  { value: 'none', label: '不分文理' },
])

const UNSAFE_FILENAME = /[\\/:*?"<>|\u0000-\u001f]/g

// Only send filters the user actually set; the API rejects unknown values.
export function buildPastPaperQuery({ mode, region, seriesKey, year, track } = {}) {
  const params = {}
  if (mode === BROWSE_MODES.region && region) params.region = region
  if (mode === BROWSE_MODES.series && seriesKey) params.series_key = seriesKey
  if (year !== '' && year !== null && year !== undefined) params.year = Number(year)
  if (track) params.track = track
  return params
}

// Region browsing needs a region first: 省份 → 年份 → 当年实际使用的试卷.
export function needsRegionChoice(mode, region) {
  return mode === BROWSE_MODES.region && !region
}

export function paperHeadline(item) {
  if (!item) return ''
  const track = item.track_name ? `（${item.track_name}）` : ''
  const spring = item.session === 'spring' ? '春季' : ''
  return `${item.year}年${spring} ${item.series_name}${track}`
}

export function groupByYear(items = []) {
  const groups = new Map()
  for (const item of items) {
    if (!groups.has(item.year)) groups.set(item.year, [])
    groups.get(item.year).push(item)
  }
  return [...groups.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([year, papers]) => ({ year, papers }))
}

export function regionSummary(regions = [], limit = 6) {
  const names = regions.map((r) => r.name)
  if (!names.length) return ''
  if (names.length <= limit) return names.join('、')
  return `${names.slice(0, limit).join('、')} 等 ${names.length} 地`
}

export function answerState(item) {
  const answer = item?.answer || {}
  if ((item?.answer_available ?? answer.available) !== true) {
    return { available: false, label: '暂无答案', tooltip: '暂无可用的配套答案/解析，试卷仍可下载使用' }
  }
  if (answer.status === 'source_paired') {
    return {
      available: true,
      label: answer.kind === 'final_answers' ? '含配套答案' : '含配套解析',
      tooltip: `${answer.note || '来源配套答案/解析。'} 未独立逐题核验数学答案，非官方答案。`,
    }
  }
  if (answer.status === 'verified') {
    return { available: true, label: '含参考答案', tooltip: answer.note || '非官方参考答案，经多个独立来源核对并独立复核。' }
  }
  if (answer.status === 'generated') {
    return {
      available: true,
      label: '含 AI 参考答案',
      tooltip: `${answer.note || 'AI 解答生成的参考答案。'} 非官方答案，未经人工逐题审核，使用前请核对。`,
    }
  }
  // Compatibility with older list payloads must not invent their check scope.
  return { available: true, label: '含答案/解析', tooltip: '非官方参考资料，具体性质与检查范围请查看详情。' }
}

export function editionLabel(item) {
  return item?.edition?.label || (item?.verification?.status === 'resource_checked' ? '网络原版 PDF' : '版本未说明')
}

export function verificationState(item) {
  const verification = item?.verification || {}
  if (verification.status === 'resource_checked') {
    return {
      label: '资料检查',
      description: `${verification.summary || '身份、文件角色、全页机械渲染及分层视觉抽样已检查。'} 未独立逐题核验数学内容，不代表官方认证。`,
    }
  }
  if (verification.status === 'verified') {
    return { label: '来源核对与独立复核', description: verification.summary || '逐题来源核对并经独立复核；不代表官方认证。' }
  }
  return { label: '检查范围未说明', description: verification.summary || '未提供检查范围说明。' }
}

export function filePageLabel(meta = {}, legacy = false) {
  const pages = meta.pages
  if (!Number.isInteger(pages) || pages <= 0) return ''
  const dimensions = meta.page_dimensions_mm
  let size = '尺寸未登记'
  if (Array.isArray(dimensions) && dimensions.length === pages && dimensions.every(d =>
    typeof d?.width === 'number' && Number.isFinite(d.width) && d.width > 0 &&
    typeof d?.height === 'number' && Number.isFinite(d.height) && d.height > 0)) {
    const isA4 = dimensions.every(d => {
      const [short, long] = [d.width, d.height].sort((a, b) => a - b)
      return Math.abs(short - 210) <= 0.6 && Math.abs(long - 297) <= 0.6
    })
    const sizes = [...new Set(dimensions.map(d => `${Number(d.width.toFixed(1))} × ${Number(d.height.toFixed(1))} mm`))]
    size = isA4 ? 'A4' : sizes.length === 1 ? sizes[0] : `混合尺寸（${sizes.join('；')}）`
  } else if (legacy && typeof meta.page_size === 'string' && meta.page_size.trim()) {
    size = meta.page_size
  }
  return `${size} 共 ${pages} 页`
}

export function studentPageLabel(item) {
  return filePageLabel(item?.student || { pages: item?.pages }, item?.verification?.status === 'verified')
}

export function printNote(item) {
  return item?.student?.print_note || (item?.verification?.status === 'resource_checked'
    ? '保留来源 PDF 版式及原有水印；打印时按实际尺寸选择纸张或缩放。' : '')
}

export function downloadName(item, kind) {
  const track = item?.track_name ? `（${item.track_name}）` : ''
  const spring = item?.session === 'spring' ? '春季' : ''
  const label = kind === 'answer' ? '参考答案' : '试卷'
  const stem = `${item?.year ?? ''}${spring}高考数学${item?.series_name ?? ''}${track}_${label}`
  return `${stem.replace(UNSAFE_FILENAME, '_')}.pdf`
}

// One view state, so loading/failure/empty are never shown together.
export function listViewState({ loading, error, data, mode, region }) {
  if (loading) return 'loading'
  if (error) return 'error'
  if (!data || !Array.isArray(data.items)) return 'loading'
  if (data.stage_supported === false) return 'stage'
  if (!data.facets?.years?.length) return 'library-empty'
  if (needsRegionChoice(mode, region)) return 'choose-region'
  if (!data.items.length) return 'filtered-empty'
  return 'results'
}
