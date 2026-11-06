import test from 'node:test'
import assert from 'node:assert/strict'
import './helpers/srcModules.js'
import { createPinia } from 'pinia'
import { buildUsageTourSteps, SERVICE_STATE_LEGEND } from '../src/utils/usageTour.js'
import { isRouteBusy, setRouteBusy } from '../src/utils/routeBusy.js'
import { createDesktopBindingManager, desktopProcessorReady } from '../src/composables/useDesktopBindingDialog.js'
import {
  TEACHER, assertAbsent, assertSame, cpuWorker, desktopBridge, dialogTitled, focusedOpener, isOpen, pressKey, signIn, teleportToBody, tourCard,
} from './helpers/homeGuideLayout.js'
import { memoryDocument } from './helpers/memoryDom.js'
import { installFetch } from './helpers/memoryFetch.js'
import { mountComponent } from './helpers/mountComponent.js'

// One Pinia for every mount in this file (see helpers/homeGuideLayout.js).
const pinia = createPinia()

const { default: Layout } = await import('../src/views/Layout.vue?sfc')
const { default: Home } = await import('../src/views/Home.vue?sfc')
const { default: App } = await import('../src/App.vue?sfc')
const { default: ClassTable } = await import('../src/views/ClassTable.vue?sfc')
const { default: Upload } = await import('../src/views/Upload.vue?sfc')
const { default: Exercise } = await import('../src/views/Exercise.vue?sfc')
const { default: GuidedTour } = await import('../src/components/GuidedTour.vue?sfc')
const { default: UsageGuideDialog } = await import('../src/components/UsageGuideDialog.vue?sfc')

const withStudents = { classes: [{ id: 7, class_name: '高一(3)班', student_count: 42 }], currentClassId: 7 }
const noClasses = { classes: [], currentClassId: null }
const STEP_COUNT = buildUsageTourSteps(withStudents).length

// The backend a signed-in layout talks to: the teacher's classes, nothing
// pending, and whatever `extra` answers.
function layoutBackend(client, { classes = withStudents, extra = () => null } = {}) {
  client.respond((config) => {
    const answer = extra(config)
    if (answer) return answer
    if (config.url === '/auth/classes') return { data: { classes: classes.classes, current_class_id: classes.currentClassId } }
    if (config.url === '/bank/pending-answers') return { data: { items: [], total: 0 } }
    return { data: {} }
  })
}

async function mountLayout(t, path, options = {}) {
  return mountComponent(t, Layout, { path, pinia, ...options })
}

const guideButton = page => page.root.find(el => el.getAttribute('aria-label') === '打开使用引导')

// The tour's first step names the current class; without classes loaded it
// only says where the class will show.
const NO_CLASS = '这里显示当前班级'

function assertTourAtFirstStep(message, title = '先确认当前班级') {
  const card = tourCard()
  assert.ok(card, message)
  assert.equal(card.byClass('guided-tour__count').text, `第 1 / ${STEP_COUNT} 步`)
  assert.equal(card.byClass('guided-tour__title').text, title)
}

async function skipTour(page) {
  await tourCard().byText('el-button', '跳过引导').dispatch('click')
  await page.flush()
  assertAbsent(tourCard())
}

test('登录后布局提供长期可见的使用引导入口并初始化首次引导', async (t) => {
  teleportToBody(t)
  const { client } = await signIn(pinia, { storage: { disclaimer_agreed: 'true' } })
  layoutBackend(client)
  const page = await mountLayout(t, '/upload')
  // Mounting the layout initializes the guide: a first visit gets the tour.
  assertTourAtFirstStep('the first-run tour starts once the layout is mounted')
  assert.match(tourCard().byClass('guided-tour__body').text, /高一\(3\)班/)
  await skipTour(page)

  // The header keeps an entry to the guide, which opens it again.
  const button = guideButton(page)
  assert.equal(button.tagName, 'el-button')
  assert.equal(button.getAttribute('data-tour'), 'guide-button')
  assert.equal(button.byClass('guide-button__label').text, '使用引导')
  await button.dispatch('click')
  await page.flush()
  assertTourAtFirstStep('the header button reopens the tour')
  page.unmount()

  // ?guide=1 opens the guide (the home page links there) and leaves the URL.
  const linked = await mountLayout(t, '/upload?guide=1&tab=x')
  assertTourAtFirstStep('?guide=1 opens the guide')
  assert.equal(linked.router.currentRoute.value.fullPath, '/upload?tab=x')
})

test('登录后布局在教学学段未完善时不初始化引导，入口提示先完善学段', async (t) => {
  teleportToBody(t)
  const incomplete = { ...TEACHER, profile_complete: false }
  const { client } = await signIn(pinia, { user: incomplete, storage: { disclaimer_agreed: 'true' } })
  layoutBackend(client)
  const page = await mountLayout(t, '/profile')
  assertAbsent(tourCard())
  await guideButton(page).dispatch('click')
  await page.flush()
  assertAbsent(tourCard())
  assert.deepEqual(client.messages.filter(({ type }) => type === 'info').map(({ message }) => message), ['请先选择教学学段，保存后继续使用引导'])
})

