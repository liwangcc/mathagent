import assert from 'node:assert/strict'
import test from 'node:test'

import {
  compressImage,
  compressImageFiles,
  createRotatedOcrReadyFile,
} from '../src/utils/uploadValidation.js'

function preserveBrowserGlobals() {
  const previousBitmap = globalThis.createImageBitmap
  const previousDocument = globalThis.document
  return () => {
    if (previousBitmap === undefined) delete globalThis.createImageBitmap
    else globalThis.createImageBitmap = previousBitmap
    if (previousDocument === undefined) delete globalThis.document
    else globalThis.document = previousDocument
  }
}

function pngBytes(width, height) {
  const bytes = new Uint8Array(24)
  bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], 0)
  bytes.set([0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52], 8)
  const view = new DataView(bytes.buffer)
  view.setUint32(16, width, false)
  view.setUint32(20, height, false)
  return bytes
}

function jpegBytes(width, height, orientation = 1) {
  const exif = orientation === 1 ? [] : [
    0xff, 0xe1, 0x00, 0x1e,
    0x45, 0x78, 0x69, 0x66, 0x00, 0x00,
    0x49, 0x49, 0x2a, 0x00, 0x08, 0x00, 0x00, 0x00,
    0x01, 0x00,
    0x12, 0x01, 0x03, 0x00, 0x01, 0x00, 0x00, 0x00,
    orientation, 0x00, 0x00, 0x00,
  ]
  return new Uint8Array([
    0xff, 0xd8,
    ...exif,
    0xff, 0xc0, 0x00, 0x11, 0x08,
    (height >>> 8) & 0xff, height & 0xff,
    (width >>> 8) & 0xff, width & 0xff,
    0x03, 0x01, 0x11, 0x00, 0x02, 0x11, 0x00, 0x03, 0x11, 0x00,
    0xff, 0xd9,
  ])
}

function gifBytes(width, height) {
  const bytes = new Uint8Array(10)
  bytes.set([0x47, 0x49, 0x46, 0x38, 0x39, 0x61], 0)
  const view = new DataView(bytes.buffer)
  view.setUint16(6, width, true)
  view.setUint16(8, height, true)
  return bytes
}

function bmpBytes(width, height) {
  const bytes = new Uint8Array(26)
  bytes.set([0x42, 0x4d], 0)
  const view = new DataView(bytes.buffer)
  view.setUint32(14, 40, true)
  view.setInt32(18, width, true)
  view.setInt32(22, height, true)
  return bytes
}

function webpBytes(width, height) {
  const bytes = new Uint8Array(30)
  bytes.set([0x52, 0x49, 0x46, 0x46], 0)
  const view = new DataView(bytes.buffer)
  view.setUint32(4, 22, true)
  bytes.set([0x57, 0x45, 0x42, 0x50, 0x56, 0x50, 0x38, 0x58], 8)
  view.setUint32(16, 10, true)
  const storedWidth = width - 1
  const storedHeight = height - 1
  bytes.set([
    storedWidth & 0xff,
    (storedWidth >>> 8) & 0xff,
    (storedWidth >>> 16) & 0xff,
  ], 24)
  bytes.set([
    storedHeight & 0xff,
    (storedHeight >>> 8) & 0xff,
    (storedHeight >>> 16) & 0xff,
  ], 27)
  return bytes
}

function webpLossyBytes(width, height) {
  const bytes = new Uint8Array((512 * 1024) + 100)
  bytes.set([0x52, 0x49, 0x46, 0x46], 0)
  const view = new DataView(bytes.buffer)
  view.setUint32(4, bytes.length - 8, true)
  bytes.set([0x57, 0x45, 0x42, 0x50, 0x56, 0x50, 0x38, 0x20], 8)
  view.setUint32(16, bytes.length - 20, true)
  bytes.set([0x9d, 0x01, 0x2a], 23)
  view.setUint16(26, width, true)
  view.setUint16(28, height, true)
  return bytes
}

