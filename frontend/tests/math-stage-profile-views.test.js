import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, reactive, ref } from 'vue'
import { parse } from '@vue/compiler-sfc'
import { normalizeEducationStage, historyStageLabel } from '../src/utils/educationStage.js'

const pages = ['ClassTable', 'StudentDetail', 'Stats', 'Reports']
const sources = Object.fromEntries(pages.map(page => [page, readFileSync(new URL(`../src/views/${page}.vue`, import.meta.url), 'utf8')]))
const exposed = {
  ClassTable: 'loadStudents, openHeatmap, students, coverage, heatmapTree, activeClassId, loading, recognizeImage, studentImageFile, recognizedStudents, importExamScores, examFile',
  StudentDetail: 'load, detail, generateReport, generateCorrectionPaper, reporting, loading',
  Stats: 'loadStats, stats, studentId, metric, loading',
  Reports: 'load, loadClassStudents, loadLetterShares, report, classStudents, letterShares, selectedStudentId, generating, generateClassReport, generateStudentReport, generateBatchReport, regenerateBatchReport, loading',
}
const defer = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function setup(page, stage = 'junior', overrides = {}) {
  const calls = [], downloads = [], messages = [], watches = []
  const auth = reactive({ token: 'mock-token', user: { id: 1, education_stage: stage }, currentClassId: 1, classes: [{ id: 1, class_name: '班级' }], fetchClasses: async () => {}, switchClass: async () => {} })
  const route = reactive({ params: { classId: '1', studentId: '1', id: '1' }, query: { education_stage: stage === 'junior' ? 'senior' : 'junior' }, path: '/students' })
  const reply = (name, result = {}) => async (...args) => { calls.push({ name, args }); return result }
  const api = {
    student: { list: reply('roster', { students: [] }), listAll: reply('roster', { students: [] }), classDetail: reply('detail'), report: reply('student-report'), ...overrides.student },
    stats: { student: reply('stats'), ...overrides.stats },
    report: { summary: reply('summary'), ...overrides.report },
    classroom: { students: reply('students'), heatmap: reply('heatmap'), letterShares: reply('shares'), report: reply('class-report'), reportBatch: reply('batch-report', { mode: 'share', shares: [] }), ...overrides.classroom },
    file: { downloadUrl: async (...args) => downloads.push(args), downloadGenerated: async (...args) => downloads.push(args) },
    exercise: { generate: reply('exercise', {}) },
  }
  const bindings = {
    ref, reactive, computed, onMounted: () => {}, watch: (...args) => watches.push(args),
    useRoute: () => route, useRouter: () => ({ push() {}, replace() {} }), useAuthStore: () => auth,
    ElMessage: Object.fromEntries(['error', 'success', 'warning', 'info'].map(key => [key, value => messages.push(value)])),
    ElMessageBox: { alert: async () => {}, confirm: async () => {} }, api, normalizeEducationStage, historyStageLabel,
    compareStudentNumbers: () => 0, knowledgeTopicStudents: () => [],
  }
  const script = parse(sources[page]).descriptor.scriptSetup.content.replace(/^import[\s\S]*?from ['"][^'"]+['"]\r?\n/gm, '')
  const view = new Function(...Object.keys(bindings), `${script}\nreturn { educationStage, profileScope, reloadProfileScope, ${exposed[page]} }`)(...Object.values(bindings))
  return { view, auth, route, calls, downloads, messages, watches }
}

for (const page of pages) {
  test(`${page}: profile is sole stage source, query cannot override, no selector`, async () => {
    assert.doesNotMatch(sources[page], /v-model="educationStage"|HISTORY_STAGE_OPTIONS|route\.query\.education_stage/)
    assert.match(sources[page], /跟随个人资料/)
    for (const stage of ['primary', 'junior', 'senior']) {
      const { view, calls, watches } = setup(page, stage)
      assert.equal(view.educationStage.value, stage)
      assert.ok(watches.some(([source, callback, options]) => callback === view.reloadProfileScope && options?.flush === 'sync'))
      await view.reloadProfileScope()
      const scoped = calls.filter(call => call.name !== 'roster')
      assert.ok(scoped.length > 0)
      for (const call of scoped) assert.equal((call.args[1] || call.args[0]).education_stage, stage, call.name)
    }
  })

  test(`${page}: profile missing does not issue unscoped history requests`, async () => {
    const { view, calls } = setup(page, '')
    await view.reloadProfileScope()
    assert.equal(calls.length, 0)
  })
}

