"""Local-only automatic receiver integration, with no completion marker/callback."""
import json
import shutil
import time
import uuid
from pathlib import Path
import pytest
import yaml
from convest.config import WORKSPACE,atomic_json
from convest.jobs import checksum
from convest.ingest.config import load_service_config
from convest.ingest.worker import worker_context,scan
from convest.ingest.inputs import snapshot,check_input,stage_input,InputChanged
from convest.ingest.batches import BatchPublisher as Publisher
from convest.verify import verify
from conversion_helpers import make_conversion_bag
from convest.ingest.control import request_maintenance


@pytest.fixture
def service(tmp_path,monkeypatch):
    import convest.pipeline as pipeline
    def serial(items,config,contract,staging,workers):
        for i,item in enumerate(items):
            stage=staging/f'serial-{i}'
            try:yield item,stage,pipeline.prepare_bag(item,config,contract,stage),None
            except Exception as exc:yield item,stage,None,exc
    monkeypatch.setattr(pipeline,'prepared_in_order',serial)
    work=WORKSPACE/'outputs'/('test_ingest_'+uuid.uuid4().hex)
    raw={'schema_version':1,'data_root':str(tmp_path/'data'),'state_root':str(tmp_path/'state'),
         'work_root':str(work),'poll_seconds':1,'stable_scans':2,'stable_seconds':1,'retry_seconds':1,'max_attempts':3,'max_jobs_per_scan':16,'max_checks_per_scan':32,
         'initial_history':{'mode':'all'},'routes':[{'robot':r,'group':g} for r in ('sharpa','wuji') for g in ('A','B')]}
    path=tmp_path/'config.yaml';path.write_text(yaml.safe_dump(raw));cfg=load_service_config(path)
    for route in cfg['routes']:Path(route['source']).mkdir(parents=True)
    yield cfg
    if work.exists():shutil.rmtree(work)


def bag(cfg,route=0,number=1):
    r=cfg['routes'][route]
    return make_conversion_bag(Path(r['source'])/f'episode{number}',r['robot'],frames=18)


def ready_scan(cfg,q,t=None):
    t=t or time.time_ns();scan(cfg,q,now_ns=t)
    return scan(cfg,q,now_ns=t+2_000_000_000)


def test_four_routes_append_atomic_generations_read_only_originals(service):
    cfg=service;originals={}
    for i in range(4):
        b=bag(cfg,i);originals[str(b)]={p.name:checksum(p) for p in b.iterdir()}
    with worker_context(cfg) as q:
        first=scan(cfg,q);assert first['counts']=={'waiting_stable':4}
        report=scan(cfg,q,now_ns=time.time_ns()+2_000_000_000);assert report['counts']=={'published':4}
        pinned=[]
        for r in cfg['routes']:
            root=Publisher(r,cfg['fingerprint']).current();pinned.append(root)
            assert Path(r['output']).is_dir() and (Path(r['output'])/'1_1_data_lerobot').is_symlink()
            assert verify(root)['episodes']==1
        old=pinned[2];old_meta=(old/'meta/info.json').read_bytes();old_file=next(old.glob('data/**/*.parquet'));old_sha=checksum(old_file)
        bag(cfg,2,2);assert ready_scan(cfg,q)['counts']=={'published':5}
        pub=Publisher(cfg['routes'][2],cfg['fingerprint']);new=pub.current()
        assert new!=old and verify(new)['episodes']==2
        assert (old/'meta/info.json').read_bytes()==old_meta and checksum(old_file)==old_sha
        assert (new/old_file.relative_to(old)).stat().st_ino==old_file.stat().st_ino
        assert scan(cfg,q)['counts']=={'published':5} and pub.current()==new
    for name,files in originals.items():assert {p.name:checksum(p) for p in Path(name).iterdir()}==files
    assert not [p for p in Path(cfg['work_root']).iterdir() if p.is_dir()]


