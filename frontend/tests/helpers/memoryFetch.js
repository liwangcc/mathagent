// A `fetch` answered in memory, for the API client's streaming calls
// (authFetch: server-sent event streams, file downloads), which do not go
// through axios.
//
//   const backend = installFetch(t, request => sseResponse([['progress', { current: 1 }], ['done', {}]]))
//   ...
//   backend.requests[0].url, backend.requests[0].init.body (a FormData)
//
// Each call is recorded as { url, init } and answered by the handler (a
// Response, or a promise of one). The previous fetch is restored after the
// test.
export function installFetch(t, handler) {
  const previous = globalThis.fetch
  const requests = []
  let answer = handler
  globalThis.fetch = async (url, init = {}) => {
    const request = { url: String(url), init }
    requests.push(request)
    return answer(request)
  }
  t?.after(() => { globalThis.fetch = previous })
  return {
    requests,
    respond(next) { answer = next },
    restore() { globalThis.fetch = previous },
  }
}

// A text/event-stream Response carrying `events`, [name, data] pairs.
export function sseResponse(events, { status = 200 } = {}) {
  const body = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
  return new Response(body, { status, headers: { 'Content-Type': 'text/event-stream' } })
}

// A JSON Response, e.g. an error status the stream request ends with.
export function jsonResponse(data, { status = 200 } = {}) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
}
