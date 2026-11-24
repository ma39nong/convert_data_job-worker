import copy
from types import SimpleNamespace
import pytest
from convest.subtasks import fine_release,fine_cycles


def fixture():
    ep=SimpleNamespace(fps=30,length=900)
    coarse={'segments':[{'start_frame':a,'end_frame':b,'label':l} for a,b,l in zip([0,30,300,360,810],[30,300,360,810,900],fine_cycles.LABELS)]}
    def e(held,released):return dict(last_held_frame=held,first_released_frame=released,left_hand_empty=True,fruit_in_tray=True,evidence_views=['cam0','cam1'],observation='fruit now in tray and hand empty')
    doc={'sequence_supported':True,'review_notes':[],'rounds':[
        {'coarse_phase_index':2,'coverage_complete':True,'events':[e(120,123),e(270,273)],'uncertainties':[]},
        {'coarse_phase_index':4,'coverage_complete':True,'events':[e(720,723)],'uncertainties':[]}]}
    manifest={'source_frame_indices':list(range(0,900,3)),'camera_order':['cam0','cam1']}
    return ep,coarse,doc,manifest


def test_release_events_derive_conservative_cuts_one_fruit_whole_round():
    ep,c,d,m=fixture();a=fine_release.convert(d,ep,c,m);rows=fine_cycles.parse(a,ep,c)
    assert [r['end_frame'] for r in rows]==[30,123,300,360,810,900]
    assert a['harvests'][0]['cycles'][0]['delivery_seconds']==123/30
    assert all(r['reviewed'] is False for r in rows)
    assert a['harvests'][1]['cycles'][0]['start_seconds']==12
    assert a['harvests'][1]['cycles'][0]['end_seconds']==27


@pytest.mark.parametrize('failure',['still_held','missing_tray','unseen_frame','overlap','incomplete','uncertainty','empty'])
def test_no_hard_split_without_release_evidence(failure):
    ep,c,d,m=fixture();h=d['rounds'][0];e=h['events'][0]
    if failure=='still_held':e['left_hand_empty']=False
    if failure=='missing_tray':e['fruit_in_tray']=False
    if failure=='unseen_frame':e['first_released_frame']=124
    if failure=='overlap':h['events'][1]['last_held_frame']=90
    if failure=='incomplete':h['coverage_complete']=False
    if failure=='uncertainty':h['uncertainties']=[{'start_frame':33,'end_frame':60,'reason':'occluded'}]
    if failure=='empty':h['events']=[]
    a=fine_release.convert(d,ep,c,m);rows=fine_cycles.parse(a,ep,c)
    assert len(a['harvests'][0]['cycles'])==1
    assert a['harvests'][0]['cycles'][0]['successful'] is None
    assert rows[1]['label']=='unknown' and rows[1]['start_frame']==30 and rows[1]['end_frame']==300


def test_same_release_repeated_is_one_event_not_two_fruits():
    ep,c,d,m=fixture();h=d['rounds'][1];h['events'].append(copy.deepcopy(h['events'][0]))
    a=fine_release.convert(d,ep,c,m)
    assert len(a['harvests'][1]['cycles'])==1


def test_legacy_cycle_response_not_accepted_as_new_evidence():
    ep,c,d,m=fixture()
    with pytest.raises(ValueError):fine_release.convert({'sequence_supported':True,'harvests':[],'review_notes':[]},ep,c,m)


def test_source_frame_stamp_is_same_frame_in_all_views():
    import numpy as np
    from convest.subtasks.source_frame_label import stamp
    canvas=np.full((720,960,3),100,dtype=np.uint8);stamp(canvas,4,480,360,720,30)
    # F720 T24.000 glyph region must be identical in each quadrant; video pixels below stay unchanged.
    for y,x in [(0,480),(360,0),(360,480)]:
        assert np.array_equal(canvas[4:19,41:185],canvas[y+4:y+19,x+41:x+185])
    assert (canvas[30:350,:]==100).all()


def test_failed_multiview_experiment_is_not_wired_into_production():
    import inspect
    assert 'fine_release' not in inspect.getsource(fine_cycles.process)
