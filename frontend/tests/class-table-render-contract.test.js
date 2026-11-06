import './helpers/srcModules.js'
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { loadApiClient } from './helpers/apiClient.js'
import { mountComponent } from './helpers/mountComponent.js'

const { default: ClassTable } = await import('../src/views/ClassTable.vue?sfc')

const TEACHER = { id: 1, role: 'teacher', education_stage: 'junior', profile_complete: true }
const STUDENTS = [
  { id: 11, name: '王小明', student_no: '01', risk_level: 'attention', wrong_rate: 0.4, correction_rate: 0.2, weakness_count: 2 },
  { id: 12, name: '李华', student_no: '02', observation_mode: true, risk_level: 'observation' },
]

// The class page of a signed-in teacher whose class 3 the backend lists with
// `students` and `coverage` (each answer to /class/3/students takes the next
// roster in `rosters`, the last one repeating).
async function openClassPage(t, rosters = [{ students: STUDENTS, coverage: { class_size: 2, effective_students: 1, observation_students: 1 } }]) {
  const client = await loadApiClient({
    localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(TEACHER) },
  })
  let served = 0
  client.respond((config) => {
    if (config.url === '/auth/classes') {
      return { data: { classes: [{ id: 3, class_name: '七年级一班' }], current_class_id: 3, max_classes: 4 } }
    }
    if (config.url === '/class/3/students') {
      const roster = rosters[Math.min(served, rosters.length - 1)]
      served += 1
      return { data: { class_name: '七年级一班', ...roster } }
    }
    return { data: {} }
  })
  const page = await mountComponent(t, ClassTable, { path: '/class/3/students' })
  return { client, page }
}

const coverageTag = page => page.root.find(el => el.tagName === 'el-tag' && el.text.startsWith('有效统计'))

test('班级管理页导入并使用 reactive，避免 setup 初始化时整页白屏', async (t) => {
  const { page } = await openClassPage(t, [
    { students: STUDENTS, coverage: { class_size: 2, effective_students: 1, observation_students: 1 } },
    { students: [STUDENTS[0]], coverage: { class_size: 1, effective_students: 1, observation_students: 0 } },
  ])
  // setup ran to the end: the page rendered its header and the loaded class.
  assert.deepEqual(page.errors, [])
  assert.equal(page.root.find(el => el.tagName === 'h1').text, '七年级一班')
  assert.equal(page.root.byClass('class-count').text, '2 名学生')
  // The coverage figures are reactive state: the loaded numbers render, and a
  // reload with other numbers replaces them.
  assert.equal(coverageTag(page).text, '有效统计 1 / 2 · 观察 1')
  await page.root.byText('el-button', '刷新').dispatch('click')
  await page.flush()
  assert.equal(page.root.byClass('class-count').text, '1 名学生')
  assert.equal(coverageTag(page), null, 'no observation students: the coverage tag is gone')
})

test('添加学生时提示同名学生填写学号确认身份', async (t) => {
  const { page } = await openClassPage(t)
  const dialog = () => page.root.find(el => el.tagName === 'el-dialog' && el.getAttribute('title') === '添加学生')
  const tip = () => dialog().find(el => el.tagName === 'el-alert' && el.classList.contains('student-identity-tip'))
  assert.equal(dialog().getAttribute('data-open'), 'false')
  assert.equal(tip(), null)

  await page.root.find(el => el.tagName === 'el-button' && el.getAttribute('data-tour') === 'student-add').dispatch('click')
  await page.flush()
  assert.equal(dialog().getAttribute('data-open'), 'true')
  assert.equal(tip().getAttribute('title'), '如果班级中有同名学生，请填写学号，以便准确确定身份。')
})

test('操作列给四个按钮预留完整宽度并固定在表格右侧', async (t) => {
  const { page } = await openClassPage(t)
  const column = page.root.find(el => el.tagName === 'el-table-column' && el.getAttribute('label') === '操作')
  assert.equal(column.getAttribute('width'), '300')
  assert.equal(column.getAttribute('fixed'), 'right')
  // Every row gets all four actions in one group.
  const actions = column.byTag('el-cell').map(cell => cell.byClass('action-btns').byTag('el-button').map(button => button.text))
  assert.deepEqual(actions, [
    ['详情', '编辑', '设为观察', '移出'],
    ['详情', '编辑', '恢复统计', '移出'],
  ])
})

// Layout only: whether the four buttons stay on one line needs a browser's
// layout engine, so these style rules are still checked as written.
test('操作列按钮不换行并消除 Element Plus 的重复左边距', () => {
  const source = readFileSync(new URL('../src/views/ClassTable.vue', import.meta.url), 'utf8')
  assert.match(source, /\.action-btns\s*\{[^}]*white-space:\s*nowrap;/s)
  assert.match(source, /\.action-btns :deep\(\.el-button\)\s*\{[^}]*flex:\s*0 0 auto;[^}]*margin-left:\s*0;/s)
})
