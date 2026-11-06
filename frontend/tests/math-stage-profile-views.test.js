import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { messages } from './helpers/elementPlus.js'
import {
  HOLD,
  bodyOf,
  button,
  captureDownloads,
  choose,
  chooseCommand,
  flush,
  holdingBackend,
  installAnimationFrames,
  installClipboard,
  isLoading,
  mountView,
  route,
  signIn,
  stageOf,
  teacher,
  tokenOf,
} from './helpers/viewsStageReports.js'

// The four analytics pages take their teaching stage from the teacher's
// profile only, reload the moment the profile (or the signed-in account)
// changes, and never let a response from the previous profile reach the page.

const LABELS = { primary: '小学数学', junior: '初中数学', senior: '高中数学' }
const STAGE_CHOICES = new Set([...Object.keys(LABELS), ...Object.values(LABELS), '全部历史', '历史未标学段', 'legacy'])

const pages = {
  ClassTable: {
    load: () => import('../src/views/ClassTable.vue?sfc'),
    path: '/class/1/students',
    data: 'get /class/1/students',
    reply: marker => ({ students: [{ id: 11, name: marker, student_no: '01', risk_level: 'stable' }], coverage: { class_size: 1, effective_students: 1, observation_students: 0 } }),
    shown: page => page.root.byClass('class-table')?.text ?? '',
    loading: page => isLoading(page.root.byClass('class-table')),
    cached: setup => setup.students.map(student => student.name).join(),
    // The class's knowledge-point heatmap, opened from the more menu.
    act: page => chooseCommand(page, '查看知识点热力图'),
    acts: ['get /class/1/heatmap'],
  },
  StudentDetail: {
    load: () => import('../src/views/StudentDetail.vue?sfc'),
    path: '/student/1/detail',
    data: 'get /student/1/detail',
    reply: marker => ({ student: { id: 1, name: marker } }),
    shown: page => page.root.byTag('h2')[0]?.text ?? '',
    loading: page => isLoading(page.root.byClass('detail-grid')),
    cached: setup => setup.detail.student.name ?? '',
    // The student's report and the letter to the parents.
    act: async (page) => {
      await chooseCommand(page, '学生个人报告')
      await flush()
      await chooseCommand(page, '致家长信')
    },
    acts: ['post /student/1/report'],
  },
  Stats: {
    load: () => import('../src/views/Stats.vue?sfc'),
    path: '/stats/1',
    data: 'get /stats/1',
    reply: marker => ({ student: { id: 1, name: marker }, total_questions: 1, total_gradings: 1 }),
    shown: page => page.root.byTag('h1')[0]?.text ?? '',
    loading: page => Boolean(page.root.byClass('stats-loading')),
    cached: setup => setup.stats?.student?.name ?? '',
    // Another student picked in the student picker.
    act: page => choose(page.root.find(el => el.tagName === 'el-select' && el.getAttribute('aria-label') === '选择学生'), 2),
    acts: ['get /stats/2'],
  },
  Reports: {
    load: () => import('../src/views/Reports.vue?sfc'),
    path: '/reports',
    data: 'post /reports/summary',
    reply: marker => ({ summary: marker, total: 1 }),
    shown: page => page.root.byClass('summary-text')?.text ?? '',
    loading: page => isLoading(page.root.byClass('report-body')),
    cached: setup => setup.report.summary,
    // Another period, the class report, the parent-letter links, and a picked
    // student's report and letter.
    act: async (page) => {
      await choose(page.root.byClass('ink-segmented'), 'month')
      await flush()
      await button(page, '生成并下载').dispatch('click')
      await flush()
      await button(page, '准备并复制链接').dispatch('click')
      await flush()
      await choose(page.root.find(el => el.tagName === 'el-select' && el.getAttribute('placeholder') === '选择学生'), 5)
      await flush()
      await chooseCommand(page, '学生报告')
      await flush()
      await chooseCommand(page, '致家长信')
    },
    acts: ['post /reports/summary', 'post /class/1/report', 'post /class/1/report/batch', 'get /class/1/letter-shares', 'post /student/5/report'],
  },
}

// Requests that are not history or analytics: the roster picker and the class list.
const UNSCOPED = new Set(['get /student/list', 'get /auth/classes'])

