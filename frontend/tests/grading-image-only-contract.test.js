import test from 'node:test'
import assert from 'node:assert/strict'
import { errorMessages, filePickers } from './helpers/availabilityQuota.js'
import { mountUpload, paperFile } from './helpers/uploadPage.js'

// Grading reads images only. The real Upload page neither offers PDF in its
// pickers and hints nor accepts one from any of its import paths.

const pdfFile = (name = 'paper.pdf') => new File(['%PDF-1.7\n'], name, { type: 'application/pdf' })
// The 已选 N 个文件 line under the student upload area.
const papersChosen = upload => /已选 \d+ 个文件/.test(upload.page.root.byClass('grade-card').text)

test('grading upload does not advertise unsupported PDF input', async (t) => {
  const upload = await mountUpload(t)
  const pickers = filePickers(upload)
  for (const name of ['template', 'papers']) {
    const accept = pickers[name].getAttribute('accept')
    assert.match(accept, /\.png/, name)
    assert.doesNotMatch(accept, /pdf/i, name)
  }
  const hints = upload.page.root.allByClass('upload-limit').map(el => el.text)
  assert.equal(hints.length, 2, 'the template and the student upload hints')
  for (const hint of hints) {
    assert.match(hint, /^支持 .*图片/)
    assert.doesNotMatch(hint, /PDF/i)
  }

  // Phone captures and student submissions hand their files straight to the
  // page; a PDF among them is refused as a whole.
  const { importPhoneCaptures, importStudentSubmissions } = upload.page.setup
  assert.equal(await importPhoneCaptures([paperFile('a.png'), pdfFile()]), false)
  assert.equal(await importStudentSubmissions([pdfFile()]), false)
  await upload.settle()
  const errors = errorMessages(upload)
  assert.equal(errors.length, 2)
  for (const message of errors) assert.match(message, /批改仅支持图片/)
  assert.equal(papersChosen(upload), false)
  assert.equal(upload.client.messages.some(message => message.type === 'success'), false)
})

test('a PDF chosen with the student file picker is refused before recognition', async (t) => {
  const upload = await mountUpload(t)
  await upload.choosePapers([pdfFile('第1页.pdf')])
  const errors = errorMessages(upload)
  assert.equal(errors.length, 1)
  assert.match(errors[0], /第1页\.pdf.*不是支持的图片.*PDF.*请先逐页导出图片/)
  assert.equal(papersChosen(upload), false)
  assert.deepEqual(upload.fetch.requests, [], 'nothing is sent for recognition')
})