const races = {
  ClassTable: { group: 'classroom', method: 'students', load: 'loadStudents', data: marker => ({ students: [{ name: marker }], coverage: { class_size: 1 } }), value: view => view.students.value[0]?.name, init: view => { view.activeClassId.value = 1 } },
  StudentDetail: { group: 'student', method: 'classDetail', load: 'load', data: marker => ({ student: { name: marker } }), value: view => view.detail.student.name },
  Stats: { group: 'stats', method: 'student', load: 'loadStats', data: marker => ({ student: { name: marker }, total_questions: 1 }), value: view => view.stats.value?.student?.name, init: view => { view.studentId.value = 1 } },
  Reports: { group: 'report', method: 'summary', load: 'load', data: marker => ({ summary: marker, total: 1 }), value: view => view.report.summary },
}
for (const [page, scenario] of Object.entries(races)) {
  for (const change of ['stage', 'account']) {
    test(`${page}: ${change} change clears old data and ignores stale success/failure`, async () => {
      for (const rejected of [false, true]) {
        const old = defer(), fresh = defer()
        let requests = 0
        const { view, auth } = setup(page, 'junior', { [scenario.group]: { [scenario.method]: () => (++requests === 1 ? old.promise : fresh.promise) } })
        scenario.init?.(view)
        const first = view[scenario.load]()
        if (change === 'stage') auth.user.education_stage = 'senior'
        else { auth.user.id = 2; auth.token = 'other-mock-token' }
        const next = view.reloadProfileScope()
        assert.ok(!scenario.value(view), 'old snapshot must be cleared immediately')
        fresh.resolve(scenario.data('CURRENT'))
        await next
        if (rejected) old.reject(new Error('old request failed'))
        else old.resolve(scenario.data('STALE'))
        await first
        assert.equal(scenario.value(view), 'CURRENT')
        assert.equal(view.loading.value, false)
      }
    })
  }
}

test('report generation uses profile stage and does not download stale files', async () => {
  for (const [page, method, group, endpoint] of [
    ['StudentDetail', 'generateReport', 'student', 'report'],
    ['Reports', 'generateClassReport', 'classroom', 'report'],
    ['Reports', 'generateStudentReport', 'student', 'report'],
  ]) {
    const pending = defer(), params = []
    const { view, auth, downloads } = setup(page, 'senior', { [group]: { [endpoint]: (...args) => { params.push(args); return pending.promise } } })
    if (view.selectedStudentId) view.selectedStudentId.value = 1
    const request = view[method]('parent_letter')
    assert.equal(params[0][1].education_stage, 'senior')
    auth.user.education_stage = 'junior'
    await view.reloadProfileScope()
    pending.resolve({ download_url: '/api/files/mock' })
    await request
    assert.deepEqual(downloads, [])
  }
})

test('parent shares and heatmaps cannot return from an old account scope', async () => {
  for (const [page, endpoint, load, result, value] of [
    ['Reports', 'letterShares', 'loadLetterShares', { shares: [{ id: 99 }] }, view => view.letterShares.value],
    ['ClassTable', 'heatmap', 'openHeatmap', { tree: [{ chapter: 'old' }] }, view => view.heatmapTree.value],
  ]) {
    const pending = defer()
    let count = 0
    const { view, auth } = setup(page, 'junior', { classroom: { [endpoint]: () => (++count === 1 ? pending.promise : Promise.resolve({})) } })
    if (view.activeClassId) view.activeClassId.value = 1
    const request = view[load]()
    auth.user.id = 2
    await view.reloadProfileScope()
    pending.resolve(result)
    await request
    assert.deepEqual(value(view), [])
  }
})

test('parent letter creation follows senior profile and drops delayed old-stage links', async () => {
  const pending = defer(), params = []
  const { view, auth } = setup('Reports', 'senior', { classroom: { reportBatch: (...args) => { params.push(args); return pending.promise } } })
  const request = view.generateBatchReport(false)
  assert.equal(params[0][1].education_stage, 'senior')
  auth.user.education_stage = 'junior'
  await view.reloadProfileScope()
  // A stale response must not reach clipboard (not available in this Node harness).
  pending.resolve({ mode: 'share', shares: [{ url: '/old-secret', student_name: 'old' }] })
  await request
  assert.deepEqual(view.letterShares.value, [])
  assert.equal(view.generating.value, '')
})

test('class import callbacks cannot reveal stale account records after a profile change', async () => {
  for (const [endpoint, action, file] of [
    ['importStudentsImage', 'recognizeImage', 'studentImageFile'],
    ['importExamScores', 'importExamScores', 'examFile'],
  ]) {
    const pending = defer()
    const { view, auth, messages } = setup('ClassTable', 'senior', { classroom: { [endpoint]: () => pending.promise } })
    view.activeClassId.value = 1
    view[file].value = new Blob(['synthetic fixture'])
    const request = view[action]()
    auth.user.id = 2
    await view.reloadProfileScope()
    pending.resolve({ recognized: [{ name: 'old student' }], matched: 1, unmatched: ['old student'] })
    await request
    assert.deepEqual(view.recognizedStudents.value, [])
    assert.deepEqual(messages, [])
  }
})

test('all report generation paths retain runtime single-flight protection', async () => {
  for (const [page, action, group, endpoint] of [
    ['Reports', 'generateClassReport', 'classroom', 'report'],
    ['Reports', 'generateStudentReport', 'student', 'report'],
    ['Reports', 'generateBatchReport', 'classroom', 'reportBatch'],
    ['StudentDetail', 'generateReport', 'student', 'report'],
  ]) {
    const pending = defer()
    let count = 0
    const { view } = setup(page, 'senior', { [group]: { [endpoint]: () => { count++; return pending.promise } } })
    if (view.selectedStudentId) view.selectedStudentId.value = 1
    const request = view[action]('parent_letter')
    await view[action]('parent_letter')
    assert.equal(count, 1)
    pending.resolve({ mode: 'share', shares: [], file_id: 'synthetic-file' })
    await request
  }
})
