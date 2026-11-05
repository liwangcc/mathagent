import assert from 'node:assert/strict'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'

test('blob JSON failures preserve the backend service detail for the page', async () => {
  const client = await loadApiClient()
  const detail = '原图已过期，请重新上传后再试'
  client.respond(() => ({
    status: 410,
    data: new Blob([JSON.stringify({ detail })], { type: 'application/json' }),
  }))

  const error = await client.api.grade.paperImage('b1', 'p1', 'original').catch(err => err)

  assert.equal(client.requests[0].responseType, 'blob')
  assert.equal(error.message, detail)
  assert.deepEqual(error.response.data, { detail })
  assert.deepEqual(client.messages, [{ type: 'error', message: detail }])
})
