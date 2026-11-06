import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'
import { mountComponent } from './helpers/mountComponent.js'

const { default: StudentDetail } = await import('../src/views/StudentDetail.vue?sfc')

const TEACHER = { id: 1, role: 'teacher', education_stage: 'junior', profile_complete: true }

const detail = wrongQuestions => ({
  student: { id: 7, name: '王小明', student_no: '01', class_name: '七年级一班', class_id: 3 },
  has_records: true,
  graded_count: 2,
  wrong_questions: wrongQuestions,
})

test('student detail distinguishes a load fault from an empty wrong-question history', async (t) => {
  const client = await loadApiClient({
    localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(TEACHER) },
  })
  let answer = () => ({ data: detail([{ question_no: '3', question: '解方程 x + 1 = 2', wrong_count: 2, status: 'pending', cycle_no: 1 }]) })
  client.respond(config => (config.url === '/student/7/detail' ? answer(config) : { data: {} }))
  const page = await mountComponent(t, StudentDetail, { path: '/student/7/detail' })

  const errorAlert = () => page.root.find(el => el.tagName === 'el-alert' && el.classList.contains('load-error'))
  // The wrong-question table lists each question by its number on the paper.
  const questionNumbers = () => page.root.find(el => el.tagName === 'el-table-column' && el.getAttribute('label') === '原题号')
    .byTag('el-cell').map(cell => cell.text)
  assert.deepEqual(client.requests.map(request => [request.url, request.params]), [['/student/7/detail', { education_stage: 'junior' }]])
  assert.deepEqual(questionNumbers(), ['3'])
  assert.equal(errorAlert(), null)

  // A refresh that fails says so and keeps the last snapshot on screen, rather
  // than showing an empty wrong-question history.
  answer = () => ({ status: 500, data: { detail: '服务器暂时不可用' } })
  await page.root.byText('el-button', '刷新').dispatch('click')
  await page.flush()
  assert.equal(errorAlert().byClass('load-error-content').byTag('span')[0].text, '学生详情加载失败，当前数据可能不是最新，请重试。')
  assert.deepEqual(questionNumbers(), ['3'])
  assert.equal(page.root.find(el => el.tagName === 'h2').text, '王小明')

  // Retrying clears the fault once the detail loads again.
  answer = () => ({ data: detail([]) })
  await errorAlert().byText('el-button', '重试').dispatch('click')
  await page.flush()
  assert.equal(errorAlert(), null)
  assert.deepEqual(questionNumbers(), [])
  assert.equal(client.requests.filter(request => request.url === '/student/7/detail').length, 3)
})