def test_partial_bag_waits_despite_successor_then_last_bag_auto_processes(service):
    cfg=service;b=bag(cfg,2,1);data=next(b.glob('*.db3'));full=data.read_bytes();data.write_bytes(full[:100])
    t=time.time_ns()
    with worker_context(cfg) as q:
        scan(cfg,q,now_ns=t);bag(cfg,2,2)
        report=scan(cfg,q,now_ns=t+2_000_000_000)
        assert q.get('wuji-A','episode1')['state']=='waiting_upload'
        assert q.get('wuji-A','episode2')['state']=='waiting_stable'
        # The last bag completes without episode3 or any marker.
        scan(cfg,q,now_ns=t+4_000_000_000)
        assert q.get('wuji-A','episode2')['state']=='published'
        data.write_bytes(full)
        scan(cfg,q,now_ns=t+6_000_000_000)
        scan(cfg,q,now_ns=t+8_000_000_000)
        assert q.get('wuji-A','episode1')['state']=='published'
        assert verify(Publisher(cfg['routes'][2],cfg['fingerprint']).current())['episodes']==2


def test_repeated_fast_scans_do_not_fake_stability_and_restart_keeps_observations(service):
    cfg=service;bag(cfg,2);t=time.time_ns()
    with worker_context(cfg) as q:
        scan(cfg,q,now_ns=t)
        for _ in range(5):assert scan(cfg,q,now_ns=t+10)['counts']=={'waiting_stable':1}
    with worker_context(cfg) as q:
        assert scan(cfg,q,now_ns=t+2_000_000_000)['counts']=={'published':1}


def test_new_successor_prioritizes_predecessor_but_not_readiness(service,monkeypatch):
    cfg=service;bag(cfg,2,1);bag(cfg,2,2);t=time.time_ns();calls=[]
    import convest.ingest.worker as worker
    real=worker.check_input
    def track(b,*args,**kwargs):calls.append(b.name);return real(b,*args,**kwargs)
    monkeypatch.setattr(worker,'check_input',track)
    with worker_context(cfg) as q:
        scan(cfg,q,now_ns=t);bag(cfg,2,3)
        scan(cfg,q,now_ns=t+2_000_000_000)
        # episode2 newly gets priority from episode3, ahead of episode1.
        assert calls[0]=='episode2'
        assert q.get('wuji-A','episode3')['state']=='waiting_stable'


def test_mutation_during_staging_discards_attempt_and_retries(service,monkeypatch):
    cfg=service;b=bag(cfg,2);import convest.ingest.worker as worker
    real=worker.stage_input;first=[True]
    def change(*args,**kw):
        result=real(*args,**kw)
        if first[0]:
            first[0]=False;state=json.loads((b/'collection_state.json').read_text());state['upload_note']='continued upload'
            atomic_json(b/'collection_state.json',state)
        return result
    monkeypatch.setattr(worker,'stage_input',change)
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'waiting_stable':1}
        assert Publisher(cfg['routes'][2],cfg['fingerprint']).current() is None
        assert ready_scan(cfg,q)['counts']=={'published':1}


def test_mutation_after_conversion_before_publication_never_exposes_candidate(service,monkeypatch):
    cfg=service;b=bag(cfg,2);import convest.ingest.worker as worker
    real=worker.run
    def change(*args,**kw):
        result=real(*args,**kw);p=b/'metadata.yaml';p.write_text(p.read_text()+'\n# upload continued\n');return result
    monkeypatch.setattr(worker,'run',change)
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'waiting_stable':1}
        assert Publisher(cfg['routes'][2],cfg['fingerprint']).current() is None


def test_mutation_in_final_precommit_guard_blocks_switch(service,monkeypatch):
    cfg=service;b=bag(cfg,2);import convest.ingest.publish as module
    real=module.writer.write_metadata
    def change(*args,**kw):
        result=real(*args,**kw)
        if '.worker-generations' in str(args[0]):
            p=b/'metadata.yaml';p.write_text(p.read_text()+'\n# late write\n')
        return result
    monkeypatch.setattr(module.writer,'write_metadata',change)
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'waiting_stable':1}
        assert Publisher(cfg['routes'][2],cfg['fingerprint']).current() is None


def test_crash_after_pointer_commit_reconciles_without_duplicate(service,monkeypatch):
    cfg=service;bag(cfg,2)
    with worker_context(cfg) as q:
        real=q.put;first=[True]
        def fail(route,episode,identity,state,**kw):
            if state=='published' and first[0]:first[0]=False;raise OSError('journal failure')
            return real(route,episode,identity,state,**kw)
        monkeypatch.setattr(q,'put',fail)
        assert ready_scan(cfg,q)['counts']=={'retry':1}
        current=Publisher(cfg['routes'][2],cfg['fingerprint']).current()
    with worker_context(cfg) as q:
        assert scan(cfg,q)['counts']=={'published':1}
        assert Publisher(cfg['routes'][2],cfg['fingerprint']).current()==current
        assert verify(current)['episodes']==1


