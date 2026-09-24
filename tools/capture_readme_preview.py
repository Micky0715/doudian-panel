"""Render README screenshots with local assets and isolated, empty API fixtures.

Never connects to the panel server, merchant APIs, or a signed-in browser.
Every request is intercepted; writes and unrecognized destinations are blocked.
Run: python tools/capture_readme_preview.py
"""
import json
import math
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'docs' / 'images'
ORIGIN = 'http://readme-preview.invalid'
FIXTURES = {
    '/api/shops': {'shops': [], 'max_parallel': 4},
    '/api/tasks': {'tasks': []},
    '/api/gaia/model-mappings/latest': {'success': True, 'mapping': None},
    '/api/gaia/qualities/status': {'success': True, 'qualities': {}},
    '/api/gaia/capabilities': {
        'success': True,
        'features': {'gaia_submit': False},
        'contracts': {'mapping_policy': 'explicit_mainland_only_v2'},
    },
}


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    blocked = []
    errors = []

    def serve(route):
        request = route.request
        url = urlparse(request.url)
        if request.method != 'GET':
            blocked.append({'method': request.method, 'path': url.path})
            route.abort()
            return
        if url.netloc != 'readme-preview.invalid':
            # Offline system-font fallback: no external font/CDN requests.
            route.abort()
            return
        if url.path in FIXTURES:
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps(FIXTURES[url.path]))
            return
        path = ROOT / ('panel.html' if url.path == '/' else unquote(url.path).lstrip('/'))
        path = path.resolve()
        allowed = path == ROOT / 'panel.html' or ROOT / 'assets' in path.parents
        if allowed and path.is_file():
            route.fulfill(status=200, body=path.read_bytes(),
                          content_type=mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            return
        blocked.append({'method': request.method, 'path': url.path})
        route.abort()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1600, 'height': 1100},
                                      device_scale_factor=1, reduced_motion='reduce',
                                      service_workers='block', locale='zh-CN')
        context.route('**/*', serve)
        context.add_init_script("""
          window.EventSource = class {
            constructor() {
              setTimeout(() => {
                this.onopen?.({});
                this.onmessage?.({data: JSON.stringify({type: 'snapshot', tasks: []})});
              }, 0);
            }
            close() {}
          };
        """)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(ORIGIN + '/', wait_until='load')
        page.locator('#connTxt').filter(has_text='已连接').wait_for()
        page.evaluate("""async () => {
          await document.fonts.ready;
          await Promise.all(Array.from(document.images).map(img => img.decode().catch(() => {})));
        }""")

        def capture(name, selectors):
            boxes = [page.locator(selector).bounding_box() for selector in selectors]
            assert all(boxes), selectors
            scroll = page.evaluate('({x: window.scrollX, y: window.scrollY})')
            x = min(box['x'] for box in boxes) + scroll['x']
            y = min(box['y'] for box in boxes) + scroll['y']
            right = max(box['x'] + box['width'] for box in boxes) + scroll['x']
            bottom = max(box['y'] + box['height'] for box in boxes) + scroll['y']
            page.screenshot(path=str(OUTPUT / name), animations='disabled', full_page=True,
                            clip={'x': max(0, math.floor(x) - 16),
                                  'y': max(0, math.floor(y) - 12),
                                  'width': math.ceil(right - x) + 32,
                                  'height': math.ceil(bottom - y) + 24})

        capture('home.png', ['.topbar', '#opsStage'])
        capture('gaia.png', ['.workspace-nav', '#gaiaView'])
        page.locator('.workspace-tab[data-view="doudian"]').click()
        capture('doudian.png', ['.workspace-nav', '#doudianView'])
        assert not blocked, blocked
        assert not errors, errors
        browser.close()
    print(json.dumps({'screenshots': [str(OUTPUT / name) for name in
                                    ['home.png', 'gaia.png', 'doudian.png']],
                      'blocked_unexpected_requests': blocked, 'page_errors': errors,
                      'production_requests': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