function webpLosslessBytes(width, height) {
  const bytes = new Uint8Array((512 * 1024) + 100)
  bytes.set([0x52, 0x49, 0x46, 0x46], 0)
  const view = new DataView(bytes.buffer)
  view.setUint32(4, bytes.length - 8, true)
  bytes.set([0x57, 0x45, 0x42, 0x50, 0x56, 0x50, 0x38, 0x4c], 8)
  view.setUint32(16, bytes.length - 20, true)
  bytes[20] = 0x2f
  view.setUint32(21, (width - 1) | ((height - 1) << 14), true)
  return bytes
}

function imageBlob(bytes, type) {
  return new Blob([bytes], { type })
}

function imageFile(bytes, name, type) {
  return new File([bytes], name, { type })
}

function canvasDocument() {
  return {
    createElement() {
      return {
        width: 0,
        height: 0,
        getContext() {
          return {
            fillStyle: '',
            fillRect() {},
            drawImage() {},
            getImageData() {
              return { data: new Uint8ClampedArray([0, 0, 0, 255]) }
            },
            putImageData() {},
          }
        },
        toBlob(callback, type) {
          callback(new Blob(['ready'], { type }))
        },
      }
    },
  }
}

test('accepted image formats expose bounded dimensions before decode and EXIF swaps resize axes', async () => {
  const restore = preserveBrowserGlobals()
  const resizeOptions = []
  globalThis.createImageBitmap = async (_file, options) => {
    resizeOptions.push(options)
    return {
      width: options.resizeWidth,
      height: options.resizeHeight,
      close() {},
    }
  }
  globalThis.document = canvasDocument()

  try {
    const sources = [
      imageBlob(jpegBytes(1200, 2400, 6), 'image/jpeg'),
      imageBlob(pngBytes(2400, 1200), 'image/png'),
      imageBlob(gifBytes(2400, 1200), 'image/gif'),
      imageBlob(bmpBytes(2400, 1200), 'image/bmp'),
      imageBlob(webpBytes(2400, 1200), 'image/webp'),
      imageBlob(webpLossyBytes(2400, 1200), 'image/webp'),
      imageBlob(webpLosslessBytes(2400, 1200), 'image/webp'),
    ]
    for (const source of sources) await compressImage(source)

    assert.deepEqual(
      resizeOptions.map(({ resizeWidth, resizeHeight }) => [resizeWidth, resizeHeight]),
      [
        [1920, 960],
        [1920, 960],
        [1920, 960],
        [1920, 960],
        [1920, 960],
        [1920, 960],
        [1920, 960],
      ],
    )
  } finally {
    restore()
  }
})

test('unreadable headers fail closed before invoking a pixel decoder', async () => {
  const restore = preserveBrowserGlobals()
  let decodeCalls = 0
  globalThis.createImageBitmap = async () => {
    decodeCalls += 1
    throw new Error('must not decode')
  }
  globalThis.document = canvasDocument()

  try {
    await assert.rejects(
      () => compressImage(imageBlob(new Uint8Array([0xff, 0xd8, 0xff]), 'image/jpeg')),
      (error) => error.code === 'IMAGE_HEADER_INVALID',
    )
    assert.equal(decodeCalls, 0)
  } finally {
    restore()
  }
})

test('older createImageBitmap implementations fall back without resize options', async () => {
  const restore = preserveBrowserGlobals()
  const bitmapOptions = []
  let encodedDimensions = null
  globalThis.createImageBitmap = async (_file, options) => {
    bitmapOptions.push(options)
    if ('resizeWidth' in options) throw new TypeError('resize options unsupported')
    return {
      width: 2400,
      height: 1200,
      close() {},
    }
  }
  const fallbackDocument = canvasDocument()
  const originalCreateElement = fallbackDocument.createElement
  fallbackDocument.createElement = (...args) => {
    const canvas = originalCreateElement(...args)
    canvas.toBlob = function toBlob(callback, type) {
      encodedDimensions = [this.width, this.height]
      callback(new Blob(['ready'], { type }))
    }
    return canvas
  }
  globalThis.document = fallbackDocument

  try {
    await compressImage(imageBlob(pngBytes(2400, 1200), 'image/png'))
    assert.equal(bitmapOptions.length, 2)
    assert.deepEqual(bitmapOptions[1], { imageOrientation: 'from-image' })
    assert.deepEqual(encodedDimensions, [1920, 960])
  } finally {
    restore()
  }
})