test('首页说明卡和首次免责声明确认都打开统一引导而不是重复弹窗', async (t) => {
  const { client } = await signIn(pinia)
  client.respond(() => ({ data: { items: [] } }))
  const page = await mountComponent(t, Home, { path: '/', pinia })
  const replace = t.mock.method(page.router, 'replace')
  const push = t.mock.method(page.router, 'push')
  // Home has no guide dialog of its own: only the disclaimer and the notices.
  assert.deepEqual(page.root.byTag('el-dialog').map(dialog => dialog.getAttribute('title')), ['用户协议与免责声明', '系统公告'])

  const disclaimer = dialogTitled(page.root, '用户协议与免责声明')
  assert.equal(isOpen(disclaimer), true, 'a first visit shows the disclaimer')
  await disclaimer.byText('el-button', '我已阅读并同意').dispatch('click')
  await page.flush()
  assert.equal(client.localStorage.getItem('disclaimer_agreed'), 'true')
  assert.equal(isOpen(disclaimer), false)
  assert.deepEqual(replace.mock.calls.map(call => call.arguments), [[{ path: '/', query: { guide: '1' } }]])
  assert.equal(page.router.currentRoute.value.fullPath, '/?guide=1')

  await page.root.find(el => el.classList.contains('command-row') && el.text.startsWith('使用引导')).dispatch('click')
  await page.flush()
  assert.deepEqual(push.mock.calls.map(call => call.arguments), [[{ path: '/', query: { guide: '1' } }]])
  assert.ok(page.root.byTag('el-dialog').every(dialog => !isOpen(dialog)), 'the guide is not a home page dialog')

  // The app shell renders the routed page and does nothing on its own: no
  // desktop status reads, no fallback prompt, no message box.
  page.unmount()
  const bridge = desktopBridge({ runtime_info: { runtime: 'desktop' }, get_worker_status: cpuWorker() })
  const Page = { name: 'RoutedPage', render: () => 'routed page' }
  const shell = await mountComponent(t, Page, {
    path: '/',
    pinia,
    routes: [{ path: '/', component: App, children: [{ path: '', component: { render: () => null } }] }],
    plugins: [bridge.plugin],
  })
  assert.equal(shell.root.text, 'routed page')
  assert.deepEqual(bridge.calls, [])
  assert.deepEqual(shell.boxes, [])
})

test('引导按真实批改顺序逐步指向页面上的按钮', () => {
  const steps = buildUsageTourSteps(withStudents)
  assert.deepEqual(steps.map((step) => step.key), [
    'class-switcher', 'roster', 'grade-category', 'template-import', 'paper-upload', 'service-state', 'start-grading', 'review', 'replay',
  ])
  for (const step of steps) {
    assert.ok(step.title && step.body, `${step.key} must say what to click`)
    assert.ok(step.targets.length > 0, `${step.key} must point at a control`)
  }
  assert.equal(steps.find((step) => step.key === 'roster').route, '/students')
  for (const key of ['grade-category', 'template-import', 'paper-upload', 'service-state', 'start-grading', 'review']) {
    assert.equal(steps.find((step) => step.key === key).route, '/upload')
  }
  assert.match(steps[0].body, /高一\(3\)班/)
})

test('引导随班级状态指向下一件该做的事', () => {
  const none = buildUsageTourSteps({ classes: [], currentClassId: null })
  assert.deepEqual(none[1].targets, ['class-create'])
  assert.match(none[1].title, /添加班级/)
  const empty = buildUsageTourSteps({ classes: [{ id: 1, class_name: '初二1班', student_count: 0 }], currentClassId: 1 })
  assert.deepEqual(empty[1].targets, ['student-add'])
  assert.match(empty[1].title, /添加学生/)
  assert.match(empty[1].body, /姓名和学号要与学生写在试卷上的一致/)
  const full = buildUsageTourSteps(withStudents)
  assert.deepEqual(full[1].targets, ['student-add'])
  assert.match(full[1].body, /42 名学生/)
})

test('引导保留服务状态含义和教师确认边界', () => {
  const steps = buildUsageTourSteps(withStudents)
  const status = steps.find((step) => step.key === 'service-state')
  assert.deepEqual(SERVICE_STATE_LEGEND.map((item) => item.label), ['可用', '需排队', '暂不可用'])
  assert.equal(status.legend, SERVICE_STATE_LEGEND)
  assert.match(status.body, /只表示能否开启新任务/)
  assert.match(SERVICE_STATE_LEGEND[2].text, /暂时禁止开启新任务/)
  assert.match(steps.find((step) => step.key === 'start-grading').body, /已完成、失败和剩余数量/)
  assert.match(steps.find((step) => step.key === 'review').emphasis, /身份、答案、评分和正式成绩均以教师确认为准/)
})

// The data-tour anchors a mounted page renders, by name.
function anchors(root) {
  const found = new Map()
  for (const el of root.findAll(node => node.hasAttribute('data-tour'))) {
    const key = el.getAttribute('data-tour')
    found.set(key, [...(found.get(key) ?? []), el])
  }
  return found
}

// The grading page's service as it is when a teacher opens it.
function gradingBackend(client, extra = () => null) {
  client.respond((config) => {
    const answer = extra(config)
    if (answer) return answer
    if (config.url === '/batch-grade/availability') return { data: { status: 'available' } }
    if (config.url === '/batch-grade/template/availability') return { data: { status: 'available', registration_hardware: { status: 'ready' } } }
    return { data: { items: [], total: 0 } }
  })
}

test('每个引导锚点都真实存在于它所指的页面', async (t) => {
  teleportToBody(t)
  const { client } = await signIn(pinia, { storage: { disclaimer_agreed: 'true', 'mathagent_usage_guide_v3:7': 'true' } })
  layoutBackend(client)
  const layout = await mountLayout(t, '/upload')
  const layoutAnchors = anchors(layout.root)
  layout.unmount()
  for (const key of ['class-switcher', 'guide-button', 'menu-toggle', 'nav-grade-records']) {
    assert.ok(layoutAnchors.has(key), `the layout renders ${key}`)
  }

  gradingBackend(client)
  const upload = await mountComponent(t, Upload, { path: '/upload', pinia })
  const uploadAnchors = anchors(upload.root)
  // Both template branches (import / already saved) carry the anchor.
  assert.equal(uploadAnchors.get('template-import').length, 1)
  assert.ok(uploadAnchors.get('template-import')[0].classList.contains('template-upload'), 'before a template is saved: the import area')
  upload.setup.confirmedTemplateId = 'tpl-1'
  await upload.flush()
  const saved = anchors(upload.root).get('template-import')
  assert.equal(saved.length, 1)
  assert.ok(saved[0].classList.contains('tpl-confirmed'), 'after saving: the saved template panel')
  upload.unmount()

  for (const state of [noClasses, withStudents]) {
    layoutBackend(client, { classes: state })
    const classPage = await mountComponent(t, ClassTable, { path: '/students', pinia })
    const pages = { '/students': anchors(classPage.root), '/upload': uploadAnchors }
    classPage.unmount()
    for (const step of buildUsageTourSteps(state)) {
      const page = step.route ? pages[step.route] : layoutAnchors
      assert.ok(page, `${step.key} navigates to an unmapped page`)
      for (const key of step.targets) {
        assert.ok(page.has(key), `${step.key} → ${key} (${state.classes.length} classes)`)
      }
    }
  }
})

