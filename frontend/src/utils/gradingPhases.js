// Pipeline phases shown on the grading progress cards, as on the showcase site's
// grading specimen. Anonymous batches never match identities or archive into
// student records, so they skip those two phases.
const IDENTIFIED_PHASES = ['识别', '身份', '判分', '复核', '归档']
const ANONYMOUS_PHASES = ['识别', '判分', '复核']

export function gradingPhases({
  anonymous = false,
  identityPending = false,
  grading = false,
  reviewing = false,
  archived = false,
} = {}) {
  const labels = anonymous ? ANONYMOUS_PHASES : IDENTIFIED_PHASES
  let current = '识别'
  if (archived && !anonymous) current = '归档'
  else if (reviewing) current = '复核'
  else if (grading) current = '判分'
  else if (identityPending && !anonymous) current = '身份'
  return { labels, current: labels.indexOf(current) }
}