function defaults(config) {
  const at = route(config)
  if (at === 'get /student/list') return { data: { items: [], total: 0 } }
  if (at === 'get /auth/classes') return { data: { classes: [{ id: 1, class_name: '初二（1）班', student_count: 1 }], current_class_id: 1 } }
  if (at === 'get /class/1/letter-shares') return { data: { shares: [] } }
  if (at === 'post /class/1/report/batch') return { data: { mode: 'share', shares: [] } }
  return { data: {} }
}

async function open(t, name, { stage = 'junior', query = '', reply = defaults } = {}) {
  installAnimationFrames(t)
  const signedIn = await signIn({ stage })
  const backend = holdingBackend(signedIn.client, reply)
  const { default: View } = await pages[name].load()
  const page = await mountView(t, View, { path: `${pages[name].path}${query}`, pinia: signedIn.pinia })
  return { ...signedIn, backend, page }
}

// The ways the profile scope changes under a mounted page: the profile's stage
// is saved (auth.fetchMe() then holds the new user), or another teacher signs in.
const changes = {
  stage: {
    apply: auth => { auth.user = { ...auth.user, education_stage: 'senior' } },
    isNew: config => stageOf(config) === 'senior',
  },
  account: {
    apply: auth => auth.login('second-teacher-token', teacher({ id: 2, education_stage: 'junior' })),
    isNew: config => tokenOf(config) === 'second-teacher-token',
  },
}

// Elements that would let the teacher pick a stage other than the profile's.
function stageSelectors(page) {
  return page.root.findAll((el) => {
    const offered = [el.getAttribute('label'), el.props.label, el.props.value, el.props.modelValue,
      ...(['el-option', 'option', 'el-radio', 'el-radio-button'].includes(el.tagName) ? [el.text] : []),
      ...(Array.isArray(el.props.options) ? el.props.options.flatMap(option => [option?.label, option?.value]) : [])]
    return offered.some(value => STAGE_CHOICES.has(String(value ?? '')))
  })
}

for (const [name, spec] of Object.entries(pages)) {
  test(`${name}: profile is sole stage source, query cannot override, no selector`, async (t) => {
    for (const stage of ['primary', 'junior', 'senior']) {
      // The profile changes from `stage` to `next` below; the address asks
      // for the third stage throughout.
      const next = stage === 'senior' ? 'primary' : 'senior'
      const other = Object.keys(LABELS).find(value => value !== stage && value !== next)
      const { client, auth, page } = await open(t, name, {
        stage,
        query: `?education_stage=${other}`,
        reply: config => (route(config) === spec.data ? { data: spec.reply('甲同学') } : defaults(config)),
      })
      assert.match(page.root.byClass('profile-stage-label').text, new RegExp(`${LABELS[stage]} · 跟随个人资料`))
      assert.deepEqual(stageSelectors(page).map(el => el.tagName), [], 'the page offers no stage of its own')
      const scoped = client.requests.filter(config => !UNSCOPED.has(route(config)))
      assert.ok(scoped.some(config => route(config) === spec.data), `${name} loads its ${stage} data`)
      for (const config of scoped) assert.equal(stageOf(config), stage, route(config))
      assert.match(spec.shown(page), /甲同学/)

      // Saving another stage in the profile reloads the page with it at once.
      const before = client.requests.length
      auth.user = { ...auth.user, education_stage: next }
      assert.equal(spec.cached(page.setup), '', 'the previous stage\'s data is dropped in the same tick')
      await flush()
      const reloaded = client.requests.slice(before).filter(config => !UNSCOPED.has(route(config)))
      assert.ok(reloaded.some(config => route(config) === spec.data), `${name} reloads for ${next}`)
      for (const config of reloaded) assert.equal(stageOf(config), next, route(config))
      assert.match(page.root.byClass('profile-stage-label').text, new RegExp(`${LABELS[next]} · 跟随个人资料`))

      // What the teacher then does on the page asks for the profile's stage
      // too, while the address still names another one.
      assert.equal(page.router.currentRoute.value.query.education_stage, other)
      const beforeActions = client.requests.length
      await spec.act(page)
      await flush()
      const acted = client.requests.slice(beforeActions).filter(config => !UNSCOPED.has(route(config)))
      for (const expected of spec.acts) assert.ok(acted.some(config => route(config) === expected), `${name}: ${expected}`)
      for (const config of acted) assert.equal(stageOf(config), next, `${name}: ${route(config)}`)
      page.unmount()
    }
  })

  test(`${name}: profile missing does not issue unscoped history requests`, async (t) => {
    const { client, page } = await open(t, name, { stage: '' })
    await flush()
    assert.deepEqual(client.requests.map(route), [])
    assert.match(page.root.byClass('profile-stage-label').text, /历史未标学段 · 跟随个人资料/)
  })
}