const TOUR_STEPS = [
  { key: 'here', title: '先看这里', body: '第一步说明', targets: [] },
  { key: 'upload', route: '/upload', isOn: path => path === '/upload', title: '去上传页', body: '第二步说明', targets: [] },
]

test('聚光引导遮住页面、可键盘操作，并且不会替老师中断进行中的任务', async (t) => {
  t.after(() => setRouteBusy('upload', false))
  teleportToBody(t)
  await signIn(pinia)
  const finishes = []
  // Every path renders the same page, so the tour stays mounted while it navigates.
  const page = await mountComponent(t, GuidedTour, {
    path: '/',
    pinia,
    routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }],
    props: { onFinish: reason => finishes.push(reason) },
  })
  const opener = focusedOpener(page.root)
  await page.vm.exposed.start(TOUR_STEPS)
  await page.flush()

  // A modal dialog over the whole page, outside the page's own tree.
  const card = tourCard()
  assert.equal(card.getAttribute('role'), 'dialog')
  assert.equal(card.getAttribute('aria-modal'), 'true')
  assert.equal(memoryDocument.body.byClass('guided-tour__scrim') !== null, true)
  assert.equal(page.root.contains(card), false, 'teleported to the body')
  assert.equal(card.byClass('guided-tour__title').text, '先看这里')
  assertSame(memoryDocument.activeElement, card.byText('el-button', '下一步'), 'focus moves into the card')

  // While a page is busy, the tour explains instead of navigating away.
  setRouteBusy('upload', true)
  pressKey('ArrowRight')
  await page.flush()
  assert.equal(tourCard().byClass('guided-tour__title').text, '去上传页')
  assert.equal(page.router.currentRoute.value.path, '/')
  assert.equal(tourCard().byClass('guided-tour__notice').text, '当前页面有任务正在处理，引导不会切换页面，以免中断任务。')

  setRouteBusy('upload', false)
  pressKey('ArrowLeft')
  await page.flush()
  assert.equal(tourCard().byClass('guided-tour__title').text, '先看这里')
  pressKey('ArrowRight')
  await page.flush()
  assert.equal(page.router.currentRoute.value.path, '/upload', 'an idle page lets the tour take the teacher there')
  assertAbsent(tourCard().byClass('guided-tour__notice'))

  // Escape skips the tour and gives focus back.
  const escape = pressKey('Escape')
  await page.flush()
  assert.equal(escape.defaultPrevented, true)
  assert.deepEqual(finishes, ['skip'])
  assertAbsent(tourCard())
  assertSame(memoryDocument.activeElement, opener, 'focus returns to where it was')
  assert.equal(memoryDocument.listeners.get('keydown')?.size ?? 0, 0, 'the keyboard is released')
})

test('上传页在请求进行中登记忙碌并拦截离开，卸载时清除', async (t) => {
  const idle = await signIn(pinia)
  gradingBackend(idle.client)
  const idlePage = await mountComponent(t, Upload, { path: '/upload', pinia })
  assert.equal(isRouteBusy(), false, 'an idle grading page is not busy')
  await idlePage.router.push('/')
  assert.equal(idlePage.router.currentRoute.value.path, '/')
  assert.deepEqual(idlePage.win.confirmations, [])
  idlePage.unmount()

  // Reopening the page resumes a template recognition still running on the
  // server; its state request is in flight.
  const { client } = await signIn(pinia, {
    storage: { 'mathagent_template_recognition:7': JSON.stringify({ template_id: 'tpl-9', category: 'daily' }) },
  })
  gradingBackend(client)
  const backend = installFetch(t, () => new Promise(() => {}))
  let leave = false
  const page = await mountComponent(t, Upload, { path: '/upload', pinia, confirm: () => leave })
  assert.match(backend.requests[0].url, /\/template\/tpl-9\/recognition/)
  assert.equal(isRouteBusy(), true)
  await page.router.push('/')
  assert.equal(page.router.currentRoute.value.path, '/upload', 'the teacher chose to stay')
  assert.deepEqual(page.win.confirmations, ['任务正在处理中，离开会中断当前请求。确定离开吗？'])
  page.unmount()
  assert.equal(isRouteBusy(), false, 'leaving the page clears its busy mark')
})

