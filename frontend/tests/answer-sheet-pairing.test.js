import test from 'node:test'
import assert from 'node:assert/strict'
import {
  pairFilesByFolder,
  pairFilesBySide,
  parseSideFilename,
  sourceDirectory,
  studentPageIdentityHint,
} from '../src/utils/answerSheetPairing.js'
import {
  body,
  chooseFiles,
  flush,
  installImageDecoding,
  openAnswerSheet,
  pngFile,
  sessionFixture,
} from './helpers/answerSheetPage.js'
import { loadApiClient } from './helpers/apiClient.js'
import { sseResponse } from './helpers/memoryFetch.js'

const file = (name) => ({ name, path: name })
const byPath = (item) => item.path

test('文件名正反面标记与后端解析规则一致', () => {
  const cases = [
    ['张三_正面.jpg', { base: '张三', side: 'front' }],
    ['张三-反面.png', { base: '张三', side: 'back' }],
    ['张三背面.ocr-ready-v1.jpg', { base: '张三', side: 'back' }],
    ['张三 正.jpg', { base: '张三', side: 'front' }],
    ['张三（反）.jpg', { base: '张三', side: 'back' }],
    ['【张三】背.jpg', { base: '张三', side: 'back' }],
    ['2021001_Front.JPG', { base: '2021001', side: 'front' }],
    ['张三back.jpg', { base: '张三', side: 'back' }],
    ['王正正面.jpg', { base: '王正', side: 'front' }],
    ['dir/sub\\李四_反.jpg', { base: '李四', side: 'back' }],
  ]
  for (const [name, expected] of cases) assert.deepEqual(parseSideFilename(name), expected, name)
  for (const name of ['张三.jpg', '张三_1.jpg', 'feedback.jpg', 'scan_front_001.jpg', '张三_正面(1).jpg', '正面.jpg', '_反.jpg', '', null]) {
    assert.equal(parseSideFilename(name), null, String(name))
  }
})

test('只有全部文件两两成对时才按文件名排序为正面在前', () => {
  const ordered = pairFilesBySide(['李四_反面.jpg', '张三_正面.jpg', '李四_正面.jpg', '张三_反面.jpg'].map(file))
  assert.deepEqual(ordered.map((item) => item.name), ['李四_正面.jpg', '李四_反面.jpg', '张三_正面.jpg', '张三_反面.jpg'])
  for (const names of [
    ['张三_正面.jpg', '张三_反面.jpg', '李四.jpg', '李四_反面.jpg'],
    ['张三_正面.jpg', '张三_正.jpg'],
    ['张三_正面.jpg', '李四_反面.jpg'],
    ['张三_正面.jpg'],
    [],
  ]) {
    assert.equal(pairFilesBySide(names.map(file)), null, names.join(','))
  }
})

test('每个文件夹恰有两张图片时按文件夹配对，与后端文件夹规则一致', () => {
  assert.equal(sourceDirectory('收集.zip/张三/a.jpg'), '收集.zip/张三')
  assert.equal(sourceDirectory('张三\\a.jpg'), '张三')
  assert.equal(sourceDirectory('a.jpg'), '')
  const ordered = pairFilesByFolder([
    '收集.zip/李四/b.jpg', '收集.zip/张三/IMG_2.jpg', '收集.zip/李四/a.jpg', '收集.zip/张三/IMG_10.jpg',
  ].map(file), byPath)
  assert.deepEqual(ordered.map(byPath), [
    '收集.zip/李四/a.jpg', '收集.zip/李四/b.jpg', '收集.zip/张三/IMG_2.jpg', '收集.zip/张三/IMG_10.jpg',
  ])
  assert.deepEqual(pairFilesByFolder(['张三/a.jpg', '张三/b.jpg'].map(file), byPath).map(byPath), ['张三/a.jpg', '张三/b.jpg'])
  for (const paths of [
    ['收集.zip/张三/a.jpg', '收集.zip/张三/b.jpg', '收集.zip/张三/c.jpg', '收集.zip/李四/a.jpg'],
    ['收集.zip/张三/a.jpg', '收集.zip/张三/b.jpg', '李四_正面.jpg', '李四_反面.jpg'],
    ['张三_正面.jpg', '张三_反面.jpg'],
    [],
  ]) {
    assert.equal(pairFilesByFolder(paths.map(file), byPath), null, paths.join(','))
  }
})

test('页面显示身份来源，按顺序沿用的背面提示老师核对', () => {
  const pages = [{ paper_id: 'student_1', filename: 'scan_001.ocr-ready-v1.jpg', source_relative_path: '扫描/scan_001.jpg' }]
  assert.deepEqual(
    studentPageIdentityHint({ identity: { status: 'ordered_pair_inherited', student_name: '张三', source_paper_id: 'student_1' }, pairing: { status: 'identity_inherited' } }, pages),
    { text: '身份：张三（按上传顺序沿用上一页正面（扫描/scan_001.jpg），请核对是否同一学生）', type: 'warning' },
  )
  assert.equal(studentPageIdentityHint({ identity: { status: 'path_paired', source: 'path', clue_source: 'folder', student_name: '张三' } }).text, '身份：张三（文件夹名确认）')
  assert.equal(studentPageIdentityHint({ identity: { status: 'path_paired', source: 'path', clue_source: 'filename', student_name: '张三' } }).text, '身份：张三（文件名确认）')
  assert.equal(studentPageIdentityHint({ identity: { status: 'path_paired', source: 'paired_page', source_paper_id: 'student_1', student_name: '张三' } }, pages).text, '身份：张三（沿用同组另一面（扫描/scan_001.jpg））')
  assert.equal(studentPageIdentityHint({ identity: { status: 'recognized', student_name: '李四' } }).text, '身份：李四（本页识别）')
  assert.equal(studentPageIdentityHint({ identity: { status: 'teacher_confirmed', student_name: '李四' } }).text, '身份：李四（老师确认）')
  assert.deepEqual(studentPageIdentityHint({ identity: { status: 'needs_review' } }), { text: '身份：待老师确认', type: 'warning' })
  assert.deepEqual(
    studentPageIdentityHint({ identity: { status: 'path_paired', student_name: '张三' }, pairing: { status: 'page_type_mismatch' } }),
    { text: '身份：待老师确认；正反面与模板页型或文件名标记不一致', type: 'danger' },
  )
  assert.deepEqual(
    studentPageIdentityHint({ identity: { status: 'recognized', student_name: '李四' }, pairing: { status: 'identity_conflict' } }),
    { text: '身份：李四（本页识别）；同组两页身份不一致，请分别确认', type: 'danger' },
  )
})

const S1 = '/answer-sheet/sessions/s1'
const zipFile = name => new File([new Uint8Array([0x50, 0x4b, 3, 4, 0, 0, 0, 0])], name, { type: 'application/zip' })
const studentPicker = sheet => sheet.picker('导入学生答题卡')
const orderList = sheet => sheet.root.byClass('student-order-list').byTag('li')
const orderNames = sheet => orderList(sheet).map(item => item.byClass('student-order-name').text)
const sideTags = sheet => orderList(sheet).map(item => item.byTag('el-tag')[0].text)
const orderHead = sheet => sheet.root.byClass('student-order-head')

// A frozen layout whose student upload the server accepts page by page.
function studentStage(t, overrides = {}) {
  const upload = { upload_id: 'u1', status: 'uploading', uploaded_count: 0, processed_count: 0, total_bytes: 0 }
  const frozen = (extra = {}) => sessionFixture({ status: 'frozen', answer_source: 'separate', ...overrides, ...extra })
  return openAnswerSheet(t, {
    session: frozen(),
    respond: (config) => {
      if (config.method !== 'post') return undefined
      if (config.url === `${S1}/students/upload/start` || config.url === `${S1}/students/upload`) {
        return { data: frozen({ student_upload: upload }) }
      }
      return undefined
    },
    stream: () => sseResponse([['done', { session: frozen() }]]),
  })
}

test('学生答题卡支持文件夹与压缩包导入，并随每页上传来源路径', async (t) => {
  installImageDecoding(t)
  const sheet = await studentStage(t)

  // Only the student picker imports folders and archives; the template and source pickers take images.
  const pickers = sheet.pickers()
  assert.equal(pickers.length, 4)
  const importing = pickers.filter(el => el.classList.contains('file-picker-import'))
  assert.deepEqual(importing.map(el => el.byTag('strong')[0].text), ['导入学生答题卡'])
  const [filesInput, folderInput] = studentPicker(sheet).byTag('input')
  assert.equal(filesInput.getAttribute('accept'), '.jpg,.jpeg,.png,.zip,.rar')
  assert.equal(filesInput.multiple, true)
  assert.ok(folderInput.hasAttribute('webkitdirectory'))
  for (const picker of pickers.filter(el => !importing.includes(el))) {
    const inputs = picker.byTag('input')
    assert.deepEqual(inputs.map(input => [input.getAttribute('accept'), input.hasAttribute('webkitdirectory')]), [['image/jpeg,image/png', false]])
  }

  // A ZIP goes to the archive extractor (this runtime has no unpacking worker);
  // an image-only picker refuses it as a file type (checked last, below).
  await chooseFiles(filesInput, [zipFile('收集.zip')])
  assert.equal(sheet.messages().length, 1)
  assert.equal(sheet.messages()[0][0], 'error')
  assert.match(sheet.messages()[0][1], /^\[IMPORT_DECODER_UNAVAILABLE\] 收集\.zip：/)
  sheet.client.messages.length = 0

  // Dropping images works like choosing them.
  const picker = studentPicker(sheet)
  let prevented = false
  await picker.dispatch('drop', { preventDefault: () => { prevented = true }, dataTransfer: { items: [], files: [pngFile('a.png'), pngFile('b.png')] } })
  await flush(10)
  assert.equal(prevented, true, '拖入时不让浏览器打开文件')
  assert.equal(picker.byTag('strong')[0].text, '已选择 2 个文件')

  // A folder keeps each image's path through preprocessing, into every upload.
  await chooseFiles(folderInput, [
    pngFile('2.png', { path: '收集/张三/2.png' }),
    pngFile('1.png', { path: '收集/李四/1.png' }),
    pngFile('1.png', { path: '收集/张三/1.png' }),
    pngFile('2.png', { path: '收集/李四/2.png' }),
  ])
  assert.deepEqual(sheet.messages(), [])
  assert.deepEqual(orderNames(sheet), ['收集/李四/1.png', '收集/李四/2.png', '收集/张三/1.png', '收集/张三/2.png'])
  await sheet.click('上传并处理')
  const uploads = sheet.client.requests.filter(config => config.method === 'post' && config.url === `${S1}/students/upload`)
  assert.deepEqual(uploads.map(config => config.data.get('source_path')), ['收集/李四/1.png', '收集/李四/2.png', '收集/张三/1.png', '收集/张三/2.png'])
  assert.deepEqual(uploads.map(config => config.data.get('files').name), ['1.ocr-ready-v1.jpg', '2.ocr-ready-v1.jpg', '1.ocr-ready-v1.jpg', '2.ocr-ready-v1.jpg'])
  assert.deepEqual(body(sheet.request('post', `${S1}/students/upload/start`)), { restart: false })
  sheet.close()

  const draft = await openAnswerSheet(t, { session: sessionFixture({ status: 'draft' }) })
  await chooseFiles(draft.picker('选择原始试卷图片').byTag('input')[0], [zipFile('收集.zip')])
  assert.deepEqual(draft.messages(), [['warning', '仅支持 JPG、JPEG 和 PNG 图片']])
})

test('学生答题卡上传请求附带每页来源路径，未给路径时不发送该字段', async () => {
  const client = await loadApiClient()
  const page = new File(['front'], 'a.jpg', { type: 'image/jpeg' })
  await client.api.answerSheet.uploadStudents('s1', 'u1', page, '收集.zip/张三/a.jpg')
  await client.api.answerSheet.uploadStudents('s1', 'u1', page)

  const [withPath, withoutPath] = client.requests.map(config => config.data)
  assert.equal(withPath.get('source_path'), '收集.zip/张三/a.jpg')
  assert.equal(withPath.get('files'), page)
  assert.equal(withoutPath.has('source_path'), false)
  assert.equal(withoutPath.get('files'), page)
})

test('上传区默认提醒文件夹、文件名配对或按顺序上传正反面，并在页面卡片显示身份来源', async (t) => {
  installImageDecoding(t)
  const pages = [
    { paper_id: 'student_1', filename: 'scan_001.ocr-ready-v1.jpg', source_relative_path: '扫描/scan_001.jpg', identity: { status: 'recognized', student_name: '张三' } },
    { paper_id: 'student_2', filename: 'scan_002.ocr-ready-v1.jpg', identity: { status: 'ordered_pair_inherited', student_name: '张三', source_paper_id: 'student_1' }, pairing: { status: 'identity_inherited' } },
  ]
  const sheet = await studentStage(t, { student_pages: pages })
  assert.match(sheet.root.byClass('student-pairing-tip').getAttribute('description'), /每位学生一个文件夹/)
  const [filesInput] = studentPicker(sheet).byTag('input')
  const choose = files => chooseFiles(filesInput, files)
  const head = () => [orderHead(sheet).byTag('el-tag')[0].text, orderHead(sheet).byTag('span')[0].text]
  const inFolder = path => pngFile(path.split('/').pop(), { path })

  // No folders and no side marks: sorted by capture sequence, sides follow the order.
  await choose(['scan_003.png', 'scan_001.png', 'scan_004.png', 'scan_002.png'].map(name => pngFile(name)))
  // The list shows each page by the name the teacher picked, not the preprocessed copy's.
  assert.deepEqual(orderNames(sheet), ['scan_001.png', 'scan_002.png', 'scan_003.png', 'scan_004.png'])
  assert.equal(head()[0], '文件名序号')
  assert.match(head()[1], /文件名不能确定身份时，请按顺序上传同一学生的正面和背面（正面在前）。$/)
  assert.deepEqual(sideTags(sheet), ['正面', '背面', '正面', '背面'])

  // Every file marked front or back, in pairs: paired by name, tagged by the mark.
  await choose(['张三_反面.png', '李四_正面.png', '张三_正面.png', '李四_反面.png'].map(name => pngFile(name)))
  assert.equal(head()[0], '文件名正反面')
  assert.match(head()[1], /^系统已按文件名中的正面\/反面标记排序。文件名均带正面\/反面标记且两两成对/)
  assert.deepEqual(orderNames(sheet).map((name, index) => [name, sideTags(sheet)[index]]), [
    ['李四_正面.png', '正面'],
    ['李四_反面.png', '背面'],
    ['张三_正面.png', '正面'],
    ['张三_反面.png', '背面'],
  ])

  // Folders that are not all pairs: grouped by folder, each in capture order.
  await choose(['收集/张三/3.png', '收集/李四/1.png', '收集/张三/1.png', '收集/张三/2.png'].map(inFolder))
  assert.equal(head()[0], '文件夹分组')
  assert.match(head()[1], /^系统已按文件夹分组，文件夹内按文件序号或时间排序；请核对下面列表。/)
  assert.deepEqual(orderNames(sheet), ['收集/李四/1.png', '收集/张三/1.png', '收集/张三/2.png', '收集/张三/3.png'])
  assert.deepEqual(sideTags(sheet), ['正面', '背面', '正面', '背面'])

  // A folder per student holding two pages: the template page type decides the side.
  await choose(['收集/张三/b.png', '收集/张三/a.png', '收集/李四/a.png', '收集/李四/b.png'].map(inFolder))
  assert.equal(head()[0], '文件夹配对')
  assert.match(head()[1], /^系统已按文件夹排序。每个文件夹恰有两张图片，系统按文件夹配对正反面/)
  assert.deepEqual(orderNames(sheet), ['收集/李四/a.png', '收集/李四/b.png', '收集/张三/a.png', '收集/张三/b.png'])
  assert.deepEqual(sideTags(sheet), ['按页型区分', '按页型区分', '按页型区分', '按页型区分'])

  // Each uploaded page card says where its identity came from.
  const hints = sheet.root.allByClass('student-identity-hint')
  assert.deepEqual(hints.map(el => [el.text, el.getAttribute('type')]), [
    ['身份：张三（本页识别）', 'success'],
    ['身份：张三（按上传顺序沿用上一页正面（扫描/scan_001.jpg），请核对是否同一学生）', 'warning'],
  ])
})
