import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { getUploadSourcePath, normalizeUploadSourcePath } from '../src/utils/uploadSource.js'
import {
  SUBMISSION_IMPORT_ROOT,
  nextPageNumbers,
  pageSlots,
  studentSubmitOriginWarning,
  studentSubmitUrl,
  submissionFile,
  submissionImportEntries,
  submissionSourcePath,
} from '../src/utils/studentSubmission.js'

test('page slots run to the highest uploaded page and keep gaps visible', () => {
  assert.deepEqual(pageSlots([]), [])
  const slots = pageSlots([{ page_no: 3 }, { page_no: 1 }])
  assert.deepEqual(slots.map((slot) => [slot.pageNo, slot.page?.page_no ?? null]), [[1, 1], [2, null], [3, 3]])
})

test('newly picked photos go after the last page and stop at the limit', () => {
  assert.deepEqual(nextPageNumbers([], 3, 20), [1, 2, 3])
  assert.deepEqual(nextPageNumbers([{ page_no: 1 }, { page_no: 4 }], 2, 20), [5, 6])
  assert.deepEqual(nextPageNumbers([{ page_no: 19 }], 3, 20), [20])
  assert.deepEqual(nextPageNumbers([{ page_no: 20 }], 1, 20), [])
})

test('imported pages are named by student folder and page, as valid source paths', () => {
  const path = submissionSourcePath({ name: '张三', student_no: '08' }, 2)
  assert.equal(path, `${SUBMISSION_IMPORT_ROOT}/张三_学号08/第2页.jpg`)
  assert.equal(normalizeUploadSourcePath(path), path)
  // Characters that are illegal in source paths never reach the path.
  const odd = submissionSourcePath({ name: 'A/B:C', student_no: '1\\2' }, 1)
  assert.equal(odd, `${SUBMISSION_IMPORT_ROOT}/A_B_C_学号1_2/第1页.jpg`)
  assert.equal(normalizeUploadSourcePath(odd), odd)
  assert.equal(submissionSourcePath({ name: '李四', student_no: '' }, 1), `${SUBMISSION_IMPORT_ROOT}/李四/第1页.jpg`)
})

test('import entries keep the student order and sort each student by page', () => {
  const entries = submissionImportEntries([
    { name: '张三', student_no: '1', pages: [{ id: 7, page_no: 2 }, { id: 5, page_no: 1 }] },
    { name: '李四', student_no: '2', pages: [{ id: 9, page_no: 1 }] },
  ])
  assert.deepEqual(entries.map((entry) => entry.pageId), [5, 7, 9])
  assert.equal(entries[1].path, `${SUBMISSION_IMPORT_ROOT}/张三_学号1/第2页.jpg`)
})

test('an imported file carries its folder as the upload source path', () => {
  const path = submissionSourcePath({ name: '张三', student_no: '2024001' }, 1)
  const file = submissionFile(new Blob(['x'], { type: 'image/jpeg' }), path)
  assert.equal(file.name, '第1页.jpg')
  assert.equal(file.type, 'image/jpeg')
  assert.equal(getUploadSourcePath(file), path)
})

test('links use the page origin and warn when phones cannot reach it', () => {
  assert.equal(studentSubmitUrl('https://example.edu', 'a b'), 'https://example.edu/submit/a%20b')
  assert.match(studentSubmitOriginWarning('http://localhost:5173'), /本机地址/)
  assert.equal(studentSubmitOriginWarning('http://192.168.1.20:5173'), '')
  assert.equal(studentSubmitOriginWarning('https://example.edu'), '')
})

test('the student page is public and skips the login guard', () => {
  const router = readFileSync(new URL('../src/router/index.js', import.meta.url), 'utf8')
  assert.match(router, /path: '\/submit\/:token',[\s\S]*?meta: \{ public: true, standalone: true/)
})