test('出题页在生成进行中登记忙碌并拦截离开，结束后清除', async (t) => {
  const { client } = await signIn(pinia)
  let finishGenerating
  client.respond(({ url }) => {
    if (url === '/exercises/generate') return new Promise((resolve) => { finishGenerating = resolve })
    return { data: { items: [], total: 0 } }
  })
  let leave = false
  const page = await mountComponent(t, Exercise, { path: '/exercise', pinia, confirm: () => leave })
  assert.equal(isRouteBusy(), false)
  // The click handler runs until the generation answers.
  const generation = page.root.byText('el-button', '按题型从题库抽题').dispatch('click')
  await page.flush()
  assert.equal(isRouteBusy(), true)
  await page.router.push('/')
  assert.equal(page.router.currentRoute.value.path, '/exercise')
  assert.deepEqual(page.win.confirmations, ['练习卷正在生成或导出，离开会中断。确定离开吗？'])

  finishGenerating({ data: { title: '练习', exercises: [{ type: 'choice', question: '1+1=?', answer: '2', options: { A: '1', B: '2', C: '3', D: '4' } }] } })
  await generation
  await page.flush()
  assert.equal(isRouteBusy(), false)
  await page.router.push('/')
  assert.equal(page.router.currentRoute.value.path, '/')
  assert.equal(page.win.confirmations.length, 1, 'no question once the work is done')
})

test('出题页卸载时清除忙碌标记', async (t) => {
  const { client } = await signIn(pinia)
  client.respond(({ url }) => (url === '/exercises/generate' ? new Promise(() => {}) : { data: { items: [], total: 0 } }))
  const page = await mountComponent(t, Exercise, { path: '/exercise', pinia })
  page.root.byText('el-button', '按题型从题库抽题').dispatch('click')
  await page.flush()
  assert.equal(isRouteBusy(), true)
  page.unmount()
  assert.equal(isRouteBusy(), false)
})

test('页面忙碌标记按页面登记与清除', () => {
  assert.equal(isRouteBusy(), false)
  setRouteBusy('upload', true)
  setRouteBusy('exercise', true)
  setRouteBusy('upload', false)
  assert.equal(isRouteBusy(), true)
  setRouteBusy('exercise', false)
  assert.equal(isRouteBusy(), false)
})

test('网页首次引导按账号记忆，完成后仍可从顶栏再次打开', async (t) => {
  teleportToBody(t)
  const { client, auth } = await signIn(pinia, { storage: { disclaimer_agreed: 'true' } })
  layoutBackend(client)
  const first = await mountLayout(t, '/upload')
  assertTourAtFirstStep('first visit')
  await skipTour(first)
  assert.equal(client.localStorage.getItem('mathagent_usage_guide_v3:7'), 'true')
  assert.deepEqual(client.messages.filter(({ type }) => type === 'info').map(({ message }) => message), ['已跳过引导，可随时点右上角「使用引导」重看。'])
  first.unmount()

  // The next visit remembers it, and the header still opens it.
  const next = await mountLayout(t, '/upload')
  assertAbsent(tourCard(), 'not shown again automatically')
  await guideButton(next).dispatch('click')
  await next.flush()
  assertTourAtFirstStep('the header reopens it')
  next.unmount()

  // Another account on this browser gets its own first-run tour.
  auth.login('second-teacher-token', { ...TEACHER, id: 8, username: 'wang' })
  const other = await mountLayout(t, '/upload')
  assertTourAtFirstStep('a different account has not seen it')
  await skipTour(other)
  assert.equal(client.localStorage.getItem('mathagent_usage_guide_v3:8'), 'true')
  other.unmount()

  // An account known only by its username is remembered under that name.
  auth.login('third-teacher-token', { ...TEACHER, id: undefined, username: 'zhao' })
  const byName = await mountLayout(t, '/upload')
  await skipTour(byName)
  assert.equal(client.localStorage.getItem('mathagent_usage_guide_v3:zhao'), 'true')
})

// ---- Desktop client: the first-run processor check ------------------------

const NODE = 'synthetic-node'
const bound = { configured: true, nodeId: NODE }
const unbound = { configured: false }
const gpuWorker = (overrides = {}) => cpuWorker({
  computeBackend: 'windows_nvidia_gpu',
  gpuAcceleration: { deviceName: 'RTX 4060' },
  ...overrides,
})
const unavailableWorker = () => cpuWorker({
  computeBackend: 'none',
  localProcessingAvailable: false,
  webFallbackRequired: true,
  cpuAcceleration: { reasonCodes: ['insufficient_physical_cores'] },
})

// The desktop client's bridge, by default: bound, CPU ready.
function desktop(replies = {}) {
  return desktopBridge({
    runtime_info: { runtime: 'desktop', webFallbackConfigured: false },
    desktop_binding_status: bound,
    get_worker_status: cpuWorker(),
    ...replies,
  })
}

// The usage guide of the desktop client, as the layout drives it.
async function desktopGuide(t, bridge, { storage = {}, token } = {}) {
  teleportToBody(t)
  const { client, auth } = await signIn(pinia, { token, storage: { disclaimer_agreed: 'true', ...storage } })
  client.respond(() => ({ data: {} }))
  const page = await mountComponent(t, UsageGuideDialog, { path: '/upload', pinia, plugins: [bridge.plugin] })
  const setup = () => dialogTitled(page.root, '使用引导 · 本机处理检查')
  const view = {
    setup,
    binding: () => dialogTitled(page.root, '本机绑定管理'),
    tags: () => setup().byClass('desktop-status-grid').byTag('el-tag').map(tag => tag.text),
    descriptions: () => setup().byClass('desktop-status-grid').byTag('p').map(p => p.text),
    alert: () => setup().byClass('desktop-setup').find(el => el.tagName === 'el-alert'),
    actions: () => setup().byClass('desktop-actions').byTag('el-button').map(button => button.text),
    complete: () => setup().byClass('guide-footer').byTag('el-button')[0],
  }
  const act = {
    initialize: async () => { await page.vm.exposed.initialize(); await page.flush() },
    open: async () => { await page.vm.exposed.open(); await page.flush() },
    // The guide waits until the binding dialog is closed again.
    manageBinding: async () => { setup().byText('el-button', '本机绑定管理').dispatch('click'); await page.flush() },
    inBindingDialog: async (label) => { await view.binding().byText('el-button', label).dispatch('click'); await page.flush() },
    click: async (label) => { await setup().byText('el-button', label).dispatch('click'); await page.flush() },
    complete: async () => { await view.complete().dispatch('click'); await page.flush() },
  }
  return { client, auth, page, view, act }
}

