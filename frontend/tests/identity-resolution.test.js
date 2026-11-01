import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildIdentityResolutionItems,
  identityResolutionsReady,
  mergeIdentityCandidates,
  mergeRosterIntoIdentityItems,
  serializeIdentityResolutions,
} from '../src/utils/identityResolution.js'

test('four blank OCR identities can select from the refreshed batch roster', () => {
  const papers = [1, 2, 3, 4].map((number) => ({
    paper_id: `paper-${number}`,
    filename: `${number}.jpg`,
    identity: {
      status: 'needs_confirmation',
      reason: 'missing_identity',
      ocr_name: '',
      ocr_student_no: '',
      candidates: [],
    },
  }))
  papers.push({ paper_id: 'failed-paper', error: 'OCR failed' })
  const roster = [1, 2, 3, 4].map((number) => ({
    student_id: number,
    name: `Student ${number}`,
    student_no: '',
    class_id: 8,
  }))

  let items = buildIdentityResolutionItems(papers)
  assert.equal(items.length, 4)
  items = mergeRosterIntoIdentityItems(items, roster)
  assert.deepEqual(items[0].candidates.map((item) => item.student_id), [1, 2, 3, 4])

  items.forEach((item, index) => {
    item.student_id = String(index + 1)
  })
  assert.equal(identityResolutionsReady(items), true)
  assert.deepEqual(
    serializeIdentityResolutions(items).map((item) => item.student_id),
    [1, 2, 3, 4],
  )
})

test('OCR suggestions stay first while the rest of the class remains selectable', () => {
  const merged = mergeIdentityCandidates(
    [{ student_id: 2, name: 'Suggested', student_no: '02' }],
    [
      { student_id: 1, name: 'First', student_no: '01' },
      { student_id: 2, name: 'Suggested', student_no: '02' },
      { student_id: 3, name: 'Third', student_no: '' },
    ],
  )

  assert.deepEqual(merged.map((item) => item.student_id), [2, 1, 3])
})

test('recoverable OCR placeholder still enters identity confirmation', () => {
  const items = buildIdentityResolutionItems([
    {
      paper_id: 'recoverable-paper',
      error: 'OCR 识别失败',
      requires_manual_review: true,
      identity: { status: 'needs_confirmation', reason: 'missing_identity' },
    },
    {
      paper_id: 'fatal-paper',
      error: 'OCR 识别失败',
      requires_manual_review: false,
      identity: { status: 'needs_confirmation', reason: 'missing_identity' },
    },
  ])

  assert.deepEqual(items.map((item) => item.paper_id), ['recoverable-paper'])
})

test('persisted identity resolutions survive a refresh for unresolved papers', () => {
  const items = buildIdentityResolutionItems([
    {
      paper_id: 'existing-paper',
      identity: { status: 'needs_confirmation', ocr_name: 'OCR name' },
      identity_resolution: {
        paper_id: 'existing-paper',
        action: 'existing',
        student_id: 17,
        confirmed_name: '',
        confirmed_student_no: '',
      },
    },
    {
      paper_id: 'create-paper',
      identity: { status: 'needs_confirmation', ocr_name: 'old', ocr_student_no: 'old-no' },
      identity_resolution: {
        paper_id: 'create-paper',
        action: 'create',
        student_id: null,
        confirmed_name: '  新同学  ',
        confirmed_student_no: '  20260809  ',
      },
    },
    {
      paper_id: 'matched-paper',
      identity: { status: 'matched', student_id: 99 },
      identity_resolution: {
        paper_id: 'matched-paper',
        action: 'existing',
        student_id: 99,
      },
    },
  ])

  assert.equal(items.length, 2)
  assert.deepEqual(
    items.map(({ paper_id, action, student_id, confirmed_name, confirmed_student_no }) => ({
      paper_id, action, student_id, confirmed_name, confirmed_student_no,
    })),
    [
      {
        paper_id: 'existing-paper',
        action: 'existing',
        student_id: 17,
        confirmed_name: 'OCR name',
        confirmed_student_no: '',
      },
      {
        paper_id: 'create-paper',
        action: 'create',
        student_id: null,
        confirmed_name: '新同学',
        confirmed_student_no: '20260809',
      },
    ],
  )
  assert.equal(identityResolutionsReady(items), true)
})

test('mismatched or invalid persisted identity data cannot select another paper student', () => {
  const items = buildIdentityResolutionItems([
    {
      paper_id: 'paper-a',
      student_name: 'OCR fallback A',
      identity: { status: 'needs_confirmation' },
      identity_resolution: {
        paper_id: 'paper-b',
        action: 'existing',
        student_id: 42,
      },
    },
    {
      paper_id: 'paper-b',
      student_name: 'OCR fallback B',
      identity: { status: 'needs_confirmation' },
      identity_resolution: {
        paper_id: 'paper-b',
        action: 'unsupported',
        student_id: 43,
      },
    },
  ])

  assert.deepEqual(items.map((item) => ({
    action: item.action,
    student_id: item.student_id,
    confirmed_name: item.confirmed_name,
  })), [
    { action: 'existing', student_id: null, confirmed_name: 'OCR fallback A' },
    { action: 'existing', student_id: null, confirmed_name: 'OCR fallback B' },
  ])
})

