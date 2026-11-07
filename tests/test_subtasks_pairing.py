from types import SimpleNamespace
from convest.subtasks.pairing import attach_speedup
from convest.subtasks import remote


def row(id_,recording):
    return dict(id=id_,remote_directory='/mnt/data/train_platform/datasets/tomato_sharpa/normal/x/A/batch',variant='normal',
                excluded=False,episode_index=int(id_),pair_state='missing',source_recording_id=recording,source_start_ns='100',source_end_ns='200')


def test_automatic_pairing_uses_source_not_episode_number_and_skips_ambiguous(tmp_path,monkeypatch):
    rows=[row('0','a'),row('1','b'),row('2','c')];calls=[]
    def ep(index,recording):return dict(episode_index=index,source_recording_id=recording,source_start_ns=100,source_end_ns=200)
    monkeypatch.setattr(remote,'discover',lambda scope:{'packages':[
        {'path':scope+'/speed','episodes':[ep(8,'a'),ep(1,'unrelated'),ep(9,'b')]},
        {'path':scope+'/duplicate','episodes':[ep(2,'b')]}]})
    def load(path,selected):
        calls.append((path,selected));rows[0]['pair_state']='matched';return {'imported':1}
    ws=SimpleNamespace(catalog=lambda:{'episodes':rows},_import_directory=load)
    report=attach_speedup(ws,rows[0]['remote_directory'])
    assert calls==[('/mnt/data/train_platform/datasets/tomato_sharpa/speedup/x/A/speed',[8])]
    assert report['matched']==1 and report['imported']==1
    assert len(report['issues'])==2
    assert '多个' in report['issues'][0]['reason'] and '未找到' in report['issues'][1]['reason']
    rows[1]['excluded']=True;rows[2]['excluded']=True
    monkeypatch.setattr(remote,'discover',lambda *a: (_ for _ in ()).throw(AssertionError('无需重复扫描')))
    assert attach_speedup(ws,rows[0]['remote_directory'])['matched']==1


def test_normal_import_reports_pairing_failure_without_losing_primary(tmp_path,monkeypatch):
    from test_subtasks import dataset
    from convest.subtasks.core import Store
    from convest.subtasks.workspace import Workspace
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    path='/mnt/data/train_platform/datasets/tomato_sharpa/normal/x/A/batch'
    primary={'directory':path,'imported':1,'episodes':1,'reused':0}
    monkeypatch.setattr(ws,'_import_directory',lambda *a:dict(primary))
    monkeypatch.setattr(remote,'discover',lambda *a: (_ for _ in ()).throw(ValueError('SSH暂时不可用')))
    # Force a real normal row for discovery; normal data stays registered on failure.
    monkeypatch.setattr(ws,'catalog',lambda:{'episodes':[row('0','a')]})
    try:
        result=ws.import_directory(path)
        assert result['imported']==1
        assert result['pairing']['issues']==[{'reason':'SSH暂时不可用'}]
        assert list((ws.root/'pairing-status').glob('*.json'))
    finally:store.lockfile.close()