const writes = bridge => bridge.commands().filter(command => /_teacher_desktop$/.test(command))
const required = dialog => ['close-on-click-modal', 'close-on-press-escape', 'show-close'].map(name => dialog.props[name])

test('桌面首次向导必须处理真实设备授权与本机处理器检测', async (t) => {
  const bridge = desktop()
  const { client, page, view, act } = await desktopGuide(t, bridge)
  await act.initialize()
  assert.deepEqual(bridge.commands(), ['runtime_info', 'desktop_binding_status', 'get_worker_status'])

  // First launch: the check comes first, and cannot be dismissed.
  assert.equal(isOpen(view.setup()), true)
  assertAbsent(tourCard(), 'the walkthrough waits for the check')
  assert.deepEqual(required(view.setup()), [false, false, false])
  let closed = false
  view.setup().props['before-close'](() => { closed = true })
  assert.equal(closed, false)
  assert.deepEqual(client.messages.map(({ message }) => message), ['请先完成本机处理器检测，或确认暂由云端接管。'])

  assert.deepEqual(view.tags(), ['已绑定', 'CPU'])
  assert.equal(view.descriptions()[1], 'CPU 已通过性能与内存门槛，可用并发 4。')
  assert.equal(view.alert().getAttribute('title'), 'CPU 已通过自检，本机处理可用。')
  assert.match(view.setup().byClass('desktop-footnote').text, /GPU 异常时自动回退 CPU/)
  assert.equal(view.complete().text, '下一步：操作引导')
  // A ready processor is not proof that the stored credential belongs to
  // this teacher: the binding has to be validated first.
  assert.equal(view.complete().hasAttribute('disabled'), true)

  await act.manageBinding()
  assert.equal(isOpen(view.binding()), true)
  assert.deepEqual(writes(bridge), [], 'opening the binding dialog writes nothing')
  bridge.replies.bind_teacher_desktop = bound
  await act.inBindingDialog('重新绑定并检测')
  await act.inBindingDialog('返回使用引导')
  assert.equal(view.complete().hasAttribute('disabled'), false, 'validated and ready')

  // Completing records the check for this binding, then the walkthrough runs.
  await act.complete()
  assert.equal(client.localStorage.getItem(`mathagent_desktop_setup_v1:${TEACHER.id}`), NODE)
  assert.equal(isOpen(view.setup()), false)
  assertTourAtFirstStep('the walkthrough follows the check', NO_CLASS)
  assert.deepEqual(writes(bridge), ['bind_teacher_desktop'], 'the only write is the one the teacher clicked in the binding dialog')
  page.unmount()

  // Next launch: the recorded binding needs no check; the walkthrough is still due.
  const again = desktop()
  const second = await desktopGuide(t, again, { storage: { [`mathagent_desktop_setup_v1:${TEACHER.id}`]: NODE } })
  await second.act.initialize()
  assert.equal(isOpen(second.view.setup()), false)
  assertTourAtFirstStep('no check for an already recorded binding', NO_CLASS)
  assert.deepEqual(writes(again), [])
  second.page.unmount()

  // A different device binding asks for the check again; with the
  // walkthrough already done, the check is all that is left.
  const rebound = desktop({ desktop_binding_status: { configured: true, nodeId: 'another-node' } })
  const third = await desktopGuide(t, rebound, {
    storage: { [`mathagent_desktop_setup_v1:${TEACHER.id}`]: NODE, [`mathagent_usage_guide_v3:${TEACHER.id}`]: 'true' },
  })
  await third.act.initialize()
  assert.equal(isOpen(third.view.setup()), true)
  assert.deepEqual(required(third.view.setup()), [false, false, false])
  assert.equal(third.view.complete().text, '完成引导')
  assertAbsent(tourCard())

  // Checking the new binding records it and closes the guide; the walkthrough
  // the teacher has already seen does not start again.
  await third.act.manageBinding()
  rebound.replies.bind_teacher_desktop = { configured: true, nodeId: 'another-node' }
  await third.act.inBindingDialog('重新绑定并检测')
  await third.act.inBindingDialog('返回使用引导')
  assert.equal(third.view.complete().hasAttribute('disabled'), false)
  await third.act.complete()
  assert.equal(isOpen(third.view.setup()), false)
  assert.equal(third.client.localStorage.getItem(`mathagent_desktop_setup_v1:${TEACHER.id}`), 'another-node')
  assertAbsent(tourCard(), '引导已完成，不再自动开始')
})

test('已记录的绑定在本机授权失效或任务通道断开时，启动仍弹出本机检查', async (t) => {
  const handoffs = [
    [{ configured: true, running: true, connected: true, lastErrorCode: 'node_auth_failed' }, '本机授权已失效，请重新绑定后再继续。'],
    [{ configured: true, running: true, connected: false }, '本机任务通道正在连接服务器，请稍候重新检测。'],
  ]
  for (const [handoff, alert] of handoffs) {
    // This binding's check is recorded and the walkthrough is done: only the
    // processor itself can ask for attention.
    const bridge = desktop({ get_worker_status: cpuWorker({ handoff }) })
    const { page, view, act } = await desktopGuide(t, bridge, {
      storage: { [`mathagent_desktop_setup_v1:${TEACHER.id}`]: NODE, [`mathagent_usage_guide_v3:${TEACHER.id}`]: 'true' },
    })
    await act.initialize()
    assert.equal(isOpen(view.setup()), true, `${JSON.stringify(handoff)} needs the teacher's attention`)
    assert.deepEqual(required(view.setup()), [true, true, true], 'the recorded check can still be dismissed')
    assert.equal(view.alert().getAttribute('title'), alert)
    assertAbsent(tourCard())
    page.unmount()
  }
})