def test_failed_switch_preserves_previous_generation_and_retry(service,monkeypatch):
    import convest.ingest.publish as publishing
    cfg=service;bag(cfg,2)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);pub=Publisher(cfg['routes'][2],cfg['fingerprint']);old=pub.current();bag(cfg,2,2)
        real=publishing.os.replace
        def fail(src,dst):
            if Path(dst).name=='current':raise OSError('pointer failure')
            return real(src,dst)
        monkeypatch.setattr(publishing.os,'replace',fail)
        report=ready_scan(cfg,q);assert report['counts']=={'published':1,'retry':1}
        assert pub.current()==old and verify(old)['episodes']==1
        monkeypatch.setattr(publishing.os,'replace',real)
        row=q.get('wuji-A','episode2');q.put('wuji-A','episode2',row['identity'],'retry',next_ns=0)
        assert scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)['counts']=={'published':2}


def test_source_mutation_after_publication_is_flagged_not_silently_overwritten(service):
    cfg=service;b=bag(cfg,2)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);pub=Publisher(cfg['routes'][2],cfg['fingerprint']);old=pub.current()
        p=b/'metadata.yaml';p.write_text(p.read_text()+'\n# changed\n')
        assert scan(cfg,q)['counts']=={'conflict':1};assert pub.current()==old


def test_single_worker_lock_and_route_failure_does_not_stop_others(service,monkeypatch):
    cfg=service;bag(cfg,0);bag(cfg,2);import convest.ingest.worker as worker
    real=worker.run
    def fail(config,*args,**kw):
        if config['robot']=='sharpa':raise OSError('temporary failure')
        return real(config,*args,**kw)
    monkeypatch.setattr(worker,'run',fail)
    with worker_context(cfg) as q:
        with pytest.raises(BlockingIOError):
            with worker_context(cfg):pass
        assert ready_scan(cfg,q)['counts']=={'retry':1,'published':1}


def test_staging_rejects_changed_bytes_and_symlinks(service,tmp_path):
    cfg=service;b=bag(cfg,2);expected=snapshot(b);proof=check_input(b,cfg['routes'][2],expected)
    p=next(b.glob('*.db3'));p.write_bytes(p.read_bytes()+b'change')
    with pytest.raises(InputChanged):stage_input(b,tmp_path/'copy',proof)
    (b/'link').symlink_to('/etc/passwd')
    with pytest.raises(InputChanged):snapshot(b)


@pytest.mark.parametrize('failure',['missing_volume','count_mismatch','missing_stream'])
def test_structurally_incomplete_but_stable_bags_wait_and_retry(service,failure):
    import sqlite3
    cfg=service;b=bag(cfg,2);meta_path=b/'metadata.yaml';original=meta_path.read_bytes()
    meta=yaml.safe_load(original);info=meta['rosbag2_bagfile_information']
    if failure=='missing_volume':info['relative_file_paths'].append('not_uploaded.db3')
    elif failure=='count_mismatch':info['message_count']+=1
    else:
        with sqlite3.connect(b/'data.db3') as db:
            tid=db.execute("SELECT id FROM topics WHERE name='/cam0/depth/image_raw'").fetchone()[0]
            db.execute('DELETE FROM messages WHERE topic_id=?',(tid,));deleted=18
        info['message_count']-=deleted
        for topic in info['topics_with_message_count']:
            if topic['topic_metadata']['name']=='/cam0/depth/image_raw':topic['message_count']=0
    meta_path.write_text(yaml.safe_dump(meta))
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'waiting_upload':1}
        assert not Path(cfg['routes'][2]['output']).exists()
        assert q.get('wuji-A','episode1')['attempts']==0
        if failure!='missing_stream':
            meta_path.write_bytes(original)
            assert ready_scan(cfg,q)['counts']=={'published':1}


