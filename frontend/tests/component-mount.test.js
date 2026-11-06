import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { computed, defineComponent, onMounted, ref } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import { loadApiClient } from './helpers/apiClient.js'
import { ElMessageBox } from './helpers/elementPlus.js'
import { flush, mountComponent } from './helpers/mountComponent.js'
import { installFetch, sseResponse } from './helpers/memoryFetch.js'

// What helpers/mountComponent.js gives a test: a real component, compiled as
// the development build compiles it, mounted with Vue's runtime and driven
// through what it renders.

test('a component imported with ?sfc is compiled for real, its own components too', async (t) => {
  const { default: GradingProgress } = await import('../src/components/GradingProgress.vue?sfc')
  assert.equal(GradingProgress.__file, 'components/GradingProgress.vue')
  const page = await mountComponent(t, GradingProgress, {
    props: { current: 3, total: 8, live: true, message: '正在批改第 3 份', elapsed: '1 分 05 秒' },
  })
  const track = page.root.find(el => el.getAttribute('role') === 'progressbar')
  assert.equal(track.getAttribute('aria-label'), '已完成 3 / 8 份')
  assert.equal(track.getAttribute('aria-valuenow'), '3')
  assert.equal(page.root.byClass('grading-progress__elapsed').text, '已用时 1 分 05 秒')
  assert.equal(page.root.byClass('grading-progress__copy').text, '正在批改第 3 份')
  // SheetLoader is compiled from its own file, not an empty stand-in.
  const loader = page.root.find(el => el.tagName === 'svg' && el.classList.contains('sheet-loader'))
  assert.equal(loader.style.width, '88px')
  assert.deepEqual(page.errors, [])
})

test('a component imported as it is stays an empty stand-in naming its file', async () => {
  const { default: Stub } = await import('../src/components/GradingProgress.vue')
  assert.deepEqual(Stub, { __file: 'components/GradingProgress.vue' })
})

test('Vite module conventions load: JSON imports and ?url assets', async () => {
  const { default: Home } = await import('../src/views/Home.vue?sfc')
  assert.equal(typeof Home.render, 'function')
  const { default: Cover } = await import('../src/components/CoursewareCover.vue?sfc')
  assert.equal(typeof Cover.setup, 'function')
})

const Counter = defineComponent({
  props: { start: { type: Number, default: 0 } },
  setup(props) {
    const count = ref(props.start)
    const name = ref('')
    const open = ref(false)
    const busy = ref(false)
    const rows = ref([{ id: 1, title: '一' }, { id: 2, title: '二' }])
    const doubled = computed(() => count.value * 2)
    return { count, name, open, busy, rows, doubled }
  },
  template: `
    <section>
      <button class="inc" @click="count++">+</button>
      <output class="count">{{ count }} / {{ doubled }}</output>
      <input class="name" v-model="name">
      <p v-if="name" class="greeting">你好，{{ name }}</p>
      <p v-show="count > 2" class="many">很多</p>
      <el-button class="save" :disabled="busy" @click="count += 10">保存</el-button>
      <el-dialog v-model="open" title="详情" @open="count = 100"><span class="inside">内容</span></el-dialog>
      <el-table :data="rows">
        <el-table-column label="标题"><template #default="{ row, $index }">{{ $index }}:{{ row.title }}</template></el-table-column>
      </el-table>
    </section>`,
})

test('the renderer: events re-render, v-model, v-if, v-show and setup state', async (t) => {
  const page = await mountComponent(t, Counter, { props: { start: 1 } })
  const count = () => page.root.byClass('count').text
  assert.equal(count(), '1 / 2')
  await page.root.byClass('inc').dispatch('click')
  await flush()
  assert.equal(count(), '2 / 4')
  assert.equal(page.root.byClass('many').style.display, 'none')
  page.setup.count = 3
  await flush()
  assert.notEqual(page.root.byClass('many').style.display, 'none')

  const input = page.root.byClass('name')
  assert.equal(page.root.byClass('greeting'), null)
  input.value = '王老师'
  await input.dispatch('input')
  await flush()
  assert.equal(page.setup.name, '王老师')
  assert.equal(page.root.byClass('greeting').text, '你好，王老师')
})

