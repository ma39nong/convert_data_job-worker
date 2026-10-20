"""Daily route batches and destructive retention require verified remote backup."""
from pathlib import Path
import csv
import json
import shutil
import time
import pytest
from convest.capture.worker import context,scan
from convest.capture.transfer import upload_once
from convest.capture import daily,retention
from convest.ingest.batches import BatchPublisher
from convest.jobs import checksum
from convest.verify import verify
from test_capture import capture,ready
from test_ingest import service,bag


@pytest.fixture
def daily_cfg(capture):
    cfg=capture;base=Path(cfg['work_root'])
    cfg.update(delivery_mode='daily',batch_hours=24,delete_raw_after_upload=True,cleanup_intermediates=True,
               local_batch_root=str(base/'batches'),work_root=str(base/'work'))
    for r in cfg['routes']:
        r.update(output=str(Path(cfg['local_batch_root'])/r['robot']/r['group']),batch_hours=24)
    return cfg


def due(cfg):
    return max(b['deadline_ns'] for r in cfg['routes'] for b in BatchPublisher(r,cfg['fingerprint']).status())+1


def test_four_routes_merge_two_sources_delete_backed_up_raw_and_upload_only_after_24h(daily_cfg):
    cfg=daily_cfg;originals={}
    for i in range(4):
        for number in (1,2):
            p=bag(cfg,i,number);originals[str(p)]={f.name:checksum(f) for f in p.iterdir()}
    with context(cfg) as q:
        ready(cfg,q)
        assert all(j['state']=='converted' for j in q.rows())
        assert q.db.execute('select count(*) from uploads').fetchone()[0]==8
        clocks={}
        for r in cfg['routes']:
            b=BatchPublisher(r,cfg['fingerprint']).status()[0]
            assert b['state']=='active' and b['source_count']==2 and b['episodes']==2
            assert b['deadline_ns']-b['opened_ns']==24*3600*10**9
            clocks[r['id']]=b['deadline_ns']
        assert not list(Path(cfg['work_root']).glob('**/cleaned'))
        assert not list(Path(cfg['work_root']).glob('**/dataset'))
        for _ in range(8):assert upload_once(cfg,q)
        assert not upload_once(cfg,q)  # open batches are not early uploads
        for r in cfg['routes']:
            for n in (1,2):
                p=Path(r['source'])/f'episode{n}';assert not p.exists()
                remote=Path(cfg['remote_raw_root'])/r['robot']/r['group']/p.name
                assert {f.name:checksum(f) for f in remote.iterdir()}==originals[str(p)]
        rows=list(csv.DictReader((Path(cfg['state_root'])/'recordings.csv').open(encoding='utf-8-sig')))
        assert len(rows)==8 and all(r['deletion_state']=='deleted' for r in rows)
        boundary=due(cfg)
    with context(cfg) as q:
        scan(cfg,q,now_ns=boundary)  # no source bags remain; idle still seals
        for r in cfg['routes']:
            b=BatchPublisher(r,cfg['fingerprint']).status()[0]
            assert b['state']=='sealed' and b['deadline_ns']==clocks[r['id']]
            assert b['verification']['full_video_decode'] and b['verification']['artifact_sha256']=='passed'
        assert q.db.execute('select count(*) from uploads').fetchone()[0]==12
        for _ in range(4):assert upload_once(cfg,q)
        assert not upload_once(cfg,q)
        for r in cfg['routes']:
            target=Path(cfg['remote_dataset_root'])/r['robot']/r['group']/'1_2_data_lerobot'
            assert verify(target,full_video=True)['episodes']==2
        assert q.db.execute("select count(*) from capture_batches where state='uploaded'").fetchone()[0]==4
        ledger=json.loads((Path(cfg['state_root'])/'recordings.json').read_text())
        assert all(r['batch_state']=='uploaded' for r in ledger['recordings'])


def test_raw_target_corruption_never_deletes_original(daily_cfg):
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        target=Path(cfg['remote_raw_root'])/'wuji/A/episode1';target.mkdir(parents=True)
        (target/'foreign').write_text('preserve')
        upload_once(cfg,q)
        assert p.exists() and (target/'foreign').read_text()=='preserve'
        assert q.db.execute('select state from uploads').fetchone()[0]=='conflict'
        assert q.db.execute('select raw_deleted_ns from recordings').fetchone()[0] is None