test('桌面向导中账号已校验但处理器状态读取失败时不能完成', async (t) => {
  const bridge = desktop({ bind_teacher_desktop: bound })
  const { view, act } = await desktopGuide(t, bridge)
  await act.initialize()
  await act.manageBinding()
  bridge.replies.get_worker_status = () => { throw new Error('worker status unavailable') }
  await act.inBindingDialog('重新绑定并检测')
  assert.match(view.binding().find(el => el.tagName === 'el-alert').getAttribute('title'), /本机处理组件未正常启动/)
  await act.inBindingDialog('返回使用引导')
  assert.equal(view.alert().getAttribute('type'), 'error')
  assert.equal(view.complete().hasAttribute('disabled'), true, 'a verified account alone does not resolve the check')
})

test('桌面向导未绑定时不能完成，绑定交给共享的本机绑定管理并用当前教师令牌', async (t) => {
  const bridge = desktop({
    desktop_binding_status: unbound,
    get_worker_status: gpuWorker({ handoff: { configured: true, running: true, connected: false } }),
  })
  const { client, page, view, act } = await desktopGuide(t, bridge)
  await act.initialize()
  assert.deepEqual(view.tags(), ['待绑定', 'NVIDIA GPU'])
  assert.equal(view.descriptions()[1], 'RTX 4060 已通过正确性与速度自检。')
  assert.equal(view.alert().getAttribute('title'), '请先授权这台电脑，再确认实际使用 GPU 还是 CPU。')
  assert.equal(view.complete().hasAttribute('disabled'), true)
  page.setup.completeGuide()
  await page.flush()
  assert.equal(isOpen(view.setup()), true, 'an unresolved check cannot be completed')
  assert.equal(client.localStorage.getItem(`mathagent_desktop_setup_v1:${TEACHER.id}`), null)

  // The guide opens the one shared binding dialog; opening it only reads status.
  await act.manageBinding()
  assert.equal(isOpen(view.binding()), true)
  assert.equal(view.binding().byClass('binding-actions').parentNode.find(el => el.getAttribute('name') === 'footer').text, '返回使用引导')
  assert.deepEqual(writes(bridge), [])

  bridge.replies.bind_teacher_desktop = bound
  bridge.replies.get_worker_status = gpuWorker()
  await act.inBindingDialog('绑定并检测')
  assert.deepEqual(bridge.calls.filter(call => call.command === 'bind_teacher_desktop').map(call => call.args), [{ bearerToken: 'teacher-token' }])
  await act.inBindingDialog('返回使用引导')
  assert.equal(isOpen(view.binding()), false)

  // Back in the guide: bound and verified for this teacher, ready.
  assert.deepEqual(view.tags(), ['已绑定', 'NVIDIA GPU'])
  assert.equal(view.alert().getAttribute('title'), 'NVIDIA GPU 已通过自检，本机处理可用。')
  assert.equal(view.complete().hasAttribute('disabled'), false)
  await act.complete()
  assert.equal(client.localStorage.getItem(`mathagent_desktop_setup_v1:${TEACHER.id}`), NODE)
  assertTourAtFirstStep('the walkthrough follows', NO_CLASS)
  assert.deepEqual(writes(bridge), ['bind_teacher_desktop'])
})

test('桌面向导按实际处理器标注状态和原因', async (t) => {
  const bridge = desktop()
  const { view, act } = await desktopGuide(t, bridge)
  const cases = [
    [cpuWorker({ paused: true }), ['已绑定', '已暂停'], '本机处理已暂停，重新检测时会恢复。'],
    [gpuWorker(), ['已绑定', 'NVIDIA GPU'], 'RTX 4060 已通过正确性与速度自检。', 'NVIDIA GPU 已通过自检，本机处理可用。'],
    [unavailableWorker(), ['已绑定', '暂不可用'], 'GPU 与 CPU 当前都未达到本地处理门槛，将由云端接管。', 'CPU 物理核心数未达到本地处理门槛。'],
    [cpuWorker({ handoff: { configured: true, running: true, connected: true, lastErrorCode: 'node_auth_failed' } }), ['已绑定', 'CPU'], undefined, '本机授权已失效，请重新绑定后再继续。'],
    [cpuWorker({ handoff: { configured: true, running: true, connected: false } }), ['已绑定', 'CPU'], undefined, '本机任务通道正在连接服务器，请稍候重新检测。'],
    [cpuWorker({ handoff: { configured: true, running: false, connected: true } }), ['已绑定', 'CPU'], undefined, '本机处理器暂不可用；可以稍后重试或使用网页版。'],
    [cpuWorker(), ['已绑定', 'CPU'], 'CPU 已通过性能与内存门槛，可用并发 4。', 'CPU 已通过自检，本机处理可用。'],
  ]
  for (const [worker, tags, description, alert] of cases) {
    bridge.replies.get_worker_status = worker
    await act.open()
    assert.deepEqual(view.tags(), tags)
    if (description) assert.equal(view.descriptions()[1], description)
    if (alert) assert.equal(view.alert().getAttribute('title'), alert)
  }

  // A failed status read counts as unavailable and says why.
  bridge.replies.get_worker_status = () => { throw new Error('worker sidecar exited') }
  await act.open()
  assert.deepEqual(view.tags(), ['已绑定', '暂不可用'])
  assert.equal(view.alert().getAttribute('title'), '本机处理组件未正常启动，请重启客户端后重试。')
  assert.equal(view.alert().getAttribute('type'), 'error')
})

