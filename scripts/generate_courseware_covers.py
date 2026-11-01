"""Build catalogue thumbnails from real, offline courseware diagrams.

Run with the existing development environment:
    ./venv_local/Scripts/python.exe scripts/generate_courseware_covers.py
Requires Playwright, Pillow and a locally installed Edge (or --browser-channel).
No backend, database, credentials or model/network calls are used.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageOps, ImageStat
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
# Choose a representative scene, not a generic illustration of the chapter title.
SCENE_INDEX = {
    'P01': 1, 'P12': 1, 'J04': 1, 'J08': 2, 'J09': 1, 'J15': 1,
    'H02': 2, 'H10': 2, 'H14': 1, 'H18': 2,
}
SIZE = (800, 450)

GRAPHICS = """() => {
    const graphics = [...document.querySelectorAll('svg,canvas')].filter(e => {
        const r = e.getBoundingClientRect();
        return e.checkVisibility() && r.width > 250 && r.height > 120;
    });
    const main = graphics.find(e => ['diagram','plot','chart','s','s-fallback'].includes(e.id))
        || graphics.sort((a,b) => b.clientWidth*b.clientHeight-a.clientWidth*a.clientHeight)[0];
    if (!main) throw Error('No visible teaching diagram');
    // Element screenshots capture the composited WebGL + SVG surface, not just
    // one DOM layer; do not capture the host's unrelated buttons and readouts.
    const layered = graphics.some(e => e.tagName === 'CANVAS');
    const target = main;
    target.dataset.coursewareCover = 'capture';
    return {tag:main.tagName, id:main.id, layered,
        shapes:main.querySelectorAll('path,line,rect,circle,ellipse,polygon,polyline').length,
        sceneTitle:window.CW?.app?.scene?.title ||
            document.querySelector('#sceneTitle,#scene-title,#boardTitle,.scene-title')?.textContent?.trim() ||
            document.querySelector('[role=tab][aria-selected=true],nav button.active')?.textContent?.trim() || document.title,
        sceneId:window.CW?.app?.scene?.id || window.CourseApp?.getState?.()?.scene ||
            window.MathAgent?.scene || document.body.dataset.currentScene || document.body.dataset.scene || location.hash.slice(1),
        scenes:window.CW?.app?.scenes?.map(s=>({id:s.id,title:s.title})) ||
            (typeof CHAPTER !== 'undefined' ? CHAPTER.scenes.map(s=>({id:s.id,title:s.title})) : []),
        state:window.CW?.snapshot?.()?.state || window.CourseApp?.getState?.()?.math || null};
}"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-channel', default='msedge')
    parser.add_argument('--codes', nargs='*', help='Regenerate selected codes only')
    args = parser.parse_args()
    catalogue = json.loads((ROOT / 'courseware/catalog_v4.json').read_text(encoding='utf-8'))
    entries = catalogue['entries'] + [{
        'code': 'SPACE', 'stage': 'senior', 'chapter': '空间几何关系',
        'offlineFileName': '空间几何关系.html', 'coursewarePath': 'courseware/高中/空间几何关系.html',
    }]
    output = ROOT / 'frontend/public/courseware/covers'
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = ROOT / 'frontend/src/courseware/covers.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    requested = set(args.codes or [e['code'] for e in entries])
    assert requested <= {e['code'] for e in entries}, 'Unknown chapter code'
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.browser_channel, headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1,
                                      reduced_motion='reduce')
        # Offline generation must not send content to any external origin.
        context.route('http://**/*', lambda route: route.abort())
        context.route('https://**/*', lambda route: route.abort())
        for entry in entries:
            code = entry['code']
            if code not in requested:
                continue
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            source = ROOT / entry['coursewarePath']
            index = SCENE_INDEX.get(code, 0)
            page.goto(source.as_uri(), wait_until='load')
            page.wait_for_function("document.querySelector('svg,canvas') !== null")
            overview = page.locator('#lf-overview')
            if overview.count():
                overview.click()
            if index:
                if page.evaluate('!!window.CW?.app'):
                    page.evaluate('(i)=>window.CW.app.go(i)', index)
                else:
                    tabs = page.locator('[data-scene]')
                    if not tabs.count():
                        tabs = page.locator('[role=tab]')
                    assert tabs.count() > index, f'{code}: representative scene missing'
                    tabs.nth(index).click()
            page.wait_for_timeout(400)  # Settle the chapter's ResizeObserver / WebGL drawing.
            page.evaluate("document.querySelectorAll('#camera-tools,.canvas-tools,.view-tools,#engine-badge,#fallback-warning').forEach(e=>e.style.visibility='hidden')")
            info = page.evaluate(GRAPHICS)
            if code == 'SPACE':
                info['sceneTitle'] = '线线、线面与面面关系'
                info['sceneId'] = 'll-intersect'
            assert info['sceneTitle'] and info['sceneId'], f'{code}: scene identity missing'
            assert info['shapes'] > 0 or info['layered'], f'{code}: empty diagram'
            assert not errors, f'{code}: rendering errors: {errors}'
            screenshot = page.locator('[data-courseware-cover=capture]').screenshot(animations='disabled')
            image = Image.open(io.BytesIO(screenshot)).convert('RGB')
            assert max(ImageStat.Stat(image).stddev) > 8, f'{code}: blank capture'
            # Contain instead of cover: axes, endpoints and captions must not be cropped.
            thumb = Image.new('RGB', SIZE, '#f6f8f4')
            fitted = ImageOps.contain(image, (SIZE[0]-32, SIZE[1]-24), Image.Resampling.LANCZOS)
            thumb.paste(fitted, ((SIZE[0]-fitted.width)//2, (SIZE[1]-fitted.height)//2))
            target = output / f'{code}.webp'
            thumb.save(target, 'WEBP', quality=90, method=6)
            manifest[code] = {
                'code': code, 'stage': entry['stage'], 'chapter': entry['chapter'],
                'fileName': entry['offlineFileName'], 'src': f'/courseware/covers/{code}.webp',
                'sceneTitle': info['sceneTitle'], 'sceneId': info['sceneId'],
                'sourcePath': entry['coursewarePath'], 'sourceSha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                'width': SIZE[0], 'height': SIZE[1], 'renderer': 'layered' if info['layered'] else info['tag'],
            }
            # Save after every entry; a failed chapter must not lose earlier work.
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
            print(f'{code}: {info["sceneTitle"]} -> {target.stat().st_size} bytes', flush=True)
            page.close()
        browser.close()
    if not args.codes:
        assert set(manifest) == {e['code'] for e in entries}
    print(f'Verified {len(requested)} real lesson covers; catalogue contains {len(manifest)}.', flush=True)


if __name__ == '__main__':
    main()
