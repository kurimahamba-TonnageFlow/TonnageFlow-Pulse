"""Local-only navigation QA. Network responses are unavailable fixtures, never live data."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

OUTPUT = Path('docs/home-preview')
BASE = 'http://127.0.0.1:5181'

def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        for name, width, height in [('desktop',1672,941), ('tablet-landscape',1180,820), ('tablet-portrait',820,1180), ('mobile',390,844)]:
            page = browser.new_page(viewport={'width':width,'height':height}, device_scale_factor=1)
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/health/**',lambda r:r.fulfill(status=503,content_type='application/json',body='{}'))
            page.route('**/api/**',lambda r:r.fulfill(status=503,content_type='application/json',body='{}') if r.request.resource_type in ('fetch','xhr') else r.continue_())
            page.goto(BASE)
            page.get_by_role('heading',name='Welcome to TonnageFlow Pulse').wait_for()
            page.get_by_text('API unavailable',exact=True).wait_for()
            page.screenshot(path=str(OUTPUT/f'{name}.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth') <= width
            for title in ['Warehouse','Cleaning Plant','Settings']:
                trigger=page.get_by_role('button',name=title,exact=True).first
                trigger.click()
                modal=page.get_by_role('dialog',name=title)
                assert modal.is_visible()
                page.keyboard.press('Tab')
                assert modal.evaluate('(el)=>el.contains(document.activeElement)')
                page.keyboard.press('Escape')
                assert not modal.is_visible()
                assert trigger.evaluate('(el)=>document.activeElement===el')
                assert page.url.rstrip('/')==BASE
            page.get_by_role('button',name='Search',exact=True).click()
            assert page.get_by_role('searchbox').evaluate('(el)=>document.activeElement===el')
            page.get_by_role('searchbox').fill('QA')
            assert page.get_by_role('region',name='All Destinations').get_by_role('link',name='QA',exact=True).is_visible()
            page.get_by_role('searchbox').fill('')
            for label in ['Production','QA','Performance','Operational Intelligence','Management','Reports']:
                page.get_by_role('link',name=label,exact=True).first.click()
                page.get_by_role('heading',name='Management sign in',exact=False).wait_for()
                assert '/management' in page.url
                page.go_back()
                page.get_by_role('heading',name='Welcome to TonnageFlow Pulse').wait_for()
            page.get_by_role('link',name='Engineering Workspace',exact=True).first.click()
            assert page.url.endswith('/engineering')
            page.get_by_role('link',name='Home',exact=True).click()
            page.get_by_role('heading',name='Welcome to TonnageFlow Pulse').wait_for()
            page.get_by_role('link',name='HMI',exact=True).first.click()
            assert page.url.endswith('/hmi')
            page.get_by_role('link',name='Home',exact=True).click()
            page.get_by_role('heading',name='Welcome to TonnageFlow Pulse').wait_for()
            assert not errors,errors
            results.append({'viewport':name,'width':width,'horizontal_overflow':False,'modal_keyboard_focus':'passed','protected_navigation':'passed','home_links':'passed','browser_errors':errors})
            page.close()
        browser.close()
    (OUTPUT/'verification.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results))

if __name__=='__main__':main()