for (const [name, spec] of Object.entries(pages)) {
  for (const [change, { apply, isNew }] of Object.entries(changes)) {
    test(`${name}: ${change} change clears old data and ignores stale success/failure`, async (t) => {
      // What the previous scope showed disappears at once, before the new
      // scope's data arrives.
      {
        const { auth, backend, page } = await open(t, name, {
          reply: config => (route(config) !== spec.data ? defaults(config) : isNew(config) ? HOLD : { data: spec.reply('OLD') }),
        })
        assert.match(spec.shown(page), /OLD/)
        apply(auth)
        assert.equal(spec.cached(page.setup), '', 'old snapshot must be cleared immediately')
        await flush()
        assert.doesNotMatch(spec.shown(page), /OLD/)
        assert.ok(backend.held.length > 0, 'the new scope is being loaded')
        for (const entry of backend.held.splice(0)) entry.ok(spec.reply('CURRENT'))
        await flush()
        assert.match(spec.shown(page), /CURRENT/)
        page.unmount()
      }
      // A request of the previous scope that is still running when the scope
      // changes can neither overwrite the new data nor leave the page loading.
      for (const rejected of [false, true]) {
        const { auth, backend, page } = await open(t, name, {
          reply: config => (route(config) === spec.data ? HOLD : defaults(config)),
        })
        const stale = backend.take(config => !isNew(config))
        assert.equal(spec.loading(page), true, 'the first request is running')
        apply(auth)
        await flush()
        const fresh = backend.held.splice(0)
        assert.ok(fresh.length > 0 && fresh.every(entry => isNew(entry.config)))
        for (const entry of fresh) entry.ok(spec.reply('CURRENT'))
        await flush()
        if (rejected) stale.fail(500, 'old request failed')
        else stale.ok(spec.reply('STALE'))
        await flush()
        assert.match(spec.shown(page), /CURRENT/)
        assert.doesNotMatch(spec.shown(page), /STALE/)
        assert.equal(spec.loading(page), false)
        page.unmount()
      }
    })
  }
}

test('report generation uses profile stage and does not download stale files', async (t) => {
  const cases = [
    {
      page: 'StudentDetail',
      generate: (page) => { chooseCommand(page, '致家长信') },
      request: 'post /student/1/report',
      file: '张三致家长信.docx',
    },
    {
      page: 'Reports',
      generate: (page) => { button(page, '生成并下载').dispatch('click') },
      request: 'post /class/1/report',
      file: '初二（1）班报告.docx',
    },
    {
      page: 'Reports',
      generate: async (page) => {
        await choose(page.root.find(el => el.tagName === 'el-select' && el.getAttribute('placeholder') === '选择学生'), 5)
        await flush()
        chooseCommand(page, '致家长信')
      },
      request: 'post /student/5/report',
      file: '李四致家长信.docx',
    },
  ]
  for (const { page: name, generate, request, file } of cases) {
    for (const profileChanges of [false, true]) {
      // The address asks for junior; the profile says senior.
      const { client, auth, backend, page } = await open(t, name, {
        stage: 'senior',
        query: '?education_stage=junior',
        reply: (config) => {
          const at = route(config)
          if (at === request) return HOLD
          if (at === 'get /student/1/detail') return { data: { student: { id: 1, name: '张三' } } }
          if (at === 'get /student/list') return { data: { items: [{ id: 5, name: '李四', student_no: '05' }], total: 1 } }
          return defaults(config)
        },
      })
      const downloads = captureDownloads(t, page)
      await generate(page)
      await flush()
      const report = backend.take(config => route(config) === request)
      assert.equal(stageOf(report.config), 'senior', `${name} ${request}`)
      if (request.startsWith('post /student/')) assert.equal(bodyOf(report.config).type, 'parent_letter')
      if (profileChanges) {
        auth.user = { ...auth.user, education_stage: 'junior' }
        await flush()
      }
      report.ok({ download_url: '/api/files/mock' })
      await flush()
      const fileRequests = client.requests.filter(config => config.url === '/files/mock')
      if (profileChanges) {
        assert.deepEqual(fileRequests.map(route), [], `${name}: a report generated for the old profile is never downloaded`)
        assert.deepEqual(downloads.map(download => download.filename), [])
      } else {
        assert.equal(fileRequests.length, 1, `${name}: the report is downloaded while the profile is unchanged`)
        assert.deepEqual(downloads.map(download => download.filename), [file])
      }
      page.unmount()
    }
  }
})

