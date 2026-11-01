const studentNumberCollator = new Intl.Collator('zh-CN', {
  numeric: true,
  sensitivity: 'base',
})

/**
 * Sort student numbers naturally instead of lexicographically: 2 < 11.
 * Student numbers can include leading zeros or letters, so numeric collation is
 * safer than Number(value), which would lose those identifiers.
 */
export function compareStudentNumbers(left, right) {
  return studentNumberCollator.compare(String(left ?? ''), String(right ?? ''))
}