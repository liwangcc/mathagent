import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  pairFilesByFolder,
  pairFilesBySide,
  parseSideFilename,
  sourceDirectory,
  studentPageIdentityHint,
} from '../src/utils/answerSheetPairing.js'
import { loadApiClient } from './helpers/apiClient.js'

const read = (relative) => readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
const view = read('../src/views/AnswerSheet.vue')
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

test('学生答题卡支持文件夹与压缩包导入，并随每页上传来源路径', () => {
  assert.match(view, /import-archives\n/)
  assert.match(view, /extractImagesFromFileList\(selected, \{ maxFiles: props\.maxFiles \}\)/)
  assert.match(view, /extractImagesFromDrop\(event, \{ maxFiles: props\.maxFiles \}\)/)
  assert.match(view, /webkitdirectory: true/)
  assert.match(view, /accept: '\.jpg,\.jpeg,\.png,\.zip,\.rar'/)
  assert.match(view, /captureSourcePath\(file\),\n\s+\)/)
  // Only the student picker imports folders; template/source pickers stay image-only.
  assert.equal((view.match(/import-archives/g) || []).length, 1)
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

test('上传区默认提醒文件夹、文件名配对或按顺序上传正反面，并在页面卡片显示身份来源', () => {
  assert.match(view, /每位学生一个文件夹/)
  assert.match(view, /文件名不能确定身份时，请按顺序上传同一学生的正面和背面（正面在前）/)
  assert.match(view, /const byFolder = pairFilesByFolder\(files, captureSourcePath\)/)
  assert.match(view, /const byName = pairFilesBySide\(files, captureFileName\)/)
  assert.match(view, /source: 'folder_order'/)
  assert.match(view, /studentFileSideLabel\(file, index\)/)
  assert.match(view, /studentPageIdentityHint\(page, session\.student_pages\)\.text/)
})
