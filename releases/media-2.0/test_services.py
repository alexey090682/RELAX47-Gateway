import asyncio,sys,types,tempfile,unittest,json,subprocess
from pathlib import Path
from test_pipeline import m,stay,VOICES
class Response:ONLY='only';OPTIONAL='optional'
async def role(hass,user):return 'guest' if user=='guest' else 'administrator'
async def require(hass,call,minimum):
 if call.context.user_id=='guest':raise ValueError('Forbidden')
modules={
 'homeassistant':types.ModuleType('homeassistant'),
 'homeassistant.core':types.SimpleNamespace(SupportsResponse=Response),
 'homeassistant.components':types.ModuleType('homeassistant.components'),
 'homeassistant.components.tts':types.ModuleType('homeassistant.components.tts'),
 'homeassistant.components.tts.const':types.SimpleNamespace(DATA_TTS_MANAGER='tts_manager'),
 'custom_components':types.ModuleType('custom_components'),
 'custom_components.relax47_rbac':types.SimpleNamespace(async_require_role=require,async_role_for_user=role,ROLE_ADMINISTRATOR='administrator',ROLE_GUEST='guest'),
 'voluptuous':types.SimpleNamespace(Schema=lambda x:x,Required=lambda x:x,Optional=lambda x:x)}
sys.modules.update(modules)
class States:
 def __init__(self):self.items={}
 def get(self,k):return self.items.get(k)
 def async_set(self,k,state,attributes):self.items[k]=types.SimpleNamespace(state=state,attributes=attributes)
class Bus:
 def __init__(self):self.handlers={}
 def async_listen(self,k,f):self.handlers[k]=f;return lambda:None
 async_listen_once=async_listen
class Services:
 def __init__(self):self.handlers={}
 def async_register(self,d,s,f,**kwargs):self.handlers[s]=f
class Stream:
 def async_set_message(self,message):self.message=message
 async def async_stream_result(self):yield b'fake-test-tts-audio'
class Manager:
 def async_create_result_stream(self,engine,*,language,options):return Stream()
class FakeHass:
 def __init__(self,root):
  self.states=States();self.services=Services();self.bus=Bus();self.data={'tts_manager':Manager()};self.is_running=False
  self.config=types.SimpleNamespace(path=lambda *x:str(Path(root).joinpath(*x)))
  self.states.async_set('sensor.relax47_guest_journal','2',{'entries':[stay('A'),stay('B')]})
  active=stay('A');active['status']='active';self.states.async_set('sensor.relax47_current_stay','active',{'stay':active})
 async def async_add_executor_job(self,fn,*args):return await asyncio.to_thread(fn,*args)
 def async_create_background_task(self,coro,name):return asyncio.create_task(coro,name=name)
def call(id='A',user='admin',version=1,response=True):
 return types.SimpleNamespace(data={'stay_id':id,'expected_version':version},context=types.SimpleNamespace(user_id=user),return_response=response)
class TestServices(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp=tempfile.TemporaryDirectory();self.h=FakeHass(self.temp.name)
  self.root=Path(self.temp.name)/'tv'
  async def render(payload):
   q=self.h.data['relax47_stay_media']['queue'];out,path=q.paths(payload)
   args=['ffmpeg','-loglevel','error','-y','-f','lavfi','-i','color=c=blue:s=320x180:r=25','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','0.4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(out)]
   await asyncio.to_thread(subprocess.run,args,check=True)
   path.write_text(json.dumps({'state':'ready','mode':payload['mode'],'render_job_id':m.job_identity(payload),'source_signature':payload['source_signature'],'scene_count':len(payload['scenes']),'section_count':len(payload['scenes'])}))
  await m.async_setup_stay_media(self.h,self.root,render,asyncio.Lock(),lambda *args:('/api/media/test',123))
  await self.h.bus.handlers['relax47_stay_saved'](None)
 async def asyncTearDown(self):
  await self.h.bus.handlers['homeassistant_stop'](None);self.temp.cleanup()
 async def test_service_scoping_and_stale_version(self):
  with self.assertRaises(ValueError):await self.h.services.handlers['get_stay_videos'](call('B','guest'))
  with self.assertRaises(ValueError):await self.h.services.handlers['prepare_stay_videos'](call('A','guest'))
  with self.assertRaises(ValueError):await self.h.services.handlers['prepare_stay_videos'](call(version=99))
  result=await self.h.services.handlers['get_stay_videos'](call('A','guest'));self.assertEqual(result['state'],'scheduled')
 async def test_entire_worker_without_browser(self):
  await self.h.services.handlers['prepare_stay_videos'](call(response=False))
  for _ in range(150):
   await asyncio.sleep(.05)
   result=await self.h.services.handlers['get_stay_videos'](call())
   if result['state']=='ready':break
  self.assertEqual(result['state'],'ready',result)
  self.assertEqual(set(result['videos']),set(m.ALL_MODES))
  other=await self.h.services.handlers['get_stay_videos'](call('B'));self.assertFalse(other['videos'])
  await self.h.services.handlers['prepare_stay_videos'](call())
  q=self.h.data['relax47_stay_media']['queue'];self.assertIsNone(q.claim_media())
if __name__=='__main__':unittest.main()