test('OCR image compression normalizes EXIF, bounds pixels, suppresses review ink, and releases memory', async () => {
  const restore = preserveBrowserGlobals()
  let bitmapClosed = false
  let drawArgs = null
  let encodedDimensions = null
  let encodedQuality = null
  let filledWhite = false
  let filteredPixels = null
  const imageDataRequests = []
  let canvas = null

  globalThis.createImageBitmap = async (_file, options) => {
    assert.equal(options.imageOrientation, 'from-image')
    assert.equal(options.resizeWidth, 960)
    assert.equal(options.resizeHeight, 1920)
    assert.equal(options.resizeQuality, 'high')
    return {
      width: options.resizeWidth,
      height: options.resizeHeight,
      close() {
        bitmapClosed = true
      },
    }
  }
  globalThis.document = {
    createElement(tag) {
      assert.equal(tag, 'canvas')
      const pixels = new Uint8ClampedArray([
        160, 100, 100, 255,
        100, 150, 100, 255,
        70, 80, 150, 255,
        30, 30, 30, 255,
      ])
      canvas = {
        width: 0,
        height: 0,
        getContext() {
          return {
            fillStyle: '',
            fillRect() {
              filledWhite = this.fillStyle === '#fff'
            },
            drawImage(...args) {
              drawArgs = args
            },
            getImageData(x, y, width, height) {
              imageDataRequests.push([x, y, width, height])
              return { data: pixels }
            },
            putImageData(imageData) {
              filteredPixels = [...imageData.data]
            },
          }
        },
        toBlob(callback, type, quality) {
          encodedDimensions = [this.width, this.height]
          encodedQuality = quality
          callback(new Blob(['normalized'], { type }))
        },
      }
      return canvas
    },
  }

  try {
    const output = await compressImage(imageBlob(jpegBytes(1200, 2400), 'image/jpeg'))
    assert.equal(output.type, 'image/jpeg')
    assert.deepEqual(encodedDimensions, [960, 1920])
    assert.equal(encodedQuality, 0.88)
    assert.equal(drawArgs[0].width, 960)
    assert.equal(drawArgs[0].height, 1920)
    assert.equal(filledWhite, true)
    assert.deepEqual(filteredPixels.slice(0, 8), [255, 255, 255, 255, 255, 255, 255, 255])
    assert.deepEqual(filteredPixels.slice(8), [70, 80, 150, 255, 30, 30, 30, 255])
    assert.equal(bitmapClosed, true)
    assert.ok(imageDataRequests.length > 1)
    assert.ok(imageDataRequests.every(([, , width, height]) => width * height * 4 <= 512 * 1024))
    assert.ok(imageDataRequests.every(([, , , height]) => height <= 64))
    assert.equal(canvas.width, 0)
    assert.equal(canvas.height, 0)
  } finally {
    restore()
  }
})