test('the element-plus stand-ins: buttons, dialogs and table rows', async (t) => {
  const page = await mountComponent(t, Counter)
  const save = page.root.byText('el-button', '保存')
  page.setup.busy = true
  await flush()
  await save.dispatch('click')
  assert.equal(page.setup.count, 0, 'a disabled button ignores the click')
  page.setup.busy = false
  await flush()
  await save.dispatch('click')
  assert.equal(page.setup.count, 10)

  const dialog = page.root.find(el => el.tagName === 'el-dialog')
  assert.equal(dialog.attributes.title, '详情')
  assert.equal(page.root.byClass('inside'), null, 'a closed dialog renders nothing')
  page.setup.open = true
  await flush()
  assert.equal(page.root.byClass('inside').text, '内容')
  assert.equal(page.setup.count, 100, 'opening emits open')
  await dialog.handlers['update:modelValue'](false)
  await flush()
  assert.equal(page.setup.open, false)

  assert.deepEqual(page.root.byTag('el-cell').map(cell => cell.text), ['0:一', '1:二'])
})

test('pages run inside the app routes: named routes resolve and leave guards run', async (t) => {
  const Page = defineComponent({
    setup() {
      const router = useRouter()
      const dirty = ref(true)
      onBeforeRouteLeave(() => !dirty.value || window.confirm('离开？'))
      return { dirty, toRecords: () => router.push({ name: 'grade-records' }) }
    },
    template: '<button class="go" @click="toRecords">记录</button>',
  })
  let answer = false
  const page = await mountComponent(t, Page, { path: '/upload', confirm: () => answer })
  await page.root.byClass('go').dispatch('click')
  await flush()
  assert.equal(page.router.currentRoute.value.name, 'upload')
  assert.deepEqual(page.win.confirmations, ['离开？'])
  answer = true
  await page.root.byClass('go').dispatch('click')
  await flush()
  assert.equal(page.router.currentRoute.value.name, 'grade-records')
})

test('message boxes are recorded and answered by the test', async (t) => {
  const Page = defineComponent({
    setup() {
      const result = ref('')
      const ask = () => ElMessageBox.confirm('删除？', '确认').then(() => { result.value = '删' }, (action) => { result.value = action })
      return { result, ask }
    },
    template: '<button class="ask" @click="ask">删除</button><span class="result">{{ result }}</span>',
  })
  const page = await mountComponent(t, Page, { messageBox: () => 'cancel' })
  await page.root.byClass('ask').dispatch('click')
  await flush()
  assert.equal(page.root.byClass('result').text, 'cancel')
  assert.deepEqual(page.boxes.map(box => [box.kind, box.message, box.title]), [['confirm', '删除？', '确认']])
})

test('errors in a component fail the mount instead of rendering nothing', async (t) => {
  const Broken = defineComponent({
    setup() { throw new Error('setup exploded') },
    template: '<p>never</p>',
  })
  await assert.rejects(mountComponent(t, Broken), /mounting component failed in setup function: setup exploded/)
})

test('API calls go through the real client to the in-memory backend', async (t) => {
  const client = await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  client.respond(() => ({ data: { used: 3, limit: 50 } }))
  const { default: api } = await import('../src/api/index.js')
  const Page = defineComponent({
    setup() {
      const usage = ref(null)
      onMounted(async () => { usage.value = await api.usage.check() })
      return { usage }
    },
    template: '<p class="usage" v-if="usage">{{ usage.used }} / {{ usage.limit }}</p>',
  })
  const page = await mountComponent(t, Page)
  assert.equal(page.root.byClass('usage').text, '3 / 50')
  assert.deepEqual(client.requests.map(request => [request.method, request.url]), [['get', '/usage/check']])
  assert.equal(client.requests[0].headers.Authorization, 'Bearer teacher-token')
})

test('streaming calls go through the client to an in-memory fetch', async (t) => {
  await loadApiClient({ localStorage: { mathagent_token: 'teacher-token' } })
  const backend = installFetch(t, () => sseResponse([['progress', { current: 1 }], ['done', { batch_id: 'b1' }]]))
  const { default: api } = await import('../src/api/index.js')
  const response = await api.grade.recognitionResume('b1')
  assert.match(await response.text(), /event: done\ndata: \{"batch_id":"b1"\}/)
  assert.equal(backend.requests[0].url, '/api/batch-grade/b1/recognize/resume')
  assert.equal(backend.requests[0].init.headers.Authorization, 'Bearer teacher-token')
})
