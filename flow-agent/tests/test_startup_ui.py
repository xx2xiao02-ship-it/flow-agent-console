"""Real Chromium rendering of the built UI against isolated startup fixtures."""
import json
import mimetypes
import os
from pathlib import Path
import pytest


def test_startup_screen_blocks_business_controls_and_unlocks(tmp_path):
    from playwright.sync_api import sync_playwright
    browser_path = os.environ.get('FLOW_TEST_BROWSER')
    if not browser_path:
        pytest.skip('FLOW_TEST_BROWSER required')
    dist = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    state = {'state': 'checking', 'ready': False, 'message': '正在检查',
             'completed': 9, 'total': 11, 'checks': [
                 {'id':'account:a', 'label':'账号核验 1/2', 'state':'checking', 'message':''}]}
    calls = []
    def route(request):
        path = request.request.url.split('flow.test', 1)[1].split('?', 1)[0]
        calls.append((request.request.method, path))
        if path.startswith('/console/api/'):
            if path not in ('/console/api/startup', '/console/api/accounts', '/console/api/jobs', '/console/api/media', '/console/api/history', '/console/api/accounts/removed'):
                request.fulfill(status=503, content_type='application/json', body='{"detail":"isolated fixture"}')
                return
            body = state if path.endswith('/startup') else []
            if path.endswith('/accounts'):
                body = {'accounts': [], 'total_credits': 0}
            elif path.endswith('/history'):
                body = {'generations': [], 'total': 0, 'limit': 50, 'offset': 0}
            request.fulfill(content_type='application/json', body=json.dumps(body))
        else:
            relative = path.removeprefix('/console/').lstrip('/')
            file = dist / (relative or 'index.html')
            request.fulfill(content_type=mimetypes.guess_type(file)[0] or 'text/plain', body=file.read_bytes())
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path, headless=True)
        try:
            page = browser.new_page(viewport={'width':1280, 'height':800})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', route)
            page.goto('http://flow.test/console/#media')
            page.get_by_role('progressbar').wait_for()
            assert page.get_by_role('progressbar').get_attribute('value') == '9'
            assert page.locator('.sidebar').count() == 0
            page.keyboard.press('Tab')
            assert page.locator(':focus').evaluate('(e) => e.tagName') == 'SUMMARY'
            page.screenshot(path=str(tmp_path/'startup.png'))
            state.update(state='blocked', message='数据库需要处理')
            page.get_by_role('heading', name='启动需要处理').wait_for(timeout=12000)
            assert page.locator('.sidebar').count() == 0
            state.update(state='ready', ready=True, completed=11, checks=[])
            try:
                page.locator('.sidebar').wait_for(timeout=12000)
            except Exception:
                pytest.fail(str(errors))
            assert page.get_by_role('progressbar').count() == 0
            assert all(method == 'GET' for method, _ in calls)
        finally:
            browser.close()