def test_input_change_while_copying_is_rejected_by_hash(service,tmp_path,monkeypatch):
    import convest.ingest.inputs as inputs
    cfg=service;b=bag(cfg,2);proof=check_input(b,cfg['routes'][2],snapshot(b));real=inputs.shutil.copyfileobj
    def corrupt(source,target,*args,**kwargs):
        result=real(source,target,*args,**kwargs)
        target.write(b'changed during copy');return result
    monkeypatch.setattr(inputs.shutil,'copyfileobj',corrupt)
    with pytest.raises(InputChanged,match='Staged bytes'):stage_input(b,tmp_path/'copy',proof)


def test_default_service_budgets_prevent_one_scan_processing_unbounded_backlog(service):
    cfg=service;cfg['max_jobs_per_scan']=1;bag(cfg,2,1);bag(cfg,2,2);t=time.time_ns()
    with worker_context(cfg) as q:
        scan(cfg,q,now_ns=t)
        result=scan(cfg,q,now_ns=t+2_000_000_000);assert result['counts'].get('published')==1
        result=scan(cfg,q,now_ns=t+4_000_000_000);assert result['counts']=={'published':2}


def test_one_full_readiness_per_input_and_output_unchanged(service,monkeypatch):
    """The optimization removes full checks, not output checks or sensor data."""
    import convest.ingest.inputs as inputs
    import convest.ingest.worker as worker
    import convest.jobs as jobs
    import pyarrow.parquet as pq
    cfg=service;bag(cfg,0);bag(cfg,1);calls=[]
    real=jobs.check_ready
    def check(*args,**kw):calls.append(str(args[0]));return real(*args,**kw)
    monkeypatch.setattr(inputs,'check_ready',check)
    monkeypatch.setattr(jobs,'check_ready',check)
    # A is the optimized path; B deliberately uses the original standalone
    # job precheck as a baseline, on identical real CDR messages.
    stage=worker.stage_input
    def baseline(b,target,*args,**kw):
        receipt=stage(b,target,*args,**kw)
        return None if '/B/' in str(b) else receipt
    monkeypatch.setattr(worker,'stage_input',baseline)
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'published':2}
    assert len(calls)==3  # one for each source, one baseline working copy
    a=Publisher(cfg['routes'][0],cfg['fingerprint']).current()
    b=Publisher(cfg['routes'][1],cfg['fingerprint']).current()
    ai=json.loads((a/'meta/info.json').read_text());bi=json.loads((b/'meta/info.json').read_text())
    assert ai['features']==bi['features']
    assert ai['total_frames']==bi['total_frames']
    af=next(a.glob('data/**/*.parquet'));bf=next(b.glob('data/**/*.parquet'))
    assert pq.read_table(af).equals(pq.read_table(bf))
    for f in a.glob('videos/**/*.mp4'):assert checksum(f)==checksum(b/f.relative_to(a))
    assert verify(a,full_video=True)['status']==verify(b,full_video=True)['status']=='passed'


def test_final_content_check_catches_same_size_same_mtime_change(service,tmp_path):
    import os
    from convest.ingest.inputs import verify_input_content
    cfg=service;b=bag(cfg,2);proof=check_input(b,cfg['routes'][2],snapshot(b))
    p=b/'collection_state.json';st=p.stat();data=p.read_bytes()
    p.write_bytes(data.replace(b'finalized',b'finalizeX',1));os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
    # Even if a filesystem snapshot were fooled, full content hashes remain.
    proof['snapshot']=snapshot(b)
    with pytest.raises(InputChanged,match='content changed'):verify_input_content(b,proof)


def test_receipt_is_bound_to_copy_and_schema_and_not_accepted_as_json(service,tmp_path):
    from dataclasses import replace
    cfg=service;b=bag(cfg,2);route=cfg['routes'][2]
    proof=check_input(b,route,snapshot(b));dest=tmp_path/'copy'
    receipt=stage_input(b,dest,proof)
    assert receipt.reuse(dest,route['conversion'])[0]['source']==str(dest)
    with pytest.raises(ValueError,match='path/schema'):receipt.reuse(b,route['conversion'])
    with pytest.raises(ValueError,match='path/schema'):replace(receipt,context='wrong').reuse(dest,route['conversion'])
    f=dest/'metadata.yaml';f.write_text(f.read_text()+'\n')
    with pytest.raises(ValueError,match='working copy changed'):receipt.reuse(dest,route['conversion'])


