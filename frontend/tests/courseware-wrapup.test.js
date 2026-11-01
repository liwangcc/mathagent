import test from 'node:test'
import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'

// Independent of the re-pinned base manifest: only the two reviewed render
// functions may differ from the September delivery's mathematical scripts.
// These receipts pin definitions, scene state, derivations, exercises, drag
// coordinate conversion and all other scene operations to their prior bytes.
const chapters = [
  {
    file: '../../courseware/初中/J17_坐标与投影视图.html', code: 'J17',
    script: text => text.includes('const META=') && text.includes('J17'),
    render: /function renderSolid\(P,s,d,v\)\{[\s\S]*?(?=\nconst CHAPTER=)/g,
    expected: '59c6689445722ee314ce0d775275289e6deae3a70bb4ba978f62d432f816439f',
  },
  {
    file: '../../courseware/小学/P11_位置、方向与图形运动.html', code: 'P11',
    script: text => text.includes('const Trans='),
    render: / render\(a\)\{let s=a.state,d=this.derive\(s\),P=\[400\+s.x\*55[\s\S]*?(?=return\{svg:o,meaning:)/g,
    expected: 'e88d0daf436a8f7fc5631ee49001fe9d7023c80e7233ebe7079459b8999ef02d',
  },
]

for (const chapter of chapters) {
  test(`${chapter.code} scene polish preserves every non-render mathematical byte`, () => {
    const html = fs.readFileSync(new URL(chapter.file, import.meta.url), 'utf8')
    const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(match => match[1])
    const script = scripts.find(chapter.script)
    assert.ok(script, 'original mathematical scene script is present')
    assert.equal([...script.matchAll(chapter.render)].length, 1, 'exactly the reviewed scene renderer is excluded')
    const unchanged = script.replace(chapter.render, '')
    assert.equal(crypto.createHash('sha256').update(unchanged).digest('hex'), chapter.expected)
  })
}
