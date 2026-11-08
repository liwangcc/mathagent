/**
 * How the roster is shown: the risk filter and the search box, each student's risk level (riskMeta), the
 * risk groups and the filtered table, and the rate and sort helpers the table's columns use.
 *
 * Moved from views/ClassTable.vue; the page passes its earlier parts in ctx (see ClassTable.vue).
 */
import { computed, ref } from 'vue'
import { compareStudentNumbers } from '@/utils/studentSort'

export function useStudentRiskView(ctx) {
  const {
    students,
  } = ctx

  const keyword = ref('')
  const riskFilter = ref('all')

  const riskFilterOptions = [
    { label: '全部', value: 'all' },
    { label: '观察模式', value: 'observation' },
    { label: '重点关注', value: 'attention' },
    { label: '持续跟进', value: 'watch' },
    { label: '表现良好', value: 'stable' },
    { label: '暂无数据', value: 'no_data' },
  ]

  const studentsWithRisk = computed(() =>
    students.value.map((student) => ({
      ...student,
      _risk: riskMeta(student),
    }))
  )

  const filteredStudents = computed(() => {
    const q = keyword.value.trim().toLowerCase()
    return studentsWithRisk.value.filter((s) => {
      const matchesRisk = riskFilter.value === 'all' || s._risk.key === riskFilter.value
      const matchesKeyword = !q ||
        String(s.name || '').toLowerCase().includes(q) ||
        String(s.student_no || '').toLowerCase().includes(q) ||
        String(s.exam_no || '').toLowerCase().includes(q)
      return matchesRisk && matchesKeyword
    })
  })

  const riskGroups = computed(() => {
    const groups = [
      {
        key: 'observation',
        label: '观察模式',
        description: '继续批阅，但不参与正式风险与统计判断',
        className: 'risk-observation',
      },
      {
        key: 'attention',
        label: '重点关注',
        description: '错题率高或未纠正薄弱点较多',
        className: 'risk-danger',
      },
      {
        key: 'watch',
        label: '持续跟进',
        description: '有薄弱点，但整体仍可控',
        className: 'risk-warn',
      },
      {
        key: 'stable',
        label: '表现良好',
        description: '纠错推进较好，近期风险较低',
        className: 'risk-good',
      },
      {
        key: 'no_data',
        label: '暂无数据',
        description: '还没有已归档的批阅，暂不判断风险',
        className: 'risk-neutral',
      },
    ]
    return groups.map((group) => {
      const groupStudents = studentsWithRisk.value.filter((student) => student._risk.key === group.key)
      const names = groupStudents.slice(0, 3).map((student) => student.name).filter(Boolean)
      return {
        ...group,
        students: groupStudents,
        preview: names.length ? names.join('、') : '暂无学生',
      }
    }).filter((group) => group.key !== 'no_data' || group.students.length)
  })

  const tableSubtitle = computed(() => {
    const active = riskFilterOptions.find((item) => item.value === riskFilter.value)?.label || '全部'
    return `${active} · 最近批改窗口、纠错率和薄弱点`
  })

  function applyRiskFilter(key) {
    riskFilter.value = riskFilter.value === key ? 'all' : key
  }

  function toPercent(value) {
    const n = Number(value || 0)
    return Math.max(0, Math.min(100, Math.round(n * 1000) / 10))
  }

  function formatRate(value) {
    return `${toPercent(value).toFixed(1)}%`
  }

  function rateColor(value, badHigh) {
    const n = Number(value || 0)
    if (badHigh) {
      if (n >= 0.35) return 'var(--danger)'
      if (n >= 0.18) return 'var(--warning)'
      return 'var(--success)'
    }
    if (n >= 0.7) return 'var(--success)'
    if (n >= 0.4) return 'var(--warning)'
    return 'var(--danger)'
  }

  function riskMeta(row) {
    const riskMap = {
      observation: { key: 'observation', label: '观察模式', tagType: 'info' },
      attention: { key: 'attention', label: '重点关注', tagType: 'danger' },
      watch: { key: 'watch', label: '持续跟进', tagType: 'warning' },
      stable: { key: 'stable', label: '表现良好', tagType: 'success' },
      no_data: { key: 'no_data', label: '暂无数据', tagType: 'info' },
    }
    return riskMap[row.risk_level] || riskMap.stable
  }

  function sortByName(a, b) {
    return String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN')
  }

  function sortByStudentNo(a, b) {
    return compareStudentNumbers(a.student_no, b.student_no)
  }

  function sortByWrongRate(a, b) {
    return Number(a.wrong_rate || 0) - Number(b.wrong_rate || 0)
  }

  function sortByCorrectionRate(a, b) {
    return Number(a.correction_rate || 0) - Number(b.correction_rate || 0)
  }

  function sortByLatestScore(a, b) {
    return Number(a.latest_exam_score ?? -1) - Number(b.latest_exam_score ?? -1)
  }

  return {
    keyword, riskFilter, riskFilterOptions, studentsWithRisk, filteredStudents, riskGroups,
    tableSubtitle, applyRiskFilter, toPercent, formatRate, rateColor, riskMeta, sortByName,
    sortByStudentNo, sortByWrongRate, sortByCorrectionRate, sortByLatestScore,
  }
}