test('桌面向导在登录过期时不调用本机绑定，只能先由云端接管', async (t) => {
  const bridge = desktop({ desktop_binding_status: unbound, get_worker_status: unavailableWorker() })
  const { view, act } = await desktopGuide(t, bridge, { token: '' })
  await act.initialize()
  assert.deepEqual(view.actions(), ['本机绑定管理'], 'no fallback before a binding attempt')
  await act.manageBinding()
  assert.equal(isOpen(view.binding()), false, 'no binding dialog without a login')
  assert.equal(view.alert().getAttribute('title'), '登录已过期，请重新登录后绑定本机。')
  assert.deepEqual(view.actions(), ['本机绑定管理', '先由云端接管，稍后重试'])
  assert.deepEqual(bridge.commands(), ['runtime_info', 'desktop_binding_status', 'get_worker_status'])
})

test('桌面向导选择云端接管后记为 cloud，之后只要仍需云端就不再强制检查', async (t) => {
  const bridge = desktop({
    desktop_binding_status: unbound,
    get_worker_status: unavailableWorker(),
    bind_teacher_desktop: () => { throw new Error('registration server is unavailable') },
  })
  const { client, page, view, act } = await desktopGuide(t, bridge)
  await act.initialize()
  assert.deepEqual(required(view.setup()), [false, false, false])
  await act.manageBinding()
  await act.inBindingDialog('绑定并检测')
  assert.equal(view.binding().find(el => el.tagName === 'el-alert').getAttribute('title'), '暂时无法连接设备绑定服务，请检查网络后重试。')
  await act.inBindingDialog('返回使用引导')
  assert.equal(view.complete().hasAttribute('disabled'), true, 'a failed binding does not resolve the check')

  await act.click('先由云端接管，稍后重试')
  assert.ok(client.messages.some(({ message }) => message === '已记录：本机暂不处理任务，可稍后从“使用引导”重新检测。'))
  assert.equal(view.complete().hasAttribute('disabled'), false)
  await act.complete()
  assert.equal(client.localStorage.getItem(`mathagent_desktop_setup_v1:${TEACHER.id}`), 'cloud')
  assertTourAtFirstStep('the walkthrough follows', NO_CLASS)
  page.unmount()

  // While the cloud still has to take over, the check shows but can be dismissed.
  const stillCloud = desktop({ desktop_binding_status: unbound, get_worker_status: unavailableWorker() })
  const second = await desktopGuide(t, stillCloud, { storage: { [`mathagent_desktop_setup_v1:${TEACHER.id}`]: 'cloud' } })
  await second.act.initialize()
  assert.equal(isOpen(second.view.setup()), true)
  assert.deepEqual(required(second.view.setup()), [true, true, true])
  assert.equal(second.view.complete().hasAttribute('disabled'), false)
  second.page.unmount()

  // So does a launch whose processor status cannot be read, whether the read
  // itself fails or the client reports that it could not read it.
  for (const worker of [() => { throw new Error('worker sidecar exited') }, { ...unavailableWorker(), webFallbackRequired: false, statusReadFailed: true }]) {
    const unreadable = desktop({ desktop_binding_status: unbound, get_worker_status: worker })
    const unread = await desktopGuide(t, unreadable, { storage: { [`mathagent_desktop_setup_v1:${TEACHER.id}`]: 'cloud' } })
    await unread.act.initialize()
    assert.equal(isOpen(unread.view.setup()), true)
    assert.deepEqual(required(unread.view.setup()), [true, true, true])
    unread.page.unmount()
  }

  // Once this computer can process locally, the old cloud decision no longer counts.
  const nowReady = desktop()
  const third = await desktopGuide(t, nowReady, { storage: { [`mathagent_desktop_setup_v1:${TEACHER.id}`]: 'cloud' } })
  await third.act.initialize()
  assert.equal(isOpen(third.view.setup()), true)
  assert.deepEqual(required(third.view.setup()), [false, false, false])
})

test('从顶栏打开引导时桌面端先显示本机检查，网页端直接开始', async (t) => {
  const bridge = desktop()
  const { client, view, act } = await desktopGuide(t, bridge, {
    storage: { [`mathagent_desktop_setup_v1:${TEACHER.id}`]: NODE, 'mathagent_usage_guide_v3:7': 'true' },
  })
  await act.initialize()
  assert.equal(isOpen(view.setup()), false, 'nothing is due on this launch')
  assertAbsent(tourCard())
  await act.open()
  assert.equal(isOpen(view.setup()), true, 'the processor check comes first')
  assert.deepEqual(required(view.setup()), [true, true, true])
  assert.equal(view.complete().text, '下一步：操作引导')
  assertAbsent(tourCard())
  await act.complete()
  assertTourAtFirstStep('then the walkthrough', NO_CLASS)
  assert.equal(client.localStorage.getItem(`mathagent_desktop_setup_v1:${TEACHER.id}`), NODE)
})

const readyWorker = () => ({
  localProcessingAvailable: true, paused: false,
  handoff: { configured: true, running: true, connected: true, lastErrorCode: null },
})
const disconnectedWorker = () => ({ ...readyWorker(), handoff: { configured: true, running: true, connected: false } })

// The guide hands binding to the shared manager; drive it with injected native
// commands, session and wait so no real IPC or timer runs.
function bindingFixture(replies) {
  const calls = []
  const waits = []
  const manager = createDesktopBindingManager({
    getSession: () => ({ accountId: 910001, token: 'teacher-session-token' }),
    wait: async (ms) => { waits.push(ms) },
    invoke: async (command, args) => {
      calls.push({ command, args })
      if (command === 'bind_teacher_desktop') return { configured: true, nodeId: 'synthetic-node' }
      return replies[command]()
    },
  })
  manager.open('guide')
  return { manager, calls, waits }
}

