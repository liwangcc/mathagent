function normalizeCandidate(candidate) {
  const studentId = Number(candidate?.student_id ?? candidate?.id)
  if (!Number.isInteger(studentId) || studentId <= 0) return null
  return {
    student_id: studentId,
    name: String(candidate?.name || '').trim(),
    student_no: String(candidate?.student_no || '').trim(),
    class_id: candidate?.class_id ?? null,
  }
}

export function mergeIdentityCandidates(preferred = [], roster = []) {
  const byId = new Map()
  for (const source of [preferred, roster]) {
    for (const rawCandidate of Array.isArray(source) ? source : []) {
      const candidate = normalizeCandidate(rawCandidate)
      if (candidate && !byId.has(candidate.student_id)) {
        byId.set(candidate.student_id, candidate)
      }
    }
  }
  return [...byId.values()]
}

export function buildIdentityResolutionItems(papers = []) {
  const items = (Array.isArray(papers) ? papers : [])
    .filter((paper) => {
      if (!paper || (paper.error && !paper.requires_manual_review)) return false
      const identity = paper.identity || {}
      return identity.status !== 'matched' || !identity.student_id
    })
    .map((paper) => {
      const identity = paper.identity || {}
      const saved = paper.identity_resolution
      const savedMatchesPaper = saved && typeof saved === 'object'
        && (!saved.paper_id || String(saved.paper_id) === String(paper.paper_id))
      const savedActionIsValid = savedMatchesPaper && ['existing', 'create'].includes(saved.action)
      const savedAction = savedActionIsValid ? saved.action : 'existing'
      const savedStudentId = Number(savedActionIsValid ? saved.student_id : null)
      const names = [...new Set((identity.name_sources || []).flatMap(source => source.names || []))]
      const defaultName = identity.ocr_name || paper.student_name || (names.length === 1 ? names[0] : '')
      const defaultStudentNo = identity.ocr_student_no || paper.student_no || ''
      return {
        paper_id: paper.paper_id,
        filename: paper.filename || '',
        source_relative_path: identity.source_relative_path || '',
        clue_source: identity.clue_source || paper.identity_clue_source || '',
        confirmation_group: identity.reason === 'name_ambiguous' ? identity.confirmation_group || '' : '',
        // Opt-in inheritance: a blank page follows its anchor's decision.
        inherit_anchor: String(identity.inherit_anchor?.paper_id || ''),
        inherit_blocked: String(identity.inherit_blocked || ''),
        inherited_page_count: 0,
        has_saved_resolution: Boolean(savedActionIsValid),
        pages: [{ paper_id: paper.paper_id, filename: paper.filename || '', source_relative_path: identity.source_relative_path || '' }],
        ocr_name: defaultName,
        ocr_student_no: defaultStudentNo,
        reason: identity.reason || '',
        candidates: mergeIdentityCandidates(identity.candidates),
        image_url: '',
        action: savedAction,
        student_id: savedAction === 'existing' && Number.isInteger(savedStudentId) && savedStudentId > 0
          ? savedStudentId
          : null,
        confirmed_name: savedActionIsValid && savedAction === 'create'
          ? String(saved.confirmed_name ?? defaultName).trim()
          : defaultName,
        confirmed_student_no: savedActionIsValid && savedAction === 'create'
          ? String(saved.confirmed_student_no ?? defaultStudentNo).trim()
          : defaultStudentNo,
      }
    })
  const byPaper = new Map(items.map(item => [String(item.paper_id), item]))
  const followers = new Map()
  for (const item of items) {
    const anchor = item.inherit_anchor && byPaper.get(item.inherit_anchor)
    // A restored per-page decision that differs from the anchor's stays separate.
    if (!anchor || anchor === item || anchor.inherit_anchor) continue
    if (item.has_saved_resolution && decisionKey(item) !== decisionKey(anchor)) continue
    followers.set(String(item.paper_id), String(anchor.paper_id))
  }
  const groups = new Map()
  const headOf = new Map()
  const result = []
  for (const item of items) {
    if (followers.has(String(item.paper_id))) continue
    // Keep conflicting saved decisions and per-page create actions separate.
    // Only a roster student ID can safely join pages without a student number.
    const key = item.action === 'existing' && item.confirmation_group && JSON.stringify([
      item.confirmation_group, item.candidates.map(candidate => candidate.student_id),
      item.action, item.student_id, item.confirmed_name, item.confirmed_student_no,
    ])
    const group = key && groups.get(key)
    if (group) group.pages.push(...item.pages)
    else {
      result.push(item)
      if (key) groups.set(key, item)
    }
    headOf.set(String(item.paper_id), group || item)
  }
  for (const item of items) {
    const head = headOf.get(followers.get(String(item.paper_id)))
    if (!head) continue
    head.pages.push(...item.pages)
    head.inherited_page_count += 1
  }
  return result
}

function decisionKey(item) {
  return JSON.stringify([item.action, item.student_id, item.confirmed_name, item.confirmed_student_no])
}

export function mergeRosterIntoIdentityItems(items = [], roster = []) {
  return items.map((item) => ({
    ...item,
    candidates: mergeIdentityCandidates(item.candidates, roster),
  }))
}

export function identityResolutionsReady(items = []) {
  return items.length > 0 && items.every((item) => {
    if (item.action === 'existing') {
      const studentId = Number(item.student_id)
      return Number.isInteger(studentId) && studentId > 0
    }
    if (item.action === 'create') {
      return (item.pages?.length || 1) === 1 && Boolean(String(item.confirmed_name || '').trim())
    }
    return false
  })
}

export function serializeIdentityResolutions(items = []) {
  if (items.some(item => item.action === 'create' && item.pages?.length > 1)) {
    throw new Error('多页图片请先加入班级名单，再选择同一位学生，避免分开建档。')
  }
  return items.flatMap((item) => (item.pages || [{ paper_id: item.paper_id }]).map(page => ({
    paper_id: page.paper_id,
    action: item.action,
    student_id: item.action === 'existing' ? Number(item.student_id) : null,
    confirmed_name: item.action === 'create' ? String(item.confirmed_name || '').trim() : '',
    confirmed_student_no: item.action === 'create' ? String(item.confirmed_student_no || '').trim() : '',
  })))
}
