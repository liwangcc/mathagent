export const EDUCATION_STAGES = Object.freeze([
  { value: 'primary', label: '小学数学', description: '数与运算 · 图形与几何 · 统计与概率' },
  { value: 'junior', label: '初中数学', description: '数与代数 · 图形与几何 · 统计与概率' },
  { value: 'senior', label: '高中数学', description: '函数与代数 · 几何与向量 · 概率与统计' },
])

export const HISTORY_STAGE_OPTIONS = Object.freeze([
  { value: '', label: '全部历史' }, ...EDUCATION_STAGES,
  { value: 'legacy', label: '历史未标学段' },
])

export function historyStageLabel(value) {
  return HISTORY_STAGE_OPTIONS.find(item => item.value === (value || 'legacy'))?.label || '历史未标学段'
}

export function normalizeEducationStage(value) {
  return EDUCATION_STAGES.some(item => item.value === value) ? value : ''
}

export function hasCompleteEducationProfile(user) {
  return !!normalizeEducationStage(user?.education_stage) && user?.profile_complete === true
}

export function safeProfileRedirect(value, fallback = '/') {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return fallback
  // Reject escaped separators and control characters before Vue Router decodes them.
  if (/[\\\u0000-\u0020]/.test(value) || /%(?:2f|5c|0[0-9a-f]|1[0-9a-f])/i.test(value)) return fallback
  const path = value.split(/[?#]/, 1)[0]
  if (['/login', '/profile'].includes(path.replace(/\/$/, ''))) return fallback
  return value
}

export function educationProfileRedirect(to, auth) {
  if (!auth.token || to.meta?.public || to.name === 'profile') return null
  if (to.meta?.admin && auth.user?.role === 'admin') return null
  if (hasCompleteEducationProfile(auth.user)) return null
  return {
    name: 'profile',
    query: { setup: 'education-stage', redirect: safeProfileRedirect(to.fullPath) },
  }
}