test('桌面向导的绑定用当前教师令牌请求本机授权，并恢复暂停的处理器', async () => {
  let reads = 0
  const { manager, calls } = bindingFixture({
    get_worker_status: () => ((reads += 1) === 1 ? { ...readyWorker(), paused: true } : readyWorker()),
    resume_worker: readyWorker,
  })
  assert.equal(await manager.bind(), true)
  assert.deepEqual(calls.map((call) => call.command), ['bind_teacher_desktop', 'get_worker_status', 'resume_worker'])
  assert.deepEqual(calls[0].args, { bearerToken: 'teacher-session-token' })
  assert.equal(manager.state.verified, true)
  assert.equal(desktopProcessorReady(manager.state.binding, manager.state.worker), true)
})

test('桌面向导等待处理器连上服务最多再查 6 次，连上即停止', async () => {
  const statusReads = (calls) => calls.filter((call) => call.command === 'get_worker_status').length
  const never = bindingFixture({ get_worker_status: disconnectedWorker })
  assert.equal(await never.manager.bind(), true)
  assert.equal(statusReads(never.calls), 1 + 6, 'one read after binding, then six polling attempts')
  assert.equal(never.waits.length, 5, 'polls after the first wait through the injected delay')
  assert.equal(desktopProcessorReady(never.manager.state.binding, never.manager.state.worker), false)

  let reads = 0
  const soon = bindingFixture({ get_worker_status: () => ((reads += 1) < 3 ? disconnectedWorker() : readyWorker()) })
  assert.equal(await soon.manager.bind(), true)
  assert.equal(statusReads(soon.calls), 3)
  assert.equal(desktopProcessorReady(soon.manager.state.binding, soon.manager.state.worker), true)
})

test('班级初始化失败也不会跳过首次使用引导', async (t) => {
  teleportToBody(t)
  // The class list cannot be loaded.
  const { client } = await signIn(pinia, { storage: { disclaimer_agreed: 'true' } })
  layoutBackend(client, { extra: ({ url }) => (url === '/auth/classes' ? { status: 500, data: { detail: '班级服务暂不可用' } } : null) })
  const failedList = await mountLayout(t, '/upload')
  assertTourAtFirstStep('the tour still starts', NO_CLASS)
  failedList.unmount()

  // Selecting the first class fails: the layout's own bootstrap throws.
  layoutBackend(client, {
    classes: { classes: withStudents.classes, currentClassId: null },
    extra: ({ url }) => (url === '/auth/class/switch' ? { status: 500, data: { detail: '切换班级失败' } } : null),
  })
  const failedSwitch = await mountLayout(t, '/upload', { allowErrors: true })
  assert.ok(failedSwitch.errors.some(({ err }) => /切换班级失败/.test(err?.message)), 'the switch failure surfaces')
  assert.ok(tourCard(), 'the guide was still initialized')
  failedSwitch.unmount()

  // On the desktop client the mandatory processor check still opens.
  const bridge = desktop({ desktop_binding_status: unbound })
  layoutBackend(client, { extra: ({ url }) => (url === '/auth/classes' ? { status: 500, data: { detail: '班级服务暂不可用' } } : null) })
  const desktopLayout = await mountLayout(t, '/upload', { plugins: [bridge.plugin] })
  assert.deepEqual(bridge.commands().slice(0, 3), ['runtime_info', 'desktop_binding_status', 'get_worker_status'])
  assert.equal(isOpen(dialogTitled(desktopLayout.root, '使用引导 · 本机处理检查')), true)
})

test('桌面无合格处理器时只打开编译期固定网页版入口', async (t) => {
  const failingBind = () => { throw new Error('registration server is unavailable') }
  for (const webFallbackConfigured of [true, false]) {
    await t.test(`webFallbackConfigured=${webFallbackConfigured}`, async (t) => {
      const bridge = desktop({
        runtime_info: { runtime: 'desktop', webFallbackConfigured },
        desktop_binding_status: unbound,
        get_worker_status: unavailableWorker(),
        bind_teacher_desktop: failingBind,
        open_web_fallback: null,
      })
      const { client, page, view, act } = await desktopGuide(t, bridge)
      await act.initialize()
      assert.deepEqual(view.actions(), ['本机绑定管理'], 'nothing to fall back to before trying to bind')
      await act.manageBinding()
      await act.inBindingDialog('绑定并检测')
      await act.inBindingDialog('返回使用引导')
      const fallbacks = webFallbackConfigured
        ? ['本机绑定管理', '打开网页版继续', '先由云端接管，稍后重试']
        : ['本机绑定管理', '先由云端接管，稍后重试']
      assert.deepEqual(view.actions(), fallbacks)
      assert.doesNotMatch(page.html(), /https?:\/\//, 'no web address is shown or linked')
      if (!webFallbackConfigured) return

      await act.click('打开网页版继续')
      assert.deepEqual(bridge.calls.at(-1), { command: 'open_web_fallback', args: {} }, 'the native client opens its built-in address')
      assert.deepEqual(page.win.opened, [], 'never window.open')
      assert.deepEqual(client.location.navigations, [])
      assert.ok(client.messages.some(({ type, message }) => type === 'success' && message === '已打开固定的 MathAgent 网页版入口'))
      assert.equal(view.complete().hasAttribute('disabled'), false)
    })
  }

  await t.test('the fixed entry cannot be opened', async (t) => {
    const bridge = desktop({
      runtime_info: { runtime: 'desktop', webFallbackConfigured: true },
      desktop_binding_status: unbound,
      get_worker_status: unavailableWorker(),
      bind_teacher_desktop: failingBind,
      open_web_fallback: () => { throw new Error('not configured') },
    })
    const { view, act } = await desktopGuide(t, bridge)
    await act.initialize()
    await act.manageBinding()
    await act.inBindingDialog('绑定并检测')
    await act.inBindingDialog('返回使用引导')
    await act.click('打开网页版继续')
    assert.equal(view.alert().getAttribute('title'), '暂时无法打开网页版，请联系管理员确认固定入口。')
    assert.equal(view.complete().hasAttribute('disabled'), true)
  })
})
