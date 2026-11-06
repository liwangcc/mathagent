import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { assertAbsent, signedInTeacher } from './helpers/homeGuideLayout.js'
import { mountComponent } from './helpers/mountComponent.js'

// Every teacher page, and where the app serves it.
const teacherViews = {
  'Home.vue': [() => import('../src/views/Home.vue?sfc'), '/'],
  'Upload.vue': [() => import('../src/views/Upload.vue?sfc'), '/upload'],
  'AnswerSheet.vue': [() => import('../src/views/AnswerSheet.vue?sfc'), '/answer-sheet'],
  'Exercise.vue': [() => import('../src/views/Exercise.vue?sfc'), '/exercise'],
  'ClassTable.vue': [() => import('../src/views/ClassTable.vue?sfc'), '/students'],
  'Students.vue': [() => import('../src/views/Students.vue?sfc'), '/students/legacy'],
  'Bank.vue': [() => import('../src/views/Bank.vue?sfc'), '/bank'],
  'BankPhotoImport.vue': [() => import('../src/views/BankPhotoImport.vue?sfc'), '/bank/photo-import'],
  'GradeRecords.vue': [() => import('../src/views/GradeRecords.vue?sfc'), '/grade-records'],
  'Reports.vue': [() => import('../src/views/Reports.vue?sfc'), '/reports'],
  'Stats.vue': [() => import('../src/views/Stats.vue?sfc'), '/stats'],
  'Settings.vue': [() => import('../src/views/Settings.vue?sfc'), '/settings'],
  'Profile.vue': [() => import('../src/views/Profile.vue?sfc'), '/profile'],
  'PendingAnswers.vue': [() => import('../src/views/PendingAnswers.vue?sfc'), '/pending-answers'],
  'StudentDetail.vue': [() => import('../src/views/StudentDetail.vue?sfc'), '/student/5/detail'],
}

// The help buttons a mounted page renders: the button a teacher clicks and
// the popover it opens.
function helpButtons(root) {
  return root.findAll(el => el.tagName === 'el-popover' && el.find(child => child.classList.contains('help-button')))
    .map(popover => ({
      popover,
      button: popover.find(el => el.classList.contains('help-button')),
      content: popover.byClass('local-help-content'),
    }))
}

test('local help button is accessible and renders a concise popover', async (t) => {
  const { default: HelpButton } = await import('../src/components/HelpButton.vue?sfc')
  const page = await mountComponent(t, HelpButton, {
    props: { title: '答案依据', content: '必须先导入并确认模板。', items: ['先导入模板', '再核对答案'] },
  })
  const [help] = helpButtons(page.root)
  assert.ok(help, 'a help button inside a popover')
  assert.equal(help.popover.getAttribute('trigger'), 'click', 'opens on click, not on hover')
  assert.equal(help.button.tagName, 'button')
  assert.equal(help.button.getAttribute('type'), 'button')
  assert.equal(help.button.getAttribute('aria-label'), '查看答案依据')
  assert.equal(help.button.getAttribute('title'), '查看答案依据')
  assert.equal(help.content.byClass('local-help-title').text, '答案依据')
  assert.equal(help.content.byClass('local-help-description').text, '必须先导入并确认模板。')
  assert.deepEqual(help.content.byTag('li').map(item => item.text), ['先导入模板', '再核对答案'])

  // Clicking the help button does not also click the row or card around it.
  let reachedParent = false
  page.root.addEventListener('click', () => { reachedParent = true })
  await help.button.dispatch('click')
  assert.equal(reachedParent, false)
})

test('local help without content or items renders only its title', async (t) => {
  const { default: HelpButton } = await import('../src/components/HelpButton.vue?sfc')
  const page = await mountComponent(t, HelpButton, { props: { title: '试卷栏数' } })
  const [help] = helpButtons(page.root)
  assert.equal(help.button.getAttribute('aria-label'), '查看试卷栏数')
  assert.equal(help.content.byClass('local-help-title').text, '试卷栏数')
  assertAbsent(help.content.byClass('local-help-description'))
  // Count, not deepEqual: a failing diff would print the memory tree.
  assert.equal(help.content.byTag('ul').length, 0, 'no item list without items')
})

for (const [view, [load, path]] of Object.entries(teacherViews)) {
  test(`${view} exposes local help for teacher functions`, async (t) => {
    const client = await signedInTeacher({ user: { id: 7, role: 'admin', name: '林老师', education_stage: 'senior', profile_complete: true }, storage: { disclaimer_agreed: 'true' } })
    client.respond(() => ({ data: { items: [], students: [], classes: [], total: 0 } }))
    const { default: View } = await load()
    const page = await mountComponent(t, View, { path })
    const helps = helpButtons(page.root)
    assert.ok(helps.length > 0, `${view} renders no local help button`)
    for (const { popover, button, content } of helps) {
      const title = content.byClass('local-help-title').text
      assert.ok(title, 'each help names what it explains')
      assert.equal(button.getAttribute('aria-label'), `查看${title}`)
      assert.equal(popover.getAttribute('trigger'), 'click')
    }
  })
}
