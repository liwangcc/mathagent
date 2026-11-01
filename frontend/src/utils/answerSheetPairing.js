// Front/back pairing by upload path for student answer sheets. Mirrors the
// first two rules of services/answer_sheet_page_pairing.py (one folder per
// student; side-marked file names). The roster-name rule needs the server's
// collection resolver, and the server re-derives every pair itself; this only
// orders and labels the list before upload.

const OCR_READY_RE = /\.ocr-ready-v1\.jpg$/i
const EXTENSION_RE = /\.[A-Za-z0-9]{1,8}$/
const SEPARATORS = '\\s_\\-.·()\\[\\]【】'
const SIDE_RE = new RegExp(`^(.*?)([${SEPARATORS}]*)(正面|反面|背面|正|反|背|front|back)[${SEPARATORS}]*$`, 'u')
const EDGE_SEPARATORS_RE = new RegExp(`^[${SEPARATORS}]+|[${SEPARATORS}]+$`, 'gu')
const SIDES = {
  正面: 'front', 正: 'front', front: 'front',
  反面: 'back', 反: 'back', 背面: 'back', 背: 'back', back: 'back',
}
const naturalOrder = (left, right) => left.localeCompare(right, 'zh-CN', { numeric: true })

export function parseSideFilename(filename) {
  const name = String(filename || '').replace(/\\/g, '/').split('/').pop()
  const stem = name.replace(OCR_READY_RE, '').replace(EXTENSION_RE, '').normalize('NFKC').toLowerCase()
  const match = stem.match(SIDE_RE)
  if (!match) return null
  const [, rawBase, separator, word] = match
  // English side words need a non-letter before them ("feedback" is no side).
  if (/^[a-z]/.test(word) && !separator && /[a-z]$/.test(rawBase)) return null
  const base = rawBase.replace(/\s+/gu, '').replace(EDGE_SEPARATORS_RE, '')
  return base ? { base, side: SIDES[word] } : null
}

export function sourceDirectory(path) {
  const parts = String(path || '').replace(/\\/g, '/').split('/')
  return parts.length >= 2 ? parts.slice(0, -1).join('/') : ''
}

export function pairFilesBySide(files, getName = (file) => file?.name) {
  if (!files?.length || files.length % 2) return null
  const groups = new Map()
  for (const file of files) {
    const parsed = parseSideFilename(getName(file))
    if (!parsed) return null
    const slot = groups.get(parsed.base) || {}
    if (slot[parsed.side]) return null
    slot[parsed.side] = file
    groups.set(parsed.base, slot)
  }
  const bases = [...groups.keys()]
  if (bases.some((base) => !groups.get(base).front || !groups.get(base).back)) return null
  return bases.sort(naturalOrder).flatMap((base) => [groups.get(base).front, groups.get(base).back])
}

// Every file in a folder and every folder holding exactly two files; front and
// back inside a folder are decided by the template page type on the server.
export function pairFilesByFolder(files, getPath) {
  if (!files?.length) return null
  const groups = new Map()
  for (const file of files) {
    const path = getPath(file)
    const directory = sourceDirectory(path)
    if (!directory) return null
    groups.set(directory, [...(groups.get(directory) || []), { file, path }])
  }
  const directories = [...groups.keys()]
  if (directories.some((directory) => groups.get(directory).length !== 2)) return null
  return directories.sort(naturalOrder).flatMap((directory) => (
    groups.get(directory).sort((left, right) => naturalOrder(left.path, right.path)).map((item) => item.file)
  ))
}

const PAIRING_PROBLEMS = {
  missing_back: '缺少配对的背面',
  template_unknown: '模板页数无法建立正反面页对',
  page_type_mismatch: '正反面与模板页型或文件名标记不一致',
  front_identity_missing: '正面身份未确认，背面暂不能沿用',
  identity_missing: '同组两页都没有可确认的身份',
  identity_conflict: '同组两页身份不一致，请分别确认',
}
const DERIVED_STATUSES = new Set(['ordered_pair_inherited', 'path_paired'])
const PATH_CLUES = { filename: '文件名', folder: '文件夹名', 'filename+folder': '文件名与文件夹名', 'folder+filename': '文件名与文件夹名' }

export function studentPageIdentityHint(page, pages = []) {
  const identity = page?.identity || {}
  const problem = PAIRING_PROBLEMS[page?.pairing?.status]
  const name = identity.student_name || '未命名学生'
  if (problem && DERIVED_STATUSES.has(identity.status)) {
    return { text: `身份：待老师确认；${problem}`, type: 'danger' }
  }
  const source = pages.find((item) => item?.paper_id === identity.source_paper_id)
  const sourceName = source?.filename ? `（${source.source_relative_path || source.filename}）` : ''
  let text = '身份：待老师确认'
  let type = 'warning'
  if (identity.status === 'recognized') {
    text = `身份：${name}（本页识别）`
    type = 'success'
  } else if (identity.status === 'teacher_confirmed') {
    text = `身份：${name}（老师确认）`
    type = 'success'
  } else if (identity.status === 'ordered_pair_inherited') {
    text = `身份：${name}（按上传顺序沿用上一页正面${sourceName}，请核对是否同一学生）`
  } else if (identity.status === 'path_paired') {
    text = identity.source === 'path'
      ? `身份：${name}（${PATH_CLUES[identity.clue_source] || '文件名或文件夹名'}确认）`
      : `身份：${name}（沿用同组另一面${sourceName}）`
    type = 'info'
  }
  return problem ? { text: `${text}；${problem}`, type: 'danger' } : { text, type }
}
