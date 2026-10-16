"""Independent oracle, portable verification, failure isolation and public preflight."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import numpy as np
import pytest
pytest.importorskip('pyarrow');pytest.importorskip('av')
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from convest.config import WORKSPACE, load_config, atomic_json
from convest.sources import robot_rosbag2 as source
from convest.recipes import multimodal as recipe
from convest.formats import multimodal as writer
from convest.verify import verify
from convest.preflight import inspect_sources
from conversion_helpers import make_conversion_bag


@pytest.fixture
def build_dataset(tmp_path):
    def build(robot='wuji',count=1):
        cfg=load_config(WORKSPACE/f'configs/{robot}.yaml')
        inputs=tmp_path/f'inputs_{robot}';dataset=tmp_path/f'dataset_{robot}'
        dataset.mkdir();records=[];total=0
        cfg.update(source_root=str(inputs),output_root=str(dataset))
        contract=yaml.safe_load(Path(cfg['contract']).read_text())
        for index in range(count):
            raw=make_conversion_bag(inputs/f'episode{index+1}',robot)
            item=source.discover(raw)[0]
            with source.Bag(raw,cfg['schema_dir']) as bag:
                streams,aligned=recipe.prepare(source,bag,item,cfg,contract)
                spec=recipe.output_segments(item,aligned,cfg)[0]
                ep=writer.write_segment(dataset,bag,streams,aligned,spec['bounds'],index,total,cfg,contract,spec)
            records.append({'source':str(raw),'source_snapshot':source.snapshot(raw),'alignment':aligned.report,'episodes':[ep]})
            atomic_json(dataset/f'conversion/records/{index:06d}.json',records[-1])
            total+=ep['length']
        writer.write_metadata(dataset,records,cfg)
        atomic_json(dataset/'conversion/manifest.json',{'config':cfg})
        return dataset,cfg
    return build


def corrupt_index(root,episode=0):
    p=root/f'data/chunk-000/episode_{episode:06d}.parquet'
    t=pq.read_table(p);i=t.schema.get_field_index('index')
    t=t.set_column(i,'index',pa.array(t['index'].to_numpy()+10))
    pq.write_table(t,p)


def test_portable_offline_verification_does_not_touch_source_or_contract(build_dataset,tmp_path):
    root,cfg=build_dataset('sharpa')
    moved=tmp_path/'relocated';shutil.copytree(root,moved)
    shutil.rmtree(cfg['source_root'])
    manifest=json.loads((moved/'conversion/manifest.json').read_text())
    manifest['config']['contract']='/missing/contract.yaml';manifest['config']['schema_dir']='/missing/schema'
    atomic_json(moved/'conversion/manifest.json',manifest)
    assert verify(moved)['status']=='passed'
    assert verify(moved,compare_source=True,raise_on_error=False)['status']=='failed'


def test_bad_episode_does_not_hide_good_episode_and_checks_survive_optimization(build_dataset):
    root,_=build_dataset(count=2);corrupt_index(root)
    result=verify(root,raise_on_error=False)
    assert [r['status'] for r in result['results']]==['failed','passed']
    assert result['failed_episodes']==1
    command=[sys.executable,'-O','-c','from convest.verify import verify; import sys; verify(sys.argv[1])',str(root)]
    p=subprocess.run(command,text=True,capture_output=True)
    assert p.returncode!=0 and 'verification failed' in p.stderr
    q=json.loads((root/'conversion/quality_report.json').read_text())
    assert len(q['verification_errors'])==1
    assert q['sources'][1]['episodes'][0]['verification']=='passed'


def test_alignment_bug_cannot_pass_by_reusing_conversion_function(build_dataset,monkeypatch):
    original=recipe.prepare
    def faulty(*args):
        streams,a=original(*args);a.action[3,0]+=2
        return streams,a
    monkeypatch.setattr(recipe,'prepare',faulty)
    root,_=build_dataset()
    # Its writer stats and structural metadata agree with the intentionally wrong values.
    assert verify(root)['status']=='passed'
    result=verify(root,compare_source=True,raise_on_error=False)
    assert result['status']=='failed'
    assert result['results'][0]['status']=='failed'


def test_same_element_count_wrong_nested_shape_is_rejected(build_dataset):
    root,_=build_dataset();p=root/'data/chunk-000/episode_000000.parquet'
    table=pq.read_table(p);key='observation.depths.cam0';data=np.array(table[key].to_pylist(),np.uint16).reshape(-1,4,16)
    column=writer.nested_array(data);table=table.set_column(table.schema.get_field_index(key),key,column)
    pq.write_table(table,p)
    report=verify(root,raise_on_error=False)
    assert report['status']=='failed' and 'shape' in report['results'][0]['error']


def test_preflight_lists_missing_metadata_without_creating_dataset(tmp_path):
    cfg=load_config(WORKSPACE/'configs/wuji.yaml');root=tmp_path/'inputs'
    raw=make_conversion_bag(root/'episode1');(root/'episode2').mkdir()
    cfg['source_root']=str(root)
    before=source.snapshot(raw);report=inspect_sources(cfg)
    assert report['ready']==report['not_ready']==1
    assert report['sources'][0]['streams']['cam0']['source_loss_rate'] is None
    assert source.snapshot(raw)==before
    selection=tmp_path/'list.txt';selection.write_text('1\n')
    selected=inspect_sources(cfg,episode_list=selection)
    assert selected['status']=='passed' and selected['selected']==1
    assert not (tmp_path/'dataset').exists()


def test_quality_repeated_sampling_has_explicit_denominator(build_dataset):
    root,_=build_dataset();verify(root)
    q=json.loads((root/'conversion/quality_report.json').read_text())
    x=q['sources'][0]['episodes'][0]['repeated_sample_timestamps']['cam0']
    assert x=={'equal_adjacent_pairs':0,'comparable_adjacent_pairs':11,'ratio':0.0}
    assert q['sources'][0]['source_loss_rate'] is None


def test_job_verification_failure_keeps_other_committed_jobs_completed(tmp_path,monkeypatch):
    import convest.jobs as jobs
    import convest.pipeline as pipeline
    import convest.verify as verifier
    inputs=tmp_path/'inputs'
    make_conversion_bag(inputs/'episode1');make_conversion_bag(inputs/'episode2')
    cfg=load_config(WORKSPACE/'configs/wuji.yaml');cfg['source_root']=str(inputs)
    monkeypatch.setattr(jobs,'WORKSPACE',tmp_path);monkeypatch.setattr(pipeline,'WORKSPACE',tmp_path)
    def serial(items,config,contract,staging,workers):
        for i,item in enumerate(items):
            stage=staging/str(i)
            yield item,stage,pipeline.prepare_bag(item,config,contract,stage),None
    monkeypatch.setattr(pipeline,'prepared_in_order',serial)
    real=verifier.verify
    injected=False
    def check(root,**kwargs):
        nonlocal injected
        if not injected:corrupt_index(root);injected=True
        return real(root,**kwargs)
    monkeypatch.setattr(verifier,'verify',check)
    out=tmp_path/'outputs/job'
    assert jobs.run(cfg,out)==2
    first=json.loads((out/'jobs/episode1.json').read_text());second=json.loads((out/'jobs/episode2.json').read_text())
    assert first['state']=='verification_failed' and first['conversion_committed']
    assert second['state']=='completed' and second['conversion_committed']
    assert jobs.run(cfg,out,resume=True)==2
    assert json.loads((out/'jobs/episode2.json').read_text())['state']=='completed'


def test_preflight_cli_report_guards_and_list_selection(tmp_path,monkeypatch):
    import convest.cli as cli
    cfg=load_config(WORKSPACE/'configs/wuji.yaml')
    inputs=tmp_path/'outputs/inputs';make_conversion_bag(inputs/'episode1')
    cfg['source_root']=str(inputs)
    monkeypatch.setattr(cli,'WORKSPACE',tmp_path);monkeypatch.setattr(cli,'load_config',lambda path:dict(cfg))
    selection=tmp_path/'list.txt';selection.write_text('1\n')
    forbidden=inputs/'episode1/report.json'
    assert cli.main(['inspect','--config','unused','--report',str(forbidden)])==2
    assert not forbidden.exists()
    report=tmp_path/'outputs/check.json'
    args=['check-list',str(selection),'--config','unused','--report',str(report)]
    assert cli.main(args)==0
    assert json.loads(report.read_text())['selected']==1
    before=report.read_bytes()
    assert cli.main(args)==2 and report.read_bytes()==before


def test_epoch_nanosecond_statistics_do_not_accumulate_float_cancellation():
    from convest.verify import OffsetScalarStats
    data=1_790_000_000_000_000_000+np.arange(4000,dtype=np.int64)*33_333_333
    stats=OffsetScalarStats()
    for i in range(0,len(data),8):stats.add(data[i:i+8])
    np.testing.assert_allclose(stats.result()['std'],[data.astype(np.float64).std()],rtol=1e-7)
    assert stats.result()['count']==[4000]


def test_ready_checks_actual_depth_encoding_before_trimming(tmp_path):
    import sqlite3
    from convest.jobs import check_ready
    cfg=load_config(WORKSPACE/'configs/wuji.yaml')
    raw=make_conversion_bag(tmp_path/'input')
    with source.Bag(raw,cfg['schema_dir']) as bag:
        db,tid,_=bag.topics['/cam0/depth/image_raw'][0]
        row=db.execute('SELECT id,data FROM messages WHERE topic_id=? ORDER BY timestamp LIMIT 1',(tid,)).fetchone()
        message=bag.store.deserialize_cdr(row[1],'sensor_msgs/msg/Image');message.encoding='32FC1'
        changed=bytes(bag.store.serialize_cdr(message,'sensor_msgs/msg/Image'))
    with sqlite3.connect(raw/'data.db3') as db:db.execute('UPDATE messages SET data=? WHERE id=?',(changed,row[0]))
    contract=yaml.safe_load(Path(cfg['contract']).read_text())
    with pytest.raises(ValueError,match='16uc1'):check_ready(raw,contract,cfg['schema_dir'])


def test_whole_job_quality_report_preserves_trimming_denominators(tmp_path):
    from convest.reporting import job_quality_report
    raw=make_conversion_bag(tmp_path/'input/episode1')
    root=tmp_path/'job';trim=root/'cleaned/episode1/trim_report.json'
    atomic_json(trim,{'deleted_seconds':0.1,'deleted':3,'kept':100,'mapping':[{}]})
    result=job_quality_report(root,[{'source':str(raw),'episode':'episode1','state':'completed','conversion_committed':True}])
    item=result['sources'][0]
    assert item['deleted_intervals']==1 and item['deleted_seconds']==0.1
    assert item['deleted_duration_ratio']==0.1/item['input_duration_s']
    assert item['deleted_message_ratio']==3/item['input_messages']
    assert result['source_loss_rate'] is None