test('parent shares and heatmaps cannot return from an old account scope', async (t) => {
  // Reports: the share list requested on mount by the previous teacher.
  {
    const { auth, backend, page } = await open(t, 'Reports', {
      reply: config => (route(config) === 'get /class/1/letter-shares' && tokenOf(config) === 'teacher-token' ? HOLD : defaults(config)),
    })
    const old = backend.take(config => route(config) === 'get /class/1/letter-shares')
    changes.account.apply(auth)
    await flush()
    old.ok({ shares: [{ id: 99, student_name: '旧账号学生', status: '有效', url: '/letter/old' }] })
    await flush()
    assert.equal(page.root.byClass('letter-share-table') === null, true, 'no share list is shown')
    assert.doesNotMatch(page.root.text, /旧账号学生/)
    assert.ok(page.root.byClass('letter-share-panel').find(el => el.tagName === 'el-empty' && el.getAttribute('description') === '尚未生成家长专属链接'))
    page.unmount()
  }
  // ClassTable: the heatmap the previous teacher opened, answered after the
  // next teacher opened it again.
  {
    const { auth, backend, page } = await open(t, 'ClassTable', {
      reply: config => (route(config) === 'get /class/1/heatmap' ? HOLD : defaults(config)),
    })
    const heatmapDialog = () => page.root.find(el => el.tagName === 'el-dialog' && el.getAttribute('title') === '知识点热力图')
    chooseCommand(page, '查看知识点热力图')
    await flush()
    assert.equal(heatmapDialog().dataset.open, 'true')
    const old = backend.take(config => route(config) === 'get /class/1/heatmap')
    changes.account.apply(auth)
    await flush()
    assert.equal(heatmapDialog().dataset.open, 'false', 'switching accounts closes the old heatmap')
    chooseCommand(page, '查看知识点热力图')
    await flush()
    const fresh = backend.take(config => route(config) === 'get /class/1/heatmap')
    assert.equal(tokenOf(fresh.config), 'second-teacher-token')
    old.ok({ tree: [{ chapter: '旧账号章节', wrong_students: 3, topics: [{ topic: '旧知识点', wrong_students: 3 }] }] })
    await flush()
    assert.doesNotMatch(heatmapDialog().text, /旧账号章节|旧知识点/)
    assert.equal(heatmapDialog().find(el => el.tagName === 'el-collapse-item') === null, true, 'no chapter of the old heatmap is listed')
    assert.equal(isLoading(page.root.byClass('heatmap-dialog')), true, 'still waiting for the current heatmap')
    fresh.ok({ tree: [] })
    await flush()
    assert.equal(isLoading(page.root.byClass('heatmap-dialog')), false)
    assert.ok(heatmapDialog().find(el => el.tagName === 'el-empty' && el.getAttribute('description') === '暂无热力图数据'))
    page.unmount()
  }
})

test('parent letter creation follows senior profile and drops delayed old-stage links', async (t) => {
  const clipboard = installClipboard(t)
  const { client, auth, backend, page } = await open(t, 'Reports', {
    stage: 'senior',
    reply: config => (route(config) === 'post /class/1/report/batch' ? HOLD : defaults(config)),
  })
  button(page, '准备并复制链接').dispatch('click')
  await flush()
  const batch = backend.take(config => route(config) === 'post /class/1/report/batch')
  assert.deepEqual(bodyOf(batch.config), { education_stage: 'senior', type: 'parent_letter', lock: 'token', refresh: false })
  assert.equal(button(page, '生成中...').hasAttribute('loading'), true, 'the batch button is busy')
  messages.length = 0
  auth.user = { ...auth.user, education_stage: 'junior' }
  await flush()
  const afterSwitch = client.requests.length
  batch.ok({ mode: 'share', shares: [{ url: '/old-secret', student_name: 'old' }] })
  await flush()
  assert.deepEqual(clipboard, [], 'links of the old stage are never copied')
  assert.equal(page.root.byClass('letter-share-table') === null, true, 'no share list is shown')
  assert.doesNotMatch(page.root.text, /old-secret/)
  assert.equal(button(page, '准备并复制链接').hasAttribute('loading'), false)
  assert.deepEqual(messages, [])
  assert.deepEqual(client.requests.slice(afterSwitch).map(route), [], 'nothing is reloaded for the old stage')
})

