import asyncio,json,base64
from pathlib import Path
from playwright.async_api import async_playwright
async def main():
 async with async_playwright() as p:
  b=await p.chromium.launch(headless=False, args=['--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows'])
  try:
   c=await b.new_context();pages=await asyncio.gather(*(c.new_page() for _ in range(4)));errors=[]
   for page in pages: page.on('pageerror',lambda e:errors.append(str(e)))
   await asyncio.gather(*(page.set_content(Path('business.html').read_text(encoding='utf-8').replace('__MOVIE_URL__','data:video/webm;base64,'+base64.b64encode(Path('workload-video.webm').read_bytes()).decode())) for page in pages))
   await asyncio.wait_for(asyncio.gather(*(page.evaluate('() => startBusiness()') for page in pages)),30)
   await asyncio.sleep(6)
   for page in pages:
    result=await page.evaluate('() => workloadMetrics()');print(json.dumps(result))
    assert result['movie_frames']>0 and result['rtc_frames']>0 and result['route_searches']>1 and not result['media_error'],result
    await page.bring_to_front()
    operation=await page.evaluate('() => businessStep()')
    assert operation['paint_ms'] > 0
    await page.evaluate('() => stopBusiness()')
    assert await page.evaluate('() => peers.length === 0 && routeWorker === null && !active')
   assert not errors,errors
   print('Four-tab workload: PASS')
  finally: await b.close()
asyncio.run(main())