def test_corrupt_working_copy_after_conversion_blocks_publication(service,monkeypatch):
    import convest.ingest.worker as worker
    cfg=service;bag(cfg,2);real=worker.run
    def corrupt(*args,**kw):
        result=real(*args,**kw)
        receipt=kw['validated_inputs']['episode1']
        p=next(receipt.root.glob('*.db3'));p.write_bytes(p.read_bytes()+b'corruption')
        return result
    monkeypatch.setattr(worker,'run',corrupt)
    with worker_context(cfg) as q:
        # Post-job receipt check must also cover the gap after run returns.
        result=ready_scan(cfg,q)
        assert result['counts']=={'retry':1}
        assert Publisher(cfg['routes'][2],cfg['fingerprint']).current() is None


def test_drain_finishes_current_publication_and_cleanup_then_restart_resumes(service,monkeypatch):
    import convest.ingest.worker as worker
    cfg=service;bag(cfg,2,1);bag(cfg,2,2);real=worker.run;called=[]
    def drain(*args,**kw):
        called.append(kw['episodes'][0]);request_maintenance(cfg,True)
        return real(*args,**kw)
    monkeypatch.setattr(worker,'run',drain)
    with worker_context(cfg) as q:
        report=ready_scan(cfg,q)
        assert report['maintenance']=='drained' and report['counts']=={'published':1,'waiting_stable':1}
        assert len(called)==1
        assert not [p for p in Path(cfg['work_root']).iterdir() if p.is_dir()]
    with worker_context(cfg) as q:
        assert scan(cfg,q)['maintenance']=='drained' and len(called)==1
        request_maintenance(cfg,False)
        monkeypatch.setattr(worker,'run',real)
        assert scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)['counts']=={'published':2}
        assert verify(Publisher(cfg['routes'][2],cfg['fingerprint']).current())['episodes']==2


def test_drain_during_readiness_does_not_start_processing(service,monkeypatch):
    import convest.ingest.worker as worker
    cfg=service;bag(cfg,2);real=worker.check_input
    def drain(*args,**kw):
        proof=real(*args,**kw);request_maintenance(cfg,True);return proof
    monkeypatch.setattr(worker,'check_input',drain)
    with worker_context(cfg) as q:
        result=ready_scan(cfg,q)
        assert result['maintenance']=='drained' and result['counts']=={'waiting_stable':1}
        assert Publisher(cfg['routes'][2],cfg['fingerprint']).current() is None


def test_progress_distinguishes_precheck_final_hash_and_publication(service,monkeypatch):
    import convest.ingest.worker as worker
    cfg=service;bag(cfg,2);real=worker.progress_reporter;phases=[]
    def reporter(*args,**kw):
        report=real(*args,**kw)
        def capture(phase,**detail):phases.append(phase);return report(phase,**detail)
        return capture
    monkeypatch.setattr(worker,'progress_reporter',reporter)
    with worker_context(cfg) as q:assert ready_scan(cfg,q)['counts']=={'published':1}
    assert phases.index('working_copy_readiness_reused')<phases.index('original_final_hash')<phases.index('publishing')<phases.index('cleanup')
    assert json.loads((Path(cfg['state_root'])/'progress.json').read_text())['phase']=='idle'


def test_json_receipt_cannot_bypass_standalone_precheck(service):
    from convest.jobs import run
    cfg=service;source=bag(cfg,2)
    out=Path(cfg['work_root'])/'invalid_receipt'
    assert run({**cfg['routes'][2]['conversion'],'source_root':str(source.parent)},out,
               episodes=[source.name],validated_inputs={source.name:{'status':'passed'}})==2
    summary=json.loads((out/'summary.json').read_text())
    assert summary['failed']==1 and 'Invalid readiness receipt' in summary['jobs'][0]['error']
    assert not (out/'dataset').exists()


def test_maintenance_cli_request_and_resume_do_not_delete_queue(service,monkeypatch,capsys):
    from convest.ingest import cli
    cfg=service;bag(cfg,2)
    monkeypatch.setattr(cli,'load_service_config',lambda path:cfg)
    with worker_context(cfg) as q:
        scan(cfg,q)
        assert cli.main(['drain','--config','unused.yaml'])==0
        capsys.readouterr()
        assert scan(cfg,q)['maintenance']=='drained'
        assert q.get('wuji-A','episode1')['state']=='waiting_stable'
        assert cli.main(['status','--config','unused.yaml'])==0
        status=json.loads(capsys.readouterr().out)
        assert status['maintenance_requested'] and status['last_scan']['maintenance']=='drained'
        assert cli.main(['resume','--config','unused.yaml'])==0
        capsys.readouterr()
        assert scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)['counts']=={'published':1}


