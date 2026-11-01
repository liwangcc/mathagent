import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { buildUsageTourSteps, SERVICE_STATE_LEGEND } from '../src/utils/usageTour.js'
import { isRouteBusy, setRouteBusy } from '../src/utils/routeBusy.js'

function source(relativePath) {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8')
}

const guide = source('../src/components/UsageGuideDialog.vue')
const tour = source('../src/components/GuidedTour.vue')
const classTable = source('../src/views/ClassTable.vue')
const upload = source('../src/views/Upload.vue')
const exercise = source('../src/views/Exercise.vue')
const layout = source('../src/views/Layout.vue')
const home = source('../src/views/Home.vue')
const app = source('../src/App.vue')
const bindingManager = source('../src/composables/useDesktopBindingDialog.js')

test('登录后布局提供长期可见的使用引导入口并初始化首次引导', () => {
  assert.match(layout, /<UsageGuideDialog ref="usageGuideRef" \/>/)
  assert.match(layout, /aria-label="打开使用引导"/)
  assert.match(layout, />使用引导<\/span>/)
  assert.match(layout, /usageGuideRef\.value\?\.initialize\(\)/)
  assert.match(layout, /watch\(\(\) => route\.query\.guide, openGuideFromQuery/)
})

test('首页说明卡和首次免责声明确认都打开统一引导而不是重复弹窗', () => {
  assert.equal((home.match(/query: \{ guide: '1' \}/g) || []).length, 2)
  assert.doesNotMatch(home, /v-model="showGuide"|const showGuide/)
  assert.doesNotMatch(app, /get_worker_status|showWebFallbackPrompt|ElMessageBox/)
})

const withStudents = { classes: [{ id: 7, class_name: '高一(3)班', student_count: 42 }], currentClassId: 7 }

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

test('每个引导锚点都真实存在于它所指的页面', () => {
  const pageFor = { '/students': classTable, '/upload': upload }
  for (const state of [{ classes: [], currentClassId: null }, withStudents]) {
    for (const step of buildUsageTourSteps(state)) {
      const page = step.route ? pageFor[step.route] : layout
      assert.ok(page, `${step.key} navigates to an unmapped page`)
      for (const key of step.targets) {
        assert.match(page, new RegExp(`data-tour="${key}"`), `${step.key} → ${key}`)
      }
    }
  }
  for (const key of ['class-switcher', 'guide-button', 'menu-toggle', 'nav-grade-records']) {
    assert.match(layout, new RegExp(`data-tour="${key}"`))
  }
  // Both template branches (import / already saved) carry the anchor.
  assert.equal((upload.match(/data-tour="template-import"/g) || []).length, 2)
})

test('聚光引导遮住页面、可键盘操作，并且不会替老师中断进行中的任务', () => {
  assert.match(tour, /<Teleport to="body">/)
  assert.match(tour, /role="dialog"/)
  assert.match(tour, /aria-modal="true"/)
  assert.match(tour, /event\.key === 'Escape'[\s\S]*finish\('skip'\)/)
  assert.match(tour, /if \(isRouteBusy\(\)\) \{[\s\S]*?\} else \{[\s\S]*?router\.push\(current\.route\)/)
  assert.match(tour, /restoreFocus\.focus/)
  assert.match(upload, /const leaveInterruptsWork = computed\(/)
  assert.match(upload, /watch\(leaveInterruptsWork, \(busy\) => setRouteBusy\('upload', busy\)/)
  assert.match(upload, /onBeforeRouteLeave\(\(to, from, next\) => \{\n  if \(leaveInterruptsWork\.value\)/)
  assert.match(upload, /setRouteBusy\('upload', false\)/)
  assert.match(exercise, /watch\(leaveInterruptsWork, \(busy\) => setRouteBusy\('exercise', busy\)/)
  assert.match(exercise, /onBeforeRouteLeave\(\(to, from, next\) => \{\n  if \(leaveInterruptsWork\.value\)/)
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

test('网页首次引导按账号记忆，完成后仍可从顶栏再次打开', () => {
  assert.match(guide, /mathagent_usage_guide_v3/)
  assert.match(guide, /function onTourFinish\(reason\) \{\n  storageSet\(accountStorageKey\(WEB_GUIDE_KEY\)\)/)
  assert.match(guide, /<GuidedTour ref="tourRef" @finish="onTourFinish" \/>/)
  assert.match(guide, /auth\.user\?\.id \?\? auth\.user\?\.username/)
  assert.match(guide, /defineExpose\(\{ initialize, open \}\)/)
  assert.match(guide, /storageSet\(accountStorageKey\(WEB_GUIDE_KEY\)\)/)
})

test('桌面首次向导必须处理真实设备授权与本机处理器检测', () => {
  assert.match(guide, /mathagent_desktop_setup_v1/)
  assert.match(guide, /:close-on-click-modal="!desktopSetupRequired"/)
  assert.match(guide, /:close-on-press-escape="!desktopSetupRequired"/)
  assert.match(guide, /:show-close="!desktopSetupRequired"/)
  assert.match(guide, /desktopInvoke\('runtime_info'\)/)
  assert.match(guide, /desktopInvoke\('desktop_binding_status'\)/)
  assert.match(guide, /bindingManager\.open\('guide'\)/)
  assert.match(bindingManager, /invoke\('bind_teacher_desktop'/)
  assert.match(bindingManager, /bearerToken: expected\.token/)
  assert.match(guide, /if \(!auth\.token\) \{\s*bindingAttempted\.value = true/)
  assert.match(guide, /desktopInvoke\('get_worker_status'\)/)
  assert.match(bindingManager, /invoke\('resume_worker'\)/)
  assert.match(guide, /handoffReady/)
  assert.match(guide, /handoff\?\.running === true/)
  assert.match(guide, /handoff\?\.connected === true/)
  assert.match(guide, /lastErrorCode !== 'node_auth_failed'/)
  assert.match(guide, /!bindingConfigured\.value\s*\|\| !handoffReady\.value/)
  assert.match(guide, /!initialized\.value \|\| desktopRuntime\.value/)
  assert.match(guide, /desktopSetupResolved\.value = outcome\.verified && !outcome\.error && processorReady\.value/)
  assert.doesNotMatch(guide, /desktopInvoke\('(bind|rotate|unbind)_teacher_desktop'/)
  assert.match(bindingManager, /for \(let attempt = 0; attempt < 6; attempt \+= 1\)/)
  assert.match(guide, /const cloudFallbackStillValid = storedResolution === 'cloud'/)
  assert.match(guide, /webFallbackRequired === true[\s\S]*statusReadFailed === true/)
  assert.match(guide, /storedResolution === nodeId/)
  assert.match(guide, /const resolution = typeof nodeId === 'string' && nodeId\.length > 0 \? nodeId : 'cloud'/)
  assert.match(guide, /storageSetValue\(accountStorageKey\(DESKTOP_SETUP_KEY\), resolution\)/)
  assert.match(guide, /computeBackend === 'windows_nvidia_gpu'/)
  assert.match(guide, /computeBackend === 'windows_cpu_opencv'/)
  assert.match(guide, /GPU 异常时自动回退 CPU/)
  // Desktop: the processor check comes first, then the walkthrough.
  assert.match(guide, /if \(desktopSetupPending\.value \|\| processorNeedsAttention\) \{\n    openDesktopSetup\(webGuidePending\)/)
  assert.match(guide, /if \(desktopRuntime\.value\) openDesktopSetup\(true\)\n  else startTour\(\)/)
  assert.match(guide, /:disabled="desktopSetupRequired && !desktopSetupResolved"/)
  assert.match(guide, /if \(desktopSetupRequired\.value && !desktopSetupResolved\.value\) return/)
  assert.match(guide, /visible\.value = false\n  if \(tourAfterSetup\.value\) startTour\(\)/)
})

test('班级初始化失败也不会跳过首次使用引导', () => {
  assert.match(layout, /try \{[\s\S]*await auth\.fetchClasses\(\)[\s\S]*\} finally \{[\s\S]*usageGuideRef\.value\?\.initialize\(\)/)
})

test('桌面无合格处理器时只打开编译期固定网页版入口', () => {
  assert.match(guide, /webFallbackRequired === true/)
  assert.match(guide, /runtimeInfo\?\.webFallbackConfigured/)
  assert.match(guide, /desktopInvoke\('open_web_fallback'\)/)
  assert.match(guide, /v-if="!processorReady && bindingAttempted"/)
  assert.match(guide, /先由云端接管，稍后重试/)
  assert.doesNotMatch(guide, /window\.open|https?:\/\//)
})
