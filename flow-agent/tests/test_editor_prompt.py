"""Exercise prompt validation in real local Chrome, with no provider calls."""
from pathlib import Path
import json
import os

import pytest

from flow_agent.official_images import OfficialImages, OfficialImageError
from flow_agent.official_videos import OfficialVideos


@pytest.mark.asyncio
@pytest.mark.parametrize('runner_type', [OfficialImages, OfficialVideos])
@pytest.mark.parametrize('shape,prompt,corrupt', [
    ('paragraphs', '第一行 — 中文。\nSecond line: paper-collage.\n结尾', False),
    ('paragraphs', 'Line 1\n\nLine 3\n\n\nLine 6', False),
    ('paragraphs', 'First\r\nSecond\r\n\r\nFourth', False),
    ('breaks', '首段\n第二行\n\n空行后', False),
    ('textarea', '首段\n第二行', False),
    ('plain', 'Single line:  a  b\t中文', False),
    ('paragraphs', 'Line one\nLine two must stay', True),
    ('paragraphs', 'do not\nmerge words', 'merge'),
    ('paragraphs', 'preserve  double space\nsecond line', 'space'),
    ('paragraphs', 'preserve\n\nblank line', 'blank'),
])
async def test_prompt_readiness_retains_real_content(tmp_path, runner_type, shape, prompt, corrupt):
    browser = os.environ.get('FLOW_TEST_BROWSER')
    if not browser or not Path(browser).is_file():
        pytest.skip('Set FLOW_TEST_BROWSER for the real local Chrome check')
    from playwright.async_api import async_playwright

    # The input listener reproduces the official editor's observed <p> DOM.
    html = '''<meta charset="utf-8">
      <button aria-label="Google 账号：fixture (fixture@example.test)">账号</button>
      EDITOR<button aria-label="开始生成" onclick="window.submissions++">开始生成</button>
      <script>
      window.submissions=0;
      const editor=document.querySelector('[role=textbox]');
      editor.addEventListener('input',()=>{
        // Model value before the framework renders it as paragraph DOM.
        let value=INPUT_VALUE.replace(/\\r\\n/g,'\\n');
        if (CORRUPT === true) value=value.replace('must stay','CHANGED');
        if (CORRUPT === 'merge') value=value.replace('not\\nmerge','notmerge');
        if (CORRUPT === 'space') value=value.replace('  ',' ');
        if (CORRUPT === 'blank') value=value.replace('\\n\\n','\\n');
        if (SHAPE === 'paragraphs') {
          editor.replaceChildren(...value.split('\\n').map(line=>{
            const p=document.createElement('p');
            if(line) p.textContent=line; else p.append(document.createElement('br'));
            return p;
          }));
        } else if (SHAPE === 'breaks') {
          const p=document.createElement('p');
          value.split('\\n').forEach((line,i)=>{
            if(i)p.append(document.createElement('br'));
            p.append(document.createTextNode(line));
          });
          editor.replaceChildren(p);
        }
      }, {once:true});
      </script>'''
    editor = ('<textarea role="textbox" aria-label="您希望创作什么？"></textarea>' if shape == 'textarea'
              else '<div role="textbox" aria-label="您希望创作什么？" contenteditable="true" style="white-space:pre-wrap"></div>')
    html = (html.replace('EDITOR', editor).replace('CORRUPT', json.dumps(corrupt))
            .replace('SHAPE', json.dumps(shape)).replace('INPUT_VALUE', json.dumps(prompt)))
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(str(tmp_path/'profile'),
            executable_path=browser, headless=True)
        try:
            page = await context.new_page()
            await page.set_content(html)
            runner = runner_type(None)
            if corrupt:
                with pytest.raises(OfficialImageError) as exc:
                    await runner.ready_to_submit(page, prompt, 'fixture@example.test', {})
                assert exc.value.code == 'prompt_unconfirmed'
                assert not exc.value.submitted
            else:
                try:
                    button = await runner.ready_to_submit(page, prompt, 'fixture@example.test', {})
                except OfficialImageError:
                    textbox = page.get_by_role('textbox')
                    pytest.fail(repr({'expected':prompt, 'read':await runner.editor_prompt(textbox),
                        'html':await textbox.evaluate('e=>e.innerHTML')}))
                assert await button.is_enabled()
                assert await runner.editor_prompt(page.get_by_role('textbox')) == prompt.replace('\r\n','\n')
            assert await page.evaluate('window.submissions') == 0
        finally:
            await context.close()
