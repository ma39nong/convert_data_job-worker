import importlib.util
import json
from pathlib import Path
import shutil
import pytest
from convest.config import WORKSPACE, atomic_json
from convest.jobs import checksum
from conversion_helpers import make_conversion_bag


def load_publisher():
    spec = importlib.util.spec_from_file_location('production_publisher', WORKSPACE/'scripts/production_sharpa_1016.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_publish_and_resume(tmp_path, monkeypatch):
    pub = load_publisher()
    # jobs.run intentionally permits only our workspace outputs.
    control = WORKSPACE/'outputs/test_production_publisher'
    if control.exists():
        shutil.rmtree(control)
    control.mkdir(parents=True)
    source = tmp_path/'original'; reference = tmp_path/'reference'; reference.mkdir()
    target = tmp_path/'published';target.mkdir()
    names = ['episode1','episode2']
    try:
        for name in names:
            make_conversion_bag(source/name,'sharpa',frames=30)
        original = {str(p):checksum(p) for p in source.rglob('*') if p.is_file()}
        inventory = {'source_root':str(source), 'reference_root':str(reference), 'target_root':str(target),
            'episodes':names, 'reference_paths':{n:[str(reference/n)] for n in names},
            'original_sizes':{n:sum(p.stat().st_size for p in (source/n).iterdir()) for n in names}}
        atomic_json(control/'inventory.json',inventory)
        (control/'episodes.txt').write_text('\n'.join(names)+'\n')
        monkeypatch.setattr(pub,'CONTROL',control)
        monkeypatch.setattr(pub.os.path,'ismount',lambda p: True)
        assert pub.execute()==0
        result=json.loads((target/'production/status.json').read_text())
        assert result['published']==2 and not result['local_cache_remaining']
        assert {str(p):checksum(p) for p in source.rglob('*') if p.is_file()}==original
        files={str(p):checksum(p) for p in target.rglob('*.parquet')}
        assert pub.execute()==0
        assert {str(p):checksum(p) for p in target.rglob('*.parquet')}==files
        assert len(list((target/'conversion/records').glob('*.json')))==2
        monkeypatch.syspath_prepend(str(WORKSPACE/'scripts'))
        spec=importlib.util.spec_from_file_location('rebuild_controller',WORKSPACE/'scripts/rerun_sharpa_1016_v3.py')
        controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
        old=tmp_path/'delivery';shutil.copytree(target,old)
        switch_control=tmp_path/'switch';switch_control.mkdir()
        event=controller.promote(target,old,switch_control)
        assert event['state']=='completed' and Path(event['backup']).exists()
        assert not target.exists() and (old/'production/delivery_notice.json').exists()
        assert {p.name:checksum(p) for p in old.rglob('*.parquet')}=={p.name:checksum(p) for p in Path(event['backup']).rglob('*.parquet')}

    finally:
        shutil.rmtree(control)


def test_upload_hash_and_cleanup_guard(tmp_path):
    pub=load_publisher()
    source=tmp_path/'source';source.write_bytes(b'unchanged')
    target=tmp_path/'dest/file'
    assert pub.upload_file(source,target)['sha256']==checksum(source)
    outside=tmp_path/'outside';outside.mkdir()
    cache=tmp_path/'cache';cache.mkdir()
    with pytest.raises(ValueError):pub.guarded_remove(outside,cache)
    with pytest.raises(ValueError):pub.guarded_remove(cache,cache)
    assert outside.exists()


def test_separate_exception_assessment_never_publishes(tmp_path):
    from convest.config import load_config
    pub=load_publisher()
    spec=importlib.util.spec_from_file_location('recovery_assess',WORKSPACE/'scripts/assess_sharpa_1016_exceptions.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    source=tmp_path/'original';bag=make_conversion_bag(source/'episode1','sharpa',frames=30)
    state=json.loads((bag/'collection_state.json').read_text());state.update(state='discarded',finalized=False)
    atomic_json(bag/'collection_state.json',state)
    original={p.name:checksum(p) for p in bag.iterdir() if p.is_file()}
    target=tmp_path/'target';target.mkdir();control=tmp_path/'control';(control/'cache').mkdir(parents=True)
    inv={'source_root':str(source),'target_root':str(target),'episodes':['episode1'],
         'original_sizes':{'episode1':sum(p.stat().st_size for p in bag.iterdir())}}
    result=module.assess(inv,load_config(WORKSPACE/'configs/sharpa.yaml'),control,pub.stage_source,pub.guarded_remove)
    assert result['assessed']==1 and result['published']==0
    assert result['results'][0]['technical_result']=='continuous_multimodal_candidate'
    assert not (target/'data').exists() and not list((control/'cache').iterdir())
    assert {p.name:checksum(p) for p in bag.iterdir() if p.is_file()}==original