def test_interrupted_partial_deletion_resumes_without_reconversion(daily_cfg,monkeypatch):
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        real=retention.shutil.rmtree
        def interrupt(path,*args,**kw):
            if '.capture-cleanup' in Path(path).parts:
                next(f for f in Path(path).iterdir() if f.is_file()).unlink()
                raise OSError('simulated interruption during deletion')
            return real(path,*args,**kw)
        interrupt.avoids_symlink_attacks=True
        monkeypatch.setattr(retention.shutil,'rmtree',interrupt)
        upload_once(cfg,q)
        row=q.db.execute('select * from recordings').fetchone()
        assert row['deletion_state']=='deleting' and Path(row['quarantine']).exists() and not p.exists()
        assert q.db.execute('select raw_done from uploads').fetchone()[0]==1
        monkeypatch.setattr(retention.shutil,'rmtree',real)
        q.db.execute('update uploads set next_ns=0');q.db.commit()
        upload_once(cfg,q)
        assert not Path(row['quarantine']).exists()
        assert q.db.execute('select deletion_state from recordings').fetchone()[0]=='deleted'
        assert q.get('wuji-A','episode1')['attempts']==1
        assert q.db.execute('select state from uploads').fetchone()[0]=='completed'


def test_remote_backup_rechecked_before_resuming_deletion(daily_cfg,monkeypatch):
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        real=daily.delete_original
        monkeypatch.setattr(daily,'delete_original',lambda *a: (_ for _ in ()).throw(OSError('stop before delete')))
        upload_once(cfg,q)
        assert p.exists() and q.db.execute('select raw_done from uploads').fetchone()[0]==1
        target=Path(cfg['remote_raw_root'])/'wuji/A/episode1/collection_state.json'
        target.write_bytes(b'x'*target.stat().st_size)
        monkeypatch.setattr(daily,'delete_original',real)
        q.db.execute('update uploads set next_ns=0');q.db.commit()
        upload_once(cfg,q)
        assert p.exists() and q.db.execute('select state from uploads').fetchone()[0]=='conflict'
        assert q.db.execute('select raw_deleted_ns from recordings').fetchone()[0] is None


def test_changed_local_original_is_never_deleted(daily_cfg):
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        f=p/'collection_state.json';f.write_text(f.read_text()+'\n')
        upload_once(cfg,q)
        assert p.exists() and q.db.execute('select state from uploads').fetchone()[0]=='conflict'


def test_foreign_quarantine_preserved(daily_cfg):
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        task=q.db.execute('select id from uploads').fetchone()[0]
        target=p.parent/'.capture-cleanup'/task;target.mkdir(parents=True)
        (target/'foreign').write_text('keep')
        upload_once(cfg,q)
        assert p.exists() and (target/'foreign').read_text()=='keep'
        assert q.db.execute('select raw_deleted_ns from recordings').fetchone()[0] is None


def test_reused_deleted_episode_name_is_preserved_and_conflicted(daily_cfg):
    cfg=daily_cfg;bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q);upload_once(cfg,q)
        new=bag(cfg,2,1)
        scan(cfg,q)
        assert new.exists() and q.get('wuji-A','episode1')['state']=='conflict'


def test_due_batch_not_starved_and_next_source_opens_new_batch(daily_cfg):
    cfg=daily_cfg;bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q);upload_once(cfg,q)
        first=BatchPublisher(cfg['routes'][2],cfg['fingerprint']).status()[0]
        daily.maintain(cfg,q,first['deadline_ns']+1)
        bag(cfg,2,2);ready(cfg,q)
        batches=BatchPublisher(cfg['routes'][2],cfg['fingerprint']).status()
        assert len(batches)==2 and batches[0]['state']=='sealed' and batches[1]['state']=='active'
        upload_once(cfg,q)
        assert q.db.execute("select state from capture_batches where batch_id='batch-000001'").fetchone()[0]=='uploaded'
        assert (Path(cfg['routes'][2]['source'])/'episode2').exists()
        upload_once(cfg,q)
        assert not (Path(cfg['routes'][2]['source'])/'episode2').exists()


def test_deletion_requires_durable_acknowledgement(daily_cfg):
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        row=q.db.execute('select * from uploads').fetchone();payload=json.loads(row['payload'])
        with pytest.raises(ValueError,match='durable verified'):
            retention.delete_original(cfg,q,payload,row['id'])
        assert p.exists() and not (p.parent/'.capture-cleanup').exists()


def test_change_during_quarantine_move_is_preserved(daily_cfg,monkeypatch):
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        real=retention.os.rename
        def tamper(src,dst,*a,**kw):
            result=real(src,dst,*a,**kw)
            if '.capture-cleanup' in Path(dst).parts:
                f=Path(dst)/'collection_state.json';f.write_text(f.read_text()+'\n')
            return result
        monkeypatch.setattr(retention.os,'rename',tamper)
        upload_once(cfg,q)
        assert p.exists() and (p/'collection_state.json').exists()
        assert q.db.execute('select raw_deleted_ns from recordings').fetchone()[0] is None
        assert q.db.execute('select state from uploads').fetchone()[0]=='conflict'