function groupedPaper(id, directory = 'directory-a') {
  return {
    paper_id: id, filename: `${id}.jpg`,
    identity: {
      status: 'needs_confirmation', reason: 'name_ambiguous', confirmation_group: directory,
      source_relative_path: `收集.zip/${directory}/${id}.jpg`, clue_source: 'filename',
      name_sources: [{ source: 'filename', names: ['张三'] }],
      candidates: [{ student_id: 1, name: '张三', student_no: '08' }, { student_id: 2, name: '张三', student_no: '09' }],
    },
  }
}

test('one teacher selection resolves every page in the same consistent student directory', () => {
  const items = buildIdentityResolutionItems([groupedPaper('a'), groupedPaper('b'), groupedPaper('c', 'directory-b')])
  assert.equal(items.length, 2)
  assert.equal(items[0].pages.length, 2)
  assert.equal(items[0].ocr_name, '张三')
  items[0].student_id = 1
  items[1].student_id = 2
  assert.equal(identityResolutionsReady(items), true)
  assert.deepEqual(serializeIdentityResolutions(items).map(item => [item.paper_id, item.student_id]), [['a', 1], ['b', 1], ['c', 2]])
})

test('identity conflicts and different saved decisions are never grouped', () => {
  const first = groupedPaper('a')
  const conflict = groupedPaper('b')
  conflict.identity.reason = 'identity_name_conflict'
  const saved = groupedPaper('c')
  saved.identity_resolution = { paper_id: 'c', action: 'existing', student_id: 2 }
  const items = buildIdentityResolutionItems([first, conflict, saved])
  assert.equal(items.length, 3)
  assert.equal(items[2].student_id, 2)
})

test('restored per-page create choices are not silently joined into a student group', () => {
  const papers = ['a', 'b'].map(id => ({
    ...groupedPaper(id),
    identity_resolution: { paper_id: id, action: 'create', confirmed_name: '张三', confirmed_student_no: '' },
  }))
  const items = buildIdentityResolutionItems(papers)
  assert.equal(items.length, 2)
  assert.equal(identityResolutionsReady(items), true)
  assert.deepEqual(serializeIdentityResolutions(items).map(item => item.action), ['create', 'create'])
})

test('grouped pages cannot create separate students without a frozen roster ID', () => {
  const items = buildIdentityResolutionItems([groupedPaper('a'), groupedPaper('b')])
  items[0].action = 'create'
  items[0].confirmed_name = '张三'
  assert.equal(identityResolutionsReady(items), false)
  assert.throws(() => serializeIdentityResolutions(items), /多页图片请先加入班级名单/)
})

function blankPage(paperId, anchorId = '', extra = {}) {
  return {
    paper_id: paperId,
    filename: `${paperId}.jpg`,
    identity: {
      status: 'needs_confirmation',
      reason: 'roster_name_not_found',
      ...(anchorId ? { inherit_anchor: { paper_id: anchorId, filename: `${anchorId}.jpg` } } : {}),
      ...extra,
    },
  }
}

test('blank pages that follow a pending page share one teacher decision', () => {
  // The anchor may finish OCR after its followers; grouping is by paper id.
  const items = buildIdentityResolutionItems([
    blankPage('p3', 'p1'), blankPage('p1'), blankPage('p2', 'p1'), blankPage('q1'),
  ])
  assert.deepEqual(items.map(item => item.paper_id), ['p1', 'q1'])
  assert.deepEqual(items[0].pages.map(page => page.paper_id), ['p1', 'p3', 'p2'])
  assert.equal(items[0].inherited_page_count, 2)
  items[0].student_id = 5
  items[1].student_id = 6
  assert.deepEqual(
    serializeIdentityResolutions(items).map(item => [item.paper_id, item.student_id]),
    [['p1', 5], ['p3', 5], ['p2', 5], ['q1', 6]],
  )
})

test('inherited followers join a directory group and keep different saved decisions apart', () => {
  const directoryPage = (id) => ({
    ...blankPage(id),
    identity: { status: 'needs_confirmation', reason: 'name_ambiguous', confirmation_group: 'dir', ocr_name: '张三', candidates: [] },
  })
  const savedElsewhere = blankPage('f2', 'a')
  savedElsewhere.identity_resolution = { paper_id: 'f2', action: 'existing', student_id: 9 }
  const items = buildIdentityResolutionItems([directoryPage('a'), directoryPage('b'), blankPage('f1', 'a'), savedElsewhere])
  assert.deepEqual(items.map(item => item.paper_id), ['a', 'f2'])
  assert.deepEqual(items[0].pages.map(page => page.paper_id), ['a', 'b', 'f1'])
  assert.equal(items[1].student_id, 9)
})

test('a page blocked from inheriting stays its own card with the reason', () => {
  const items = buildIdentityResolutionItems([blankPage('p1', '', { inherit_blocked: 'same_page_type' })])
  assert.equal(items.length, 1)
  assert.equal(items[0].inherit_blocked, 'same_page_type')
  assert.equal(items[0].inherited_page_count, 0)
})
