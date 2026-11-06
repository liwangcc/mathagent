// The real 答题卡批阅 page (frontend/src/views/AnswerSheet.vue) mounted for a
// test, with a saved session it restores, for browser-image-offload and
// text-grading-method.
//
//   const sheet = await mountAnswerSheet(t, { session: { session_id: 's1', status: 'review', ... } })
//   await sheet.page.root.byText('el-button', '保存人工判定').dispatch('click')
//   await sheet.settle()
//   sheet.body('POST /api/answer-sheet/sessions/s1/grading/decision')   // the JSON sent
//
// The page restores the session saved in localStorage when it mounts; its
// other mount requests (service availability, template history, batch
// choices) get a healthy, empty answer. `responses` maps 'METHOD /api/path'
// (no query string) to the data (status 200) or to config => ({ status,
// data }); any other route answers 200 {}. The rest of the options go to
// mountComponent.
//
// Returns { page, client, settle, answer(route, value), requests(route),
// body(route, n) }: page is helpers/mountComponent.js's, client
// helpers/apiClient.js's; requests are the axios configs sent to a route.
import './srcModules.js'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'
import { loadApiClient } from './apiClient.js'
import { mountComponent } from './mountComponent.js'

const { default: AnswerSheet } = await import('../../src/views/AnswerSheet.vue?sfc')
const { useAuthStore } = await import('../../src/stores/auth.js')

export const SHEET_TEACHER = Object.freeze({ id: 7, name: '测试老师', education_stage: 'junior', current_class_id: 1 })

const routeOf = config => `${String(config.method || 'GET').toUpperCase()} /api${String(config.url).split('?')[0]}`

export async function mountAnswerSheet(t, { session, responses = {}, user = SHEET_TEACHER, ...mountOptions } = {}) {
  const client = await loadApiClient({
    localStorage: {
      mathagent_token: 'teacher-token',
      mathagent_user: JSON.stringify(user),
      ...(session ? { mathagent_answer_sheet_session_id: session.session_id } : {}),
    },
  })
  const answers = {
    'GET /api/answer-sheet/availability': { status: 'available', message: '' },
    'GET /api/answer-sheet/templates/history': { templates: [] },
    'GET /api/answer-sheet/paper-templates': { templates: [] },
    'GET /api/answer-sheet/paper-templates/sources': { sources: [] },
    'GET /api/answer-sheet/sessions': { batches: [] },
    ...(session ? { [`GET /api/answer-sheet/sessions/${session.session_id}`]: session } : {}),
    ...responses,
  }
  client.respond((config) => {
    const answer = answers[routeOf(config)]
    if (typeof answer === 'function') return answer(config)
    return { status: 200, data: answer === undefined ? {} : answer }
  })
  const pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.currentClassId = 1
  auth.classes = [{ id: 1, class_name: '班级 1' }]
  const page = await mountComponent(t, AnswerSheet, { path: '/answer-sheet', pinia, ...mountOptions })
  // Requests, re-renders and the page's own promise chains, without waiting
  // on timers.
  const settle = async (rounds = 12) => {
    for (let i = 0; i < rounds; i++) {
      await new Promise(resolve => setImmediate(resolve))
      await nextTick()
    }
  }
  await settle()
  const requests = route => client.requests.filter(config => routeOf(config) === route)
  return {
    page,
    client,
    settle,
    answer(route, value) { answers[route] = value },
    requests,
    body: (route, n = 0) => {
      const config = requests(route)[n]
      return config && JSON.parse(config.data)
    },
  }
}
