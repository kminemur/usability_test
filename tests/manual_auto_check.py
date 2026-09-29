import tempfile,time,threading,json,sys,os
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from pathlib import Path
from app import Monitor
from automation import PRESETS
with tempfile.TemporaryDirectory(prefix='memory-lab-auto-') as d:
 PRESETS['desktop_auto']['seconds']=1
 m=Monitor(Path(d));t=threading.Thread(target=m.loop);t.start()
 try:
  m.automation.start('desktop_auto')
  end=time.monotonic()+150
  while time.monotonic()<end:
   status=dict(m.automation.status)
   if not status['active']:
    print(m.path.with_suffix('.desktop').joinpath('worker.log').read_text(errors='replace'))
    print(m.path.with_suffix('.desktop').joinpath('status.json').read_text(encoding='utf-8-sig'))
    raise AssertionError(status)
   if status.get('pdf_search_requests',0)>=4 and status.get('desktop',{}).get('iteration',0)>=2:
    print(json.dumps(status,ensure_ascii=True));break
   time.sleep(1)
  else:raise AssertionError(status)
  time.sleep(int(os.environ.get('MEMORY_LAB_QA_WAIT', '0')))
  assert any(r.get('pdf_rss_mib',0)>0 for r in m.history)
  lines=m.path.with_suffix('.latency.jsonl').read_text().splitlines()
  assert any(json.loads(x)['rtc_frames']>0 and json.loads(x)['movie_frames']>0 and json.loads(x)['route_searches']>0 for x in lines)
 finally:
  m.automation.cancel.set();m.automation.thread.join(35);m.done.set();t.join()
 print('End-to-end automatic desktop / PDF requests / video / routes / WebRTC / stop: PASS')