test('source pixel budget accepts the exact limit and rejects overflow before pixel decode', async () => {
  const restore = preserveBrowserGlobals()
  let bitmapCloseCount = 0
  let canvasCreateCount = 0
  let bitmapCreateCount = 0

  globalThis.createImageBitmap = async (_file, options) => ({
    width: (bitmapCreateCount += 1, options.resizeWidth),
    height: options.resizeHeight,
    close() { bitmapCloseCount += 1 },
  })
  globalThis.document = {
    createElement() {
      canvasCreateCount += 1
      return {
        width: 0,
        height: 0,
        getContext() {
          return {
            fillStyle: '',
            fillRect() {},
            drawImage() {},
            getImageData() {
              return { data: new Uint8ClampedArray([0, 0, 0, 255]) }
            },
            putImageData() {},
          }
        },
        toBlob(callback, type) {
          callback(new Blob(['ready'], { type }))
        },
      }
    },
  }

  try {
    const accepted = await compressImage(imageBlob(pngBytes(4000, 4000), 'image/png'))
    assert.equal(accepted.type, 'image/jpeg')
    assert.equal(canvasCreateCount, 1)
    assert.equal(bitmapCloseCount, 1)

    await assert.rejects(
      () => compressImage(imageBlob(pngBytes(4001, 4000), 'image/png')),
      (error) => {
        assert.equal(error.code, 'IMAGE_DECODE_PIXEL_LIMIT_EXCEEDED')
        assert.equal(error.width, 4001)
        assert.equal(error.height, 4000)
        assert.equal(error.maxPixels, 16_000_000)
        return true
      },
    )
    assert.equal(canvasCreateCount, 1)
    assert.equal(bitmapCreateCount, 1)
    assert.equal(bitmapCloseCount, 1)
  } finally {
    restore()
  }
})

test('decoded source pixel failures reach onFileError and do not stop the serial batch', async () => {
  const restore = preserveBrowserGlobals()
  const closedFiles = []
  const failures = []
  let canvasCreateCount = 0

  globalThis.createImageBitmap = async (file, options) => ({
    width: options.resizeWidth,
    height: options.resizeHeight,
    close() { closedFiles.push(file.name) },
  })
  globalThis.document = {
    createElement() {
      canvasCreateCount += 1
      return {
        width: 0,
        height: 0,
        getContext() {
          return {
            fillStyle: '',
            fillRect() {},
            drawImage() {},
            getImageData() {
              return { data: new Uint8ClampedArray([0, 0, 0, 255]) }
            },
            putImageData() {},
          }
        },
        toBlob(callback, type) {
          callback(new Blob(['ready'], { type }))
        },
      }
    },
  }

  try {
    const oversized = imageFile(pngBytes(8000, 3000), 'too-large.png', 'image/png')
    const accepted = imageFile(pngBytes(100, 100), 'accepted.png', 'image/png')
    const results = await compressImageFiles([oversized, accepted], 1920, 0.88, {
      onFileError: (failure) => failures.push(failure),
    })

    assert.deepEqual(results.map((file) => file.name), ['accepted.ocr-ready-v1.jpg'])
    assert.equal(failures.length, 1)
    assert.equal(failures[0].file, oversized)
    assert.equal(failures[0].index, 0)
    assert.equal(failures[0].error.code, 'IMAGE_DECODE_PIXEL_LIMIT_EXCEEDED')
    assert.equal(failures[0].error.maxPixels, 16_000_000)
    assert.equal(canvasCreateCount, 1)
    assert.deepEqual(closedFiles, ['accepted.png'])
  } finally {
    restore()
  }
})

test('OCR-ready batches remain serial across concurrent callers and use an explicit ready filename', async () => {
  const restore = preserveBrowserGlobals()
  let activeBitmaps = 0
  let maxActiveBitmaps = 0
  const decodeOrder = []

  globalThis.createImageBitmap = async (file, options) => {
    decodeOrder.push(file.name)
    activeBitmaps += 1
    maxActiveBitmaps = Math.max(maxActiveBitmaps, activeBitmaps)
    await new Promise((resolve) => setImmediate(resolve))
    return {
      width: options.resizeWidth,
      height: options.resizeHeight,
      close() {
        activeBitmaps -= 1
      },
    }
  }
  globalThis.document = {
    createElement() {
      return {
        width: 0,
        height: 0,
        getContext() {
          return {
            fillStyle: '',
            fillRect() {},
            drawImage() {},
            getImageData() {
              return { data: new Uint8ClampedArray([0, 0, 0, 255]) }
            },
            putImageData() {},
          }
        },
        toBlob(callback, type) {
          setImmediate(() => callback(new Blob(['ready'], { type })))
        },
      }
    },
  }

  try {
    const first = imageFile(pngBytes(800, 600), 'first.png', 'image/png')
    const second = imageFile(jpegBytes(800, 600), 'exam.page.jpeg', 'image/jpeg')
    const [firstResult, secondResult] = await Promise.all([
      compressImageFiles([first]),
      compressImageFiles([second]),
    ])

    assert.equal(maxActiveBitmaps, 1)
    assert.deepEqual(decodeOrder, ['first.png', 'exam.page.jpeg'])
    assert.equal(firstResult[0].name, 'first.ocr-ready-v1.jpg')
    assert.equal(secondResult[0].name, 'exam.page.ocr-ready-v1.jpg')
    assert.equal(firstResult[0].type, 'image/jpeg')
    assert.equal(secondResult[0].type, 'image/jpeg')
  } finally {
    restore()
  }
})

