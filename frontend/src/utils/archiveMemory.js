// Bound the decoder's internally-defined linear memory before instantiation.
// The decoder otherwise permits 2GiB even for a tiny malicious archive.
export function boundArchiveMemory(input, maxPages = 4096) {
  const bytes = new Uint8Array(input)
  const read = (cursor) => {
    let value = 0
    let shift = 0
    let byte
    do {
      if (cursor.pos >= bytes.length || shift > 28) throw new Error('Invalid WASM varint')
      byte = bytes[cursor.pos++]
      value += (byte & 127) * 2 ** shift
      shift += 7
    } while (byte & 128)
    return value
  }
  const encode = (value) => {
    const result = []
    do {
      const part = value % 128
      value = Math.floor(value / 128)
      result.push(part | (value ? 128 : 0))
    } while (value)
    return result
  }
  const cursor = { pos: 8 }
  while (cursor.pos < bytes.length) {
    const start = cursor.pos
    const id = bytes[cursor.pos++]
    const size = read(cursor)
    const end = cursor.pos + size
    if (id !== 5) { cursor.pos = end; continue }
    if (read(cursor) !== 1) throw new Error('Unsupported decoder memory layout')
    const flags = read(cursor)
    const minimum = read(cursor)
    const maximum = flags === 1 ? read(cursor) : maxPages
    if (![0, 1].includes(flags) || minimum > maxPages || cursor.pos !== end) {
      throw new Error('Unsupported decoder memory limits')
    }
    const section = [1, 1, ...encode(minimum), ...encode(Math.min(maximum, maxPages))]
    const header = [5, ...encode(section.length), ...section]
    const bounded = new Uint8Array(start + header.length + bytes.length - end)
    bounded.set(bytes.subarray(0, start))
    bounded.set(header, start)
    bounded.set(bytes.subarray(end), start + header.length)
    return bounded
  }
  throw new Error('Decoder memory section not found')
}