def test_explicit_robot_priority_moves_existing_queue_without_skipping_checks(service,monkeypatch):
    import importlib.util
    spec=importlib.util.spec_from_file_location('prioritize_ingest',WORKSPACE/'scripts/prioritize_ingest.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    cfg=service;cfg['max_jobs_per_scan']=1;bag(cfg,0);bag(cfg,2)
    import convest.ingest.worker as worker
    calls=[];real=worker.check_input
    def check(b,*args,**kw):calls.append(str(b));return real(b,*args,**kw)
    monkeypatch.setattr(worker,'check_input',check)
    with worker_context(cfg) as q:
        t=time.time_ns();scan(cfg,q,now_ns=t)
        # Simulate Wuji having higher priority due to a later successor.
        q.prioritize('wuji-A','episode1',time.time_ns())
        before=q.get('sharpa-A','episode1')
        dry=module.prioritize(cfg,'sharpa');assert not dry['applied'] and dry['queued_count']==1
        result=module.prioritize(cfg,'sharpa',True);assert result['queued_count']==1
        assert q.get('sharpa-A','episode1')==before
        report=scan(cfg,q,now_ns=t+2_000_000_000)
        assert '/sharpa/' in calls[0] and report['counts']=={'published':1,'waiting_stable':1}
        assert q.get('wuji-A','episode1')['state']=='waiting_stable'


def test_empty_output_directories_can_initialize_without_touching_existing_data(service):
    cfg=service;r=cfg['routes'][2];Path(r['output']).mkdir(parents=True);bag(cfg,2)
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'published':1}
        assert (Path(r['output'])/'1_1_data_lerobot').is_symlink()


def test_nonempty_unowned_output_is_never_overwritten(service):
    cfg=service;r=cfg['routes'][2];out=Path(r['output']);out.mkdir(parents=True);(out/'keep.txt').write_text('existing')
    bag(cfg,2)
    with worker_context(cfg) as q:
        result=ready_scan(cfg,q);assert 'wuji-A' in result['route_errors']
        assert (out/'keep.txt').read_text()=='existing' and not out.is_symlink()