test('failed preprocessing omits originals from the submit-ready result', async () => {
  const restore = preserveBrowserGlobals()
  const failures = []

  globalThis.createImageBitmap = async (_file, options) => ({
    width: options.resizeWidth,
    height: options.resizeHeight,
    close() {},
  })
  globalThis.document = {
    createElement() {
      return {
        width: 0,
        height: 0,
        getContext() {
          return {
            fillStyle: '',
            fillRect() {},
            drawImage() {},
            getImageData() {
              return { data: new Uint8ClampedArray([0, 0, 0, 255]) }
            },
            putImageData() {},
          }
        },
        toBlob(callback) {
          callback(null)
        },
      }
    },
  }

  try {
    const original = imageFile(pngBytes(100, 100), 'broken.png', 'image/png')
    const misleading = imageFile(jpegBytes(100, 100), 'retry.ocr-ready-v1.jpg', 'image/jpeg')
    const results = await compressImageFiles([original, misleading], 1920, 0.88, {
      onFileError: ({ file, index, error }) => failures.push({ file, index, error }),
    })

    assert.deepEqual(results, [])
    assert.deepEqual(failures.map(({ file, index }) => [file.name, index]), [
      ['broken.png', 0],
      ['retry.ocr-ready-v1.jpg', 1],
    ])
    assert.ok(failures.every(({ error }) => error instanceof Error))
  } finally {
    restore()
  }
})

test('teacher rotation is rendered into a new OCR-ready JPEG and releases memory', async () => {
  const restore = preserveBrowserGlobals()
  let bitmapClosed = false
  let canvas = null
  const transforms = []

  globalThis.createImageBitmap = async (_file, options) => ({
    width: options.resizeWidth,
    height: options.resizeHeight,
    close() { bitmapClosed = true },
  })
  globalThis.document = {
    createElement() {
      canvas = {
        width: 0,
        height: 0,
        getContext() {
          return {
            fillStyle: '',
            fillRect() {},
            translate(...args) { transforms.push(['translate', ...args]) },
            rotate(...args) { transforms.push(['rotate', ...args]) },
            setTransform(...args) { transforms.push(['setTransform', ...args]) },
            drawImage() {},
            getImageData() {
              return { data: new Uint8ClampedArray([0, 0, 0, 255]) }
            },
            putImageData() {},
          }
        },
        toBlob(callback, type) {
          callback(new Blob(['rotated'], { type }))
        },
      }
      return canvas
    },
  }

  try {
    const source = imageFile(pngBytes(1200, 800), 'student.png', 'image/png')
    const output = await createRotatedOcrReadyFile(source, 1)

    assert.equal(output.name, 'student.ocr-ready-v1.jpg')
    assert.equal(output.type, 'image/jpeg')
    assert.ok(transforms.some(([kind, radians]) => kind === 'rotate' && radians === Math.PI / 2))
    assert.equal(bitmapClosed, true)
    assert.equal(canvas.width, 0)
    assert.equal(canvas.height, 0)
  } finally {
    restore()
  }
})
