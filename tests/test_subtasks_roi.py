import base64
import json
import shutil
import av
import numpy as np
import pytest
from convest.subtasks.core import Episode,Store,atomic_json
from convest.subtasks.workspace import Workspace
from convest.subtasks.doubao import make_montage
from convest.subtasks.template import images_at
from convest.subtasks.processing_queue import remaining,create
from test_subtasks import dataset


def four_views(root,**kwargs):
    old=dataset(root,**kwargs);name='observation.images.interaction'
    info=old.info;info['features'][name]={'dtype':'video','shape':[32,32,3]}
    atomic_json(root/'meta/info.json',info)
    target=root/'videos/chunk-000'/name/'episode_000000.mp4';target.parent.mkdir(parents=True)
    shutil.copyfile(old.cameras['observation.images.cam0'],target)
    return Episode(root,0)


def test_four_composite_panels_and_source_frame_stills_are_synchronized(tmp_path):
    ep=four_views(tmp_path/'normal'/'full-block-more'/'subtask-roi'/'A'/'batch')
    m=make_montage(ep,tmp_path/'composite.mp4',20,41)
    assert m['camera_order'][-1]=='observation.images.interaction'
    assert m['source_frame_indices']==list(range(20,41,2))
    with av.open(str(tmp_path/'composite.mp4')) as c:frames=list(c.decode(video=0))
    for i,f in enumerate(frames):
        rgb=f.to_ndarray(format='rgb24');pixels=[rgb[y,x].astype(int) for y,x in [(180,240),(180,720),(540,240),(540,720)]]
        assert max(np.max(np.abs(pixels[0]-p)) for p in pixels)<8
        assert np.max(pixels[3])>100
        assert (pixels[0][0]>pixels[0][1])==(i<5)
    stills=images_at(ep,[20,30])
    for still in stills:
        with av.open(__import__('io').BytesIO(base64.b64decode(still['data_url'].split(',')[1]))) as c:rgb=next(c.decode(video=0)).to_ndarray(format='rgb24')
        assert np.max(np.abs(rgb[180,240].astype(int)-rgb[540,720]))<8
    # Fail rather than manufacture a missing or shifted fourth stream.
    with av.open(str(ep.cameras['observation.images.interaction']),'w') as c:
        s=c.add_stream('libx264',rate=10);s.width=s.height=32;s.pix_fmt='yuv420p'
        for i in range(3):
            for packet in s.encode(av.VideoFrame.from_ndarray(np.zeros((32,32,3),np.uint8),format='rgb24')):c.mux(packet)
        for packet in s.encode():c.mux(packet)
    with pytest.raises(ValueError):make_montage(ep,tmp_path/'bad.mp4',20,41)


def test_roi_pairs_do_not_cross_to_old_three_views_and_counts_do_not_duplicate(tmp_path):
    oldn=dataset(tmp_path/'normal'/'full-block-more'/'A'/'batch')
    olds=dataset(tmp_path/'speedup'/'full-block-more'/'A'/'batch',speedup=True)
    newn=four_views(tmp_path/'normal'/'full-block-more'/'subtask-roi'/'A'/'batch')
    news=four_views(tmp_path/'speedup'/'full-block-more'/'subtask-roi'/'A'/'batch',speedup=True)
    legacy=dataset(tmp_path/'normal'/'no-block-more'/'A'/'batch',recording='legacy')
    store=Store([e.root for e in (oldn,olds,newn,news,legacy)],tmp_path/'review');ws=Workspace(store)
    try:
        cat=ws.catalog();rows={e['id']:e for e in cat['episodes']}
        assert rows[oldn.id]['superseded_by']==newn.id
        assert rows[olds.id]['superseded_by']==news.id
        assert rows[newn.id]['pair_ids']==[news.id]
        assert rows[news.id]['pair_ids']==[newn.id]
        assert ws.normal_dependency(news.id)['id']==newn.id
        assert ws.normal_dependency(olds.id)['id']==oldn.id
        assert remaining(ws,workflow='without_return')==[newn.id]
        assert remaining(ws,workflow='with_return')==[legacy.id]
        with pytest.raises(ValueError,match='ROI'):create(ws,[oldn.id],'coarse')
        assert not any(rows[e.id]['excluded'] for e in (oldn,olds,newn,news,legacy))
    finally:store.lockfile.close()


def test_model_references_never_decode_unreviewed_library_videos(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from convest.subtasks import doubao,style
    target=dataset(tmp_path/'full-block-more'/'normal'/'target',recording='target')
    unused=dataset(tmp_path/'full-block-more'/'normal'/'unused',recording='unused')
    reviewed=dataset(tmp_path/'full-block-more'/'normal'/'reviewed',recording='reviewed')
    store=Store([e.root for e in (target,unused,reviewed)],tmp_path/'review');ws=Workspace(store)
    try:
        doc=ws.stages(reviewed)['fine'];doc['segments']=[dict(start_frame=0,end_frame=reviewed.length,label='grasp_fruit',reviewed=False)]
        ws.save_stage(reviewed,'fine',doc,confirm_review=True)
        original=store.load;seen=[]
        def load(ep):
            assert ep.id==reviewed.id,'Unreviewed episode must not be decoded as a reference'
            seen.append(ep.id);return original(ep)
        monkeypatch.setattr(store,'load',load)
        monkeypatch.setattr(style,'transition_examples',lambda docs,ep:list(docs))
        monkeypatch.setattr(doubao,'Client',lambda *a,**kw:SimpleNamespace(style_examples=kw['style_examples']))
        c=ws.model_client(target,SimpleNamespace(doubao_key='test',doubao_model='test'))
        assert seen==[reviewed.id] and c.style_examples==[reviewed.id]
    finally:store.lockfile.close()
