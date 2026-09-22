import asyncio
from contextlib import closing
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('adapter', ROOT/'relational_store.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture():
    stays = [{'id':'stay-1','version':3,'guest':'Guest','check_in':'2026-09-20 17:00:00','check_out':'2026-09-22 17:00:00',
              'tariff_snapshot':{'amount':12345.67},'unknown_field':{'x':[1,None,False]}}]
    vehicles = {'vehicle-1':{'vehicle_id':'vehicle-1','plate':'А123ВС47','stay_id':'stay-1','affiliation':'stay'},
                'vehicle-2':{'vehicle_id':'vehicle-2','plate':'А123ВС47','stay_id':'unknown','affiliation':'unknown'},
                'vehicle-3':{'vehicle_id':'vehicle-3','plate':'В123ВС47','stay_id':'administrative_passes','affiliation':'administrative'}}
    return {m.STORES[0]:dict(entries=stays,current_stay=deepcopy(stays[0]),system_events=[{'id':'s1','at':'2026-09-20T00:00:00Z','data':{'n':5}}],
                            spa_sessions=[{'start':'2026-09-21T10:00:00','end':'2026-09-21T12:00:00'}], empty=[],settings={'x':True}),
        m.STORES[1]:dict(vehicles=vehicles,
            passes={'А123ВС47':{'status':'active','vehicle_id':'vehicle-1','stay_id':'stay-1','created_at':'2026-09-20'},
                    'В123ВС47':{'status':'active','vehicle_id':'vehicle-3','stay_id':'administrative_passes'}},
            requests={'request-old':{'id':'request-old','plate':'А123ВС47','stay_id':'current','status':'pending'}},
            gate_events=[{'id':'gate-1','vehicle_id':'vehicle-1','stay_id':'current','event':'entry','source_id':'1','source':'telegram','created_at':'2026-09-20T00:00:00Z'}],
            queue=[{'id':'job1','status':'completed','plate':'А123ВС47'}], audit=[{'id':'audit1','action':'saved'}]),
        m.STORES[2]:{'events':[{'id':'zone1','stay_id':'current','rule':'bath'}],'dedupe':['a','b'],'sources':{'x':{'nested':['a',{'z':2}]}}},
        m.STORES[3]:{'violation_events':[{'id':'violation1','stay_id':'stay-1','zone_id':'bath','media':['file.jpg']}], 'providers':{'local':{'model':'test'}}},
        m.STORES[4]:{'settings':{'x':1}},m.STORES[5]:{'deliveries':[{'id':'delivery1','status':'delivered'}],'queue':[]},
        m.STORES[6]:None,m.STORES[7]:None}


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'relax47_v8/relax47.db'
        self.path.parent.mkdir()
        self.docs = fixture()
        with sqlite3.connect(self.path) as c:
            c.executescript((ROOT/'sql_schemas/schema-11.sql').read_text())
            c.execute('INSERT INTO properties VALUES(?,?,?,?,?)',('relax47',1,'Europe/Moscow','2026','2026'))
            c.execute('INSERT INTO relax47_meta VALUES(?,?)',('runtime_backend','sqlite_store_v1'))
            c.execute('INSERT INTO relax47_meta VALUES(?,?)',('migration_stage','ha_store_compatibility'))
            for key,data in self.docs.items():
                envelope=dict(version=1,minor_version=1,key=key,data=data,unrecognized_envelope='preserve')
                c.execute('INSERT INTO runtime_documents VALUES(?,?,?,?,?,?)',('relax47',key,'ha_store',7,'2026',m.dumps(envelope)))
        for key in m.STORES:
            p=self.root/'custom_components'/key.split('.')[0]/'_relax47_sql_store.py'
            p.parent.mkdir(parents=True)
            p.write_bytes((ROOT/'relational_store.py').read_bytes())

    def migrate(self):
        with closing(m.connect(self.path)) as c:
            m.migrate(c,self.path)

    def hass(self):
        async def executor(fn,*args):
            return await asyncio.to_thread(fn,*args)
        return SimpleNamespace(config=SimpleNamespace(path=lambda *p:str(self.root.joinpath(*p))), async_add_executor_job=executor)

    async def test_full_migration_and_exact_reverse_read(self):
        self.migrate()
        with closing(m.connect(self.path)) as c:
            for key,data in self.docs.items():self.assertEqual(m.read_data(c,key),data)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(c.execute('SELECT count(*) FROM stays').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM vehicles').fetchone()[0],3)
            self.assertEqual(c.execute('SELECT count(*) FROM passes').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM source_snapshots').fetchone()[0],8)
            self.assertEqual(c.execute('SELECT context_kind,stay_id FROM passes WHERE vehicle_id=?',('vehicle-3',)).fetchone(),('administrative',None))
            self.assertEqual(c.execute("SELECT value FROM relax47_meta WHERE key='runtime_backend'").fetchone(),(m.BACKEND,))

    async def test_save_restart_preserves_ids_money_unknown_fields(self):
        s=m.Store(self.hass(),1,m.STORES[0]);data=await s.async_load()
        data['entries'][0]['version']+=1;data['entries'][0]['guest']='Edited'
        await s.async_save(data)
        fresh=m.Store(self.hass(),1,m.STORES[0]);self.assertEqual(await fresh.async_load(),data)
        with closing(m.connect(self.path)) as c:
            old=json.loads(c.execute("SELECT payload_json FROM runtime_documents WHERE namespace=?",(m.STORES[0],)).fetchone()[0])
            self.assertEqual(old['data'],self.docs[m.STORES[0]])
            c.execute("UPDATE runtime_documents SET payload_json='{}'")
            c.commit()
        self.assertEqual(await fresh.async_load(),data) # no fallback/read from legacy documents

    async def test_pass_update_retains_legacy_id_and_fk(self):
        s=m.Store(self.hass(),1,m.STORES[1]);data=await s.async_load()
        with closing(m.connect(self.path)) as c:before=c.execute('SELECT id FROM passes ORDER BY id').fetchall()
        data['passes']['А123ВС47']['status']='expired'
        await s.async_save(data)
        self.assertEqual(await m.Store(self.hass(),1,m.STORES[1]).async_load(),data)
        with closing(m.connect(self.path)) as c:
            self.assertEqual(before,c.execute('SELECT id FROM passes ORDER BY id').fetchall())
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

    async def test_repeat_migration_does_not_duplicate(self):
        self.migrate();self.migrate()
        with closing(m.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM passes').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM runtime_migration_runs').fetchone()[0],1)

    async def test_new_orphan_link_rejected_and_save_rolled_back(self):
        s=m.Store(self.hass(),1,m.STORES[1]);original=await s.async_load();data=deepcopy(original)
        data['requests']['request-new']={'id':'request-new','vehicle_id':'vehicle-1','stay_id':'missing','status':'pending'}
        with self.assertRaisesRegex(ValueError,'Unresolved new'):await s.async_save(data)
        self.assertEqual(s.revision,7)
        self.assertEqual(await m.Store(self.hass(),1,m.STORES[1]).async_load(),original)

    async def test_concurrent_stale_save_rejected(self):
        first=m.Store(self.hass(),1,m.STORES[0]);second=m.Store(self.hass(),1,m.STORES[0])
        a=await first.async_load();b=await second.async_load()
        a['settings']['x']=False;await first.async_save(a)
        b['settings']['x']='stale'
        with self.assertRaisesRegex(RuntimeError,'stale'):await second.async_save(b)
        self.assertEqual(await m.Store(self.hass(),1,m.STORES[0]).async_load(),a)

    async def test_migration_failure_rolls_back_schema_and_data(self):
        with closing(m.connect(self.path)) as c:
            before=c.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
            with patch.object(m,'read_data',return_value={'wrong':True}):
                with self.assertRaisesRegex(RuntimeError,'parity'):m.migrate(c,self.path)
            self.assertEqual(c.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall(),before)
            self.assertEqual(c.execute("SELECT value FROM relax47_meta WHERE key='schema_version'").fetchone(),('11',))
        self.migrate()

    async def test_partial_deployment_cannot_start_migration(self):
        p=self.root/'custom_components'/m.STORES[2].split('.')[0]/'_relax47_sql_store.py';p.write_text('old')
        with closing(m.connect(self.path)) as c:
            with self.assertRaisesRegex(RuntimeError,'eight'):m.migrate(c,self.path)
            self.assertEqual(c.execute("SELECT value FROM relax47_meta WHERE key='schema_version'").fetchone(),('11',))

    async def test_collection_removal_preserves_history_and_roundtrip(self):
        s=m.Store(self.hass(),1,m.STORES[0]);data=await s.async_load()
        data['entries']=[];data.pop('settings');data['empty']={'key':'changed container'}
        await s.async_save(data)
        self.assertEqual(await m.Store(self.hass(),1,m.STORES[0]).async_load(),data)
        with closing(m.connect(self.path)) as c:self.assertEqual(c.execute('SELECT count(*) FROM stays').fetchone()[0],1)

    async def test_cancelled_save_waits_for_commit_and_updates_revision(self):
        s=m.Store(self.hass(),1,m.STORES[0]);data=await s.async_load();data['settings']['x']='new'
        original=s._save
        import threading
        started=threading.Event();release=threading.Event()
        def slow(payload):started.set();release.wait(5);original(payload)
        s._save=slow
        task=asyncio.create_task(s.async_save(data))
        await asyncio.to_thread(started.wait,5);task.cancel();release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(s.revision,8)
        self.assertEqual(await m.Store(self.hass(),1,m.STORES[0]).async_load(),data)

    async def test_rollback_retains_writes_made_after_cutover(self):
        s=m.Store(self.hass(),1,m.STORES[0]);data=await s.async_load()
        data['entries'].append({'id':'stay-new','guest':'New','check_in':'2026-10-01','check_out':'2026-10-02','version':1})
        await s.async_save(data)
        with closing(m.connect(self.path)) as c:
            m.rollback_to_compatibility(c)
            restored=json.loads(c.execute("SELECT payload_json FROM runtime_documents WHERE namespace=? AND item_key='ha_store'",(m.STORES[0],)).fetchone()[0])
            self.assertEqual(restored['data'],data)
            self.assertEqual(c.execute('SELECT count(*) FROM stays').fetchone()[0],2)

    async def test_new_stay_vehicle_request_pass_roundtrip(self):
        s=m.Store(self.hass(),1,m.STORES[1]);data=await s.async_load()
        data['vehicles']['v-new']={'vehicle_id':'v-new','plate':'Н007СС47','stay_id':'stay-1','affiliation':'stay'}
        data['requests']['r-new']={'id':'r-new','plate':'Н007СС47','vehicle_id':'v-new','stay_id':'stay-1','status':'created'}
        data['passes']['Н007СС47']={'id':'p-new','vehicle_id':'v-new','stay_id':'stay-1','request_id':'r-new','status':'active'}
        await s.async_save(data)
        self.assertEqual(await m.Store(self.hass(),1,m.STORES[1]).async_load(),data)
        with closing(m.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT vehicle_id,stay_id,request_id FROM passes WHERE id=?',('p-new',)).fetchone(),('v-new','stay-1','r-new'))
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

    async def test_large_existing_queue_is_preserved_without_replay(self):
        s=m.Store(self.hass(),1,m.STORES[5]);data=await s.async_load()
        data['deliveries']=[{'id':'d'+str(i),'status':'delivered','media':['x.jpg'],'text':'текст'*80} for i in range(1500)]
        await s.async_save(data)
        self.assertEqual(await m.Store(self.hass(),1,m.STORES[5]).async_load(),data)
        data['deliveries'][700]['status']='confirmed'
        await s.async_save(data)
        self.assertEqual(await m.Store(self.hass(),1,m.STORES[5]).async_load(),data)

    async def test_failed_cutover_keeps_existing_sql_available(self):
        s=m.Store(self.hass(),1,m.STORES[0])
        with patch.object(m,'write_data',side_effect=ValueError('unsupported legacy shape')):
            data=await s.async_load()
        self.assertTrue(s.compatibility_mode)
        self.assertEqual(data,self.docs[m.STORES[0]])
        data['settings']['x']='saved after rejected migration'
        await s.async_save(data)
        fresh=m.Store(self.hass(),1,m.STORES[0])
        self.assertEqual(await fresh.async_load(),data)
        self.assertTrue(fresh.compatibility_mode)


if __name__=='__main__':unittest.main()