def test_recovery_after_local_batch_commit_never_reconverts(daily_cfg,monkeypatch):
    import convest.capture.worker as worker
    cfg=daily_cfg;bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        for f in Path(cfg['work_root']).rglob('local_complete.json'):f.unlink()
        q.db.execute('delete from uploads');q.db.execute('delete from recordings');q.db.commit()
        old=q.get('wuji-A','episode1');q.put('wuji-A','episode1',old['identity'],'processing')
        monkeypatch.setattr(worker,'run',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must not reconvert')))
        scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)
        assert q.get('wuji-A','episode1')['state']=='converted'
        assert q.db.execute('select count(*) from uploads').fetchone()[0]==1


def test_seal_failure_does_not_upload_invalid_batch(daily_cfg):
    cfg=daily_cfg;bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q);upload_once(cfg,q)
        b=BatchPublisher(cfg['routes'][2],cfg['fingerprint']).status()[0]
        artifact=next(Path(b['generation']).glob('data/**/*.parquet'))
        artifact.chmod(0o644)
        errors=daily.maintain(cfg,q,b['deadline_ns']+1)
        assert 'wuji-A' in errors
        assert q.db.execute('select count(*) from uploads').fetchone()[0]==1
        assert not list(Path(cfg['remote_dataset_root']).rglob('*.parquet'))


def test_acknowledged_batch_state_survives_stale_maintenance(daily_cfg):
    cfg=daily_cfg;bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        q.db.execute("update capture_batches set state='sealed',uploaded_ns=123,remote_path='remote'");q.db.commit()
        daily.maintain(cfg,q)
        assert q.db.execute('select state from capture_batches').fetchone()[0]=='uploaded'


def test_low_space_pauses_without_exhausting_retries(daily_cfg,monkeypatch):
    import convest.capture.worker as worker
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        real=worker.shutil.disk_usage
        sample=real(cfg['work_root'])
        monkeypatch.setattr(worker.shutil,'disk_usage',lambda _:type(sample)(sample.total,sample.used,0))
        ready(cfg,q)
        assert q.get('wuji-A','episode1')['state']=='waiting_space'
        assert q.get('wuji-A','episode1')['attempts']==0
        assert p.exists() and q.db.execute('select count(*) from uploads').fetchone()[0]==0
        monkeypatch.setattr(worker.shutil,'disk_usage',real)
        q.db.execute('update jobs set next_ns=0');q.db.commit()
        scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)
        assert q.get('wuji-A','episode1')['state']=='converted'


def test_explicit_retention_option_keeps_raw(daily_cfg):
    cfg=daily_cfg;cfg['delete_raw_after_upload']=False;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q);upload_once(cfg,q)
        assert p.exists() and q.db.execute('select state from uploads').fetchone()[0]=='completed'
        assert q.db.execute('select deletion_state from recordings').fetchone()[0]=='retained'


def test_remote_ledger_backup_retries_without_repeating_raw_upload(daily_cfg,monkeypatch):
    from convest.capture import transfer,ledger
    cfg=daily_cfg;bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        real=transfer.check_mount
        monkeypatch.setattr(transfer,'check_mount',lambda _: (_ for _ in ()).throw(OSError('mirror disconnected')))
        upload_once(cfg,q)
        assert q.db.execute('select state from uploads').fetchone()[0]=='completed'
        assert q.db.execute('select deletion_state from recordings').fetchone()[0]=='deleted'
        assert not (Path(cfg['state_root'])/'ledger_mirror.json').exists()
        monkeypatch.setattr(transfer,'check_mount',real)
        assert not upload_once(cfg,q)
        backup=json.loads((Path(cfg['state_root'])/'ledger_mirror.json').read_text())
        saved=json.loads((Path(backup['remote'])/'recordings.json').read_text())
        assert saved['recordings'][0]['episode']=='episode1'
        assert saved['recordings'][0]['deletion_state']=='deleted'
        assert saved['recordings'][0]['raw_deleted_at']


def test_old_attempt_audits_never_overwrite_committed_source_audit(daily_cfg):
    cfg=daily_cfg;p=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        row=q.get('wuji-A','episode1');proof=json.loads(row['proof'])
        root=Path(cfg['work_root'])/'wuji-A/episode1'/row['identity']
        audit=Path(cfg['state_root'])/'audits/wuji-A/episode1'/row['identity']
        before=(audit/'summary.json').read_bytes()
        old=root/'attempt-0000';old.mkdir()
        (old/'manifest.json').write_text(json.dumps({'owner':'convert-data-job-worker'}))
        (old/'summary.json').write_text(json.dumps({'old_failed_attempt':True}))
        (old/'cleaned').mkdir();(old/'cleaned/raw-copy').write_bytes(b'owned')
        daily.cleanup_intermediates(cfg,cfg['routes'][2],p,proof,root)
        assert (audit/'summary.json').read_bytes()==before
        assert json.loads((audit/'work-attempts/attempt-0000/summary.json').read_text())['old_failed_attempt']
        assert not (old/'cleaned').exists()
