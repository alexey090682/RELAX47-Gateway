import importlib.util, tempfile, unittest, copy, json
from pathlib import Path
s=importlib.util.spec_from_file_location('media',Path(__file__).parent/'changes/queue.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
VOICES={'engine':'Yandex SpeechKit','yandex':'Марина · дружелюбная','piper':'ru_RU-irina-medium','piperEntity':'tts.piper'}
def stay(id='A'):
 return {'id':id,'version':1,'guest':'Гость','check_in':'2026-10-06 15:00:00','check_out':'2026-10-08 13:00:00','status':'planned','access_snapshot':{'floors':[True,True,False],'spa':True,'lights':True,'climate':True,'gate':True,'music':True,'tvSocket':True},'spa_snapshot':{'by_agreement':True,'sessions':[]}}
def manifest(p):
 return {mode:{'state':'ready','mode':mode,'source_signature':m.canonical(p)} for mode in m.ALL_MODES}
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.q=m.StayMediaQueue(self.tmp.name);self.q.initialize_media()
 def tearDown(self):self.tmp.cleanup()
 def test_future_stay_due_two_hours_and_restart(self):
  self.q.observe(stay(),VOICES,now=100)
  self.assertIsNone(self.q.claim_media(now=7299))
  q=m.StayMediaQueue(self.tmp.name);q.initialize_media()
  p=q.claim_media(now=7300);self.assertEqual(p['stayId'],'A')
  self.assertIsNone(q.claim_media(now=7301))
 def test_isolated_same_guest_different_stays(self):
  self.q.observe(stay('A'),VOICES,now=0,manual=True);self.q.observe(stay('B'),VOICES,now=1,manual=True)
  a=self.q.claim_media(now=2);b=self.q.claim_media(now=2)
  self.assertNotEqual(a['mediaFingerprint'],b['mediaFingerprint'])
  self.q.publish(a,manifest(a));self.assertFalse(self.q.get_media('B')['manifest'])
 def test_version_money_notes_do_not_rebuild(self):
  s=stay();self.q.observe(s,VOICES,now=0,manual=True);p=self.q.claim_media(now=0);self.q.publish(p,manifest(p))
  s.update(version=2,note='Новая заметка',tariff_snapshot={'total':12})
  self.q.observe(s,VOICES,now=1);m.validate_bound_profile(p,s)
  self.assertEqual(self.q.get_media('A')['state'],'ready')
 def test_changed_dates_require_manual_after_ready(self):
  s=stay();self.q.observe(s,VOICES,now=0,manual=True);p=self.q.claim_media(now=0);self.q.publish(p,manifest(p))
  s['check_out']='2026-10-09 13:00:00';s['version']=2
  self.q.observe(s,VOICES,now=1)
  self.assertEqual(self.q.get_media('A')['state'],'outdated');self.assertFalse(self.q.get_media('A')['manifest'])
  self.assertIsNone(self.q.claim_media(now=999999))
  self.q.observe(s,VOICES,now=2,manual=True);self.assertIsNotNone(self.q.claim_media(now=2))
 def test_pending_edit_keeps_deadline_and_latest_input(self):
  s=stay();self.q.observe(s,VOICES,now=10);s['guest']='Новый гость';s['version']=2
  self.q.observe(s,VOICES,now=20)
  self.assertEqual(self.q.get_media('A')['due_at'],7210)
  self.assertEqual(self.q.claim_media(now=7210)['guest'],'Новый гость')
 def test_stale_build_cannot_publish(self):
  s=stay();self.q.observe(s,VOICES,now=0,manual=True);p=self.q.claim_media(now=0)
  s['guest']='Другой';self.q.observe(s,VOICES,now=1)
  self.assertFalse(self.q.publish(p,manifest(p)))
 def test_closed_deleted_stay_cannot_publish(self):
  s=stay();self.q.observe(s,VOICES,now=0,manual=True);p=self.q.claim_media(now=0)
  s['status']='cancelled';self.q.observe(s,VOICES,now=1)
  self.assertFalse(self.q.publish(p,manifest(p)))
  self.assertIsNone(self.q.claim_media(now=99999))
 def test_retry_bounded_and_recovered(self):
  self.q.observe(stay(),VOICES,now=0,manual=True)
  for i in range(3):
   p=self.q.claim_media(now=1e20);self.q.fail(p,'failure')
  self.assertEqual(self.q.get_media('A')['state'],'error')
  self.assertIsNone(self.q.claim_media(now=1e20))
 def test_inflight_restart(self):
  self.q.observe(stay(),VOICES,now=0,manual=True);p=self.q.claim_media(now=0)
  self.q.initialize_media();self.assertEqual(self.q.claim_media(now=1)['mediaFingerprint'],p['mediaFingerprint'])
 def test_duplicate_manual_has_one_work_item(self):
  self.q.observe(stay(),VOICES,now=0,manual=True);p=self.q.claim_media(now=0)
  self.q.observe(stay(),VOICES,now=1,manual=True)
  self.assertIsNone(self.q.claim_media(now=1));self.assertTrue(self.q.publish(p,manifest(p)))
 def test_foreign_or_partial_manifest_rejected(self):
  p=m.source_profile(stay(),VOICES);b=m.source_profile(stay('B'),VOICES)
  with self.assertRaises(ValueError):self.q.publish(p,{'full':manifest(p)['full']})
  with self.assertRaises(ValueError):self.q.publish(p,manifest(b))
 def test_four_modes_unique_content_and_personalized_future(self):
  p=m.source_profile(stay(),VOICES);parts=m.build_media_scenes(p)
  self.assertEqual(set(parts),set(m.PARTS))
  ids=[x['id'] for scenes in parts.values() for x in scenes]
  self.assertEqual(len(ids),len(set(ids)))
  self.assertIn('Гость',parts['presentation'][0]['message'])
  self.assertIn('6 октября',parts['presentation'][0]['message'])
  self.assertTrue(all(x['image_path'].startswith('/local/relax47/welcome/media/') for scenes in parts.values() for x in scenes))
 def test_bundle_path_rejects_traversal(self):
  with self.assertRaises(ValueError):m.status_output(self.tmp.name,{'mode':'full','bundle_key':'../x'})
 def test_legacy_job_identity_and_paths_unchanged(self):
  p={'mode':'presentation','source_signature':json.dumps({'guest':'X'}),'scenes':[{'message':'hello'}]}
  q=m.TvRenderQueue(self.tmp.name);key,state=q.enqueue(p)
  self.assertEqual(q.paths(p)[0],Path(self.tmp.name)/'jobs'/key/'presentation.mp4')
 def test_same_legacy_job_and_new_tables_coexist(self):
  p={'mode':'rules','source_signature':'{}','scenes':[{'message':'hello'}]}
  self.q.enqueue(p);self.q.observe(stay(),VOICES,now=0)
  self.q.initialize_media();self.assertEqual(self.q.statistics()['queued'],1)
if __name__=='__main__':unittest.main()