def test_publication_lock_is_shared_even_with_separate_queue_state(service,tmp_path):
    import fcntl
    cfg=service;pub=Publisher(cfg['routes'][2],cfg['fingerprint']);pub._init_store()
    with (pub.store/'publish.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):pub.publish(tmp_path,{},'episode1',tmp_path/'audit')
    assert pub.current() is None


@pytest.mark.parametrize('arrival',['copy','rename'])
def test_configured_input_discovers_last_and_deduplicates_without_upload_protocol(service,arrival):
    cfg=service;r=cfg['routes'][2];formal=Path(r['source'])
    cfg['initial_history_by_route']['wuji-A']={'mode':'none','episodes':[]}
    staging=Path(cfg['data_root'])/'.uploading/task42'
    make_conversion_bag(staging,'wuji',frames=18)
    make_conversion_bag(formal/'.uploading/task43/episode43','wuji',frames=18)
    (formal/'episode99').symlink_to(staging,target_is_directory=True)
    originals={p.name:checksum(p) for p in staging.iterdir()};t=time.time_ns()
    with worker_context(cfg) as q:
        assert scan(cfg,q,now_ns=t)['counts']=={}  # Includes no temporary-upload jobs.
        # Arrival mechanism is irrelevant to the receiver; both use the same checks.
        if arrival=='copy':shutil.copytree(staging,formal/'episode42')
        else:staging.rename(formal/'episode42')
        assert scan(cfg,q,now_ns=t+2_000_000_000)['counts']=={'waiting_stable':1}
        assert scan(cfg,q,now_ns=t+4_000_000_000)['counts']=={'published':1}
        pub=Publisher(r,cfg['fingerprint']);fixed=pub.current()
        assert verify(fixed)['episodes']==1
        assert scan(cfg,q,now_ns=t+6_000_000_000)['counts']=={'published':1}
    with worker_context(cfg) as q:
        assert scan(cfg,q)['counts']=={'published':1} and pub.current()==fixed
        # Unexpected later source changes must not silently alter an existing dataset.
        p=formal/'episode42/metadata.yaml';p.write_text(p.read_text()+'\n# unexpected write\n')
        assert scan(cfg,q)['counts']=={'conflict':1} and pub.current()==fixed
    assert {p.name:checksum(p) for p in (formal/'episode42').iterdir() if p.name!='metadata.yaml'}=={
        name:sha for name,sha in originals.items() if name!='metadata.yaml'}


def test_initial_history_include_persists_and_restart_discovers_missed_arrivals(service):
    cfg=service;cfg['initial_history_by_route']['wuji-A']={'mode':'include','episodes':['episode1','episode3']}
    bag(cfg,2,1);b2=bag(cfg,2,2);t=time.time_ns()
    with worker_context(cfg) as q:
        assert scan(cfg,q,now_ns=t)['counts']=={'waiting_stable':1}
        assert q.get('wuji-A','episode2') is None
    # Arrivals while stopped are new, whether or not in the initial history list.
    bag(cfg,2,3);bag(cfg,2,4)
    with worker_context(cfg) as q:
        scan(cfg,q,now_ns=t+2_000_000_000)
        result=scan(cfg,q,now_ns=t+4_000_000_000)
        assert result['counts']=={'published':3}
        assert q.get('wuji-A','episode2') is None
        scope=next(r for r in result['initial_history'] if r['route']=='wuji-A')
        assert scope['skipped']==['episode2']
        p=b2/'metadata.yaml';p.write_text(p.read_text()+'\n# ignored history stays ignored\n')
        assert scan(cfg,q)['counts']=={'published':3}
        assert verify(Publisher(cfg['routes'][2],cfg['fingerprint']).current())['episodes']==3


def test_initial_history_none_skips_existing_but_resumes_legacy_queue(service):
    cfg=service;cfg['initial_history_by_route']['wuji-A']={'mode':'none','episodes':[]}
    bag(cfg,2,1);bag(cfg,2,2);t=time.time_ns()
    with worker_context(cfg) as q:
        # Model an existing 27811d8 queue: no startup-scope row yet.
        q.put('wuji-A','episode1',None,'waiting_stable')
        assert scan(cfg,q,now_ns=t)['counts']=={'waiting_stable':1}
        assert q.get('wuji-A','episode2') is None
    bag(cfg,2,3)
    with worker_context(cfg) as q:
        scan(cfg,q,now_ns=t+2_000_000_000)
        assert scan(cfg,q,now_ns=t+4_000_000_000)['counts']=={'published':2}
        assert q.get('wuji-A','episode2') is None


def test_initial_history_waits_for_missing_route_and_rejects_policy_change(service):
    cfg=service;r=cfg['routes'][2];source=Path(r['source']);source.rmdir()
    cfg['initial_history_by_route']['wuji-A']={'mode':'none','episodes':[]}
    with worker_context(cfg) as q:
        assert 'wuji-A' in scan(cfg,q)['route_errors']
        assert not any(r['route']=='wuji-A' for r in q.history_rows())
        source.mkdir();bag(cfg,2,1)
        result=scan(cfg,q);assert result['counts']=={}  # First successful scan defines the baseline.
        cfg['initial_history_by_route']['wuji-A']={'mode':'all','episodes':[]}
        assert 'policy changed' in scan(cfg,q)['route_errors']['wuji-A']


def test_initial_history_is_explicit_config_and_does_not_change_conversion_fingerprint(service):
    cfg=service;path=Path(cfg['state_root']).parent/'config.yaml';raw=yaml.safe_load(path.read_text())
    raw.pop('initial_history');path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError,match='Explicit initial_history'):load_service_config(path)
    raw['initial_history']={'mode':'none'}
    raw['routes'][2]['initial_history']={'mode':'include','episodes':['episode7']}
    path.write_text(yaml.safe_dump(raw));loaded=load_service_config(path)
    assert loaded['fingerprint']==cfg['fingerprint']  # Existing ownership/generations stay compatible.
    assert loaded['initial_history_by_route']['wuji-A']=={'mode':'include','episodes':['episode7']}
    raw['initial_history']={'mode':'include','episodes':['../episode1']};path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError,match='exact episode'):load_service_config(path)