test('class import callbacks cannot reveal stale account records after a profile change', async (t) => {
  const upload = (page, accept) => page.root.find(el => el.tagName === 'el-upload' && el.getAttribute('accept') === accept)
  const flows = [
    {
      endpoint: 'post /class/1/students/import-image',
      async start(page) {
        await button(page, '添加学生').dispatch('click')
        await flush()
        await choose(page.root.find(el => el.tagName === 'el-tabs'), 'image')
        await flush()
        upload(page, 'image/*').props['on-change']({ name: 'roster.png', raw: new Blob(['synthetic roster'], { type: 'image/png' }) })
        await flush()
        button(page, '识别名单').dispatch('click')
      },
      async reopen(page) {
        await button(page, '添加学生').dispatch('click')
        await flush()
        await choose(page.root.find(el => el.tagName === 'el-tabs'), 'image')
      },
      shownStale: page => /识别结果|old student/.test(page.root.find(el => el.tagName === 'el-dialog' && el.getAttribute('title') === '添加学生').text),
    },
    {
      endpoint: 'post /class/1/exam-scores/import',
      async start(page) {
        await chooseCommand(page, '导入月考成绩')
        await flush()
        upload(page, '.xlsx').props['on-change']({ name: 'scores.xlsx', raw: new Blob(['synthetic scores']) })
        await flush()
        button(page, '预览导入').dispatch('click')
      },
      reopen: page => chooseCommand(page, '导入月考成绩'),
      shownStale: page => /将导入|old student/.test(page.root.find(el => el.tagName === 'el-dialog' && el.getAttribute('title') === '导入月考成绩').text),
    },
  ]
  for (const { endpoint, start, reopen, shownStale } of flows) {
    const { auth, backend, page } = await open(t, 'ClassTable', {
      stage: 'senior',
      reply: config => (route(config) === endpoint ? HOLD : defaults(config)),
    })
    await start(page)
    await flush()
    const pending = backend.take(config => route(config) === endpoint)
    assert.ok(pending.config.data instanceof FormData && pending.config.data.get('file') instanceof Blob, endpoint)
    messages.length = 0
    changes.account.apply(auth)
    await flush()
    pending.ok({ recognized: [{ name: 'old student' }], matched: 1, unmatched: ['old student'], exam_names: ['旧考试'] })
    await flush()
    await reopen(page)
    await flush()
    assert.equal(shownStale(page), false, `${endpoint}: the previous account's result is not shown`)
    assert.deepEqual(messages, [])
    page.unmount()
  }
})

test('all report generation paths retain runtime single-flight protection', async (t) => {
  const paths = [
    { page: 'Reports', request: 'post /class/1/report', trigger: page => { const click = button(page, '生成并下载').handlers.click; return () => click() } },
    {
      page: 'Reports',
      request: 'post /student/5/report',
      trigger: async (page) => {
        await choose(page.root.find(el => el.tagName === 'el-select' && el.getAttribute('placeholder') === '选择学生'), 5)
        await flush()
        return () => chooseCommand(page, '致家长信', { force: true })
      },
    },
    { page: 'Reports', request: 'post /class/1/report/batch', trigger: page => { const click = button(page, '准备并复制链接').handlers.click; return () => click() } },
    { page: 'StudentDetail', request: 'post /student/1/report', trigger: page => () => chooseCommand(page, '致家长信', { force: true }) },
  ]
  for (const { page: name, request, trigger } of paths) {
    installClipboard(t)
    const { client, backend, page } = await open(t, name, {
      stage: 'senior',
      reply: (config) => {
        if (route(config) === request) return HOLD
        if (route(config) === 'get /student/list') return { data: { items: [{ id: 5, name: '李四' }], total: 1 } }
        return defaults(config)
      },
    })
    const generate = await trigger(page)
    const first = generate()
    await flush()
    const second = generate()
    await flush()
    assert.equal(client.requests.filter(config => route(config) === request).length, 1, `${name} ${request}`)
    for (const entry of backend.held.splice(0)) entry.ok({ mode: 'share', shares: [], file_id: 'synthetic-file' })
    await Promise.all([first, second])
    await flush()
    page.unmount()
  }
})
