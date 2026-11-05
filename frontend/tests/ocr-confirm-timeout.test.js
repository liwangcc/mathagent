import test from 'node:test'
import assert from 'node:assert/strict'
import { loadApiClient } from './helpers/apiClient.js'

test('orientation OCR retry outlives the full server phase budget', async () => {
  const client = await loadApiClient()
  const formData = new FormData()
  formData.append('quarter_turns', '1')

  await client.api.grade.orientationConfirm('b 1', 'p/1', formData)

  assert.equal(client.requests.length, 1)
  const [request] = client.requests
  assert.equal(request.method, 'post')
  assert.equal(request.url, '/batch-grade/b%201/paper/p%2F1/orientation-confirm')
  assert.equal(request.data, formData)
  assert.equal(request.timeout, 600000)
})
