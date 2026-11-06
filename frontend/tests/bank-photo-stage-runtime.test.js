import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { mountComponent } from './helpers/mountComponent.js'
import { chooseFiles, click, deferred, options, pageStandIns, pick, select, serve, signedInTeacher } from './helpers/v1ExerciseBank.js'

// A photo-import batch keeps the stage it was recognised for; starting over
// (clearing it, or recognising new photos) goes back to the teacher's own stage.
const { default: BankPhotoImport } = await import('../src/views/BankPhotoImport.vue?sfc')

const SAVED_IMPORT = 'mathagent-bank-import:7'

// A junior teacher returning to the page while an older senior batch is saved.
async function openRestoredSeniorBatch(t, chapters) {
  const client = await signedInTeacher({
    user: { id: 7, education_stage: 'junior' },
    localStorage: { [SAVED_IMPORT]: 'old' },
  })
  const backend = serve(client, {
    'get /bank/photo-import/old': () => ({ import_id: 'old', teaching_context: { education_stage: 'senior' }, paired: [] }),
    'get /teacher/chapters': ({ params }) => chapters(params),
    'post /bank/photo-import/recognize': () => ({ import_id: 'new', paired: [], teaching_context: { education_stage: 'junior' } }),
  })
  const page = await mountComponent(t, BankPhotoImport, { path: '/bank/photo-import', plugins: [pageStandIns] })
  return { page, root: page.root, client, backend }
}

const chapterSelect = root => select(root, '选择本学段章节（可不选）')
const batchStage = root => root.byText('el-tag', '本批次已固定')?.text
const photo = () => new File(['synthetic-only'], 'question.png', { type: 'image/png' })

test('clearing a restored senior batch clears its chapter and loads current junior catalog', async t => {
  const { page, root, client, backend } = await openRestoredSeniorBatch(t, async params => [{ chapter: `${params.education_stage}-chapter` }])
  assert.equal(batchStage(root), '高中数学 · 本批次已固定')
  assert.deepEqual(options(chapterSelect(root)), ['senior-chapter'])
  await pick(chapterSelect(root), 'senior-chapter')
  await click(root, 'el-button', '清空')
  assert.equal(chapterSelect(root).props.modelValue, '')
  assert.equal(batchStage(root), undefined)
  assert.equal(client.localStorage.getItem(SAVED_IMPORT), null)
  assert.deepEqual(options(chapterSelect(root)), ['junior-chapter'])
  assert.deepEqual(backend.calls('get /teacher/chapters').map(call => call.params.education_stage), ['senior', 'junior'])
  // The junior catalog is the one loaded: new photos are recognised against it as it is.
  await chooseFiles(root, '题干图', [photo()])
  await click(root, 'el-button', '识别并配对')
  assert.deepEqual(backend.keys().slice(-2), ['get /teacher/chapters', 'post /bank/photo-import/recognize'])
  assert.equal(backend.calls('get /teacher/chapters').length, 2)
  assert.deepEqual(page.errors, [])
})

test('late older catalog request cannot replace a fresh reset catalog', async t => {
  const old = deferred(), fresh = deferred()
  const { page, root, backend } = await openRestoredSeniorBatch(t, params => (params.education_stage === 'senior' ? old.promise : fresh.promise))
  assert.deepEqual(backend.calls('get /teacher/chapters').map(call => call.params.education_stage), ['senior'])
  const reset = click(root, 'el-button', '清空')
  fresh.resolve([{ chapter: 'junior-chapter' }])
  await reset
  old.resolve([{ chapter: 'senior-chapter' }])
  await page.flush()
  assert.deepEqual(options(chapterSelect(root)), ['junior-chapter'])
  // The page holds the junior catalog as loaded: recognition does not fetch it again.
  await chooseFiles(root, '题干图', [photo()])
  await click(root, 'el-button', '识别并配对')
  assert.deepEqual(backend.calls('get /teacher/chapters').map(call => call.params.education_stage), ['senior', 'junior'])
  assert.equal(backend.calls('post /bank/photo-import/recognize').length, 1)
})

test('starting new recognition after restoring old stage drops incompatible chapter', async t => {
  const { root, backend } = await openRestoredSeniorBatch(t, async params => [{ chapter: `${params.education_stage}-chapter` }])
  await pick(chapterSelect(root), 'senior-chapter')
  await chooseFiles(root, '题干图', [photo()])
  await click(root, 'el-button', '识别并配对')
  const recognized = backend.calls('post /bank/photo-import/recognize')
  assert.equal(recognized.length, 1)
  assert.equal(recognized[0].data.get('chapter'), null)
  assert.equal(recognized[0].data.getAll('question_files').length, 1)
  // The junior catalog was loaded before the upload went out.
  assert.deepEqual(backend.keys(), [
    'get /bank/photo-import/old', 'get /teacher/chapters', 'get /teacher/chapters', 'post /bank/photo-import/recognize',
  ])
  assert.deepEqual(backend.calls('get /teacher/chapters').map(call => call.params.education_stage), ['senior', 'junior'])
  assert.equal(chapterSelect(root).props.modelValue, '')
  assert.deepEqual(options(chapterSelect(root)), ['junior-chapter'])
})
