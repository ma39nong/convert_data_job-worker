"""Compare actual image payloads and selection against the original local copy."""
from pathlib import Path
import json
import sqlite3
import yaml
from bag_worker.bag import IMAGE_TYPES,image_capture_ns
from convest.sources.gello_rosbag2 import load_compact_time_map


def audit_images(source,cleaned):
    source,cleaned=Path(source).resolve(),Path(cleaned).resolve()
    mapping=load_compact_time_map(cleaned)
    report=json.loads((cleaned/'trim_report.json').read_text())
    assert report['image_time_basis']=='capture'
    meta=yaml.safe_load((source/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    totals={'original_images':0,'kept_images':0,'removed_images':0,
            'preserved_despite_receive_inside_cut':0,'removed_despite_receive_outside_cut':0}
    counts={}
    for fi,rel in enumerate(meta['relative_file_paths']):
        output=cleaned/f'{cleaned.name}_{fi}.db3'
        assert output.is_file(), 'Trial expects each source volume to retain some messages'
        with sqlite3.connect((source/rel).as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(output.as_uri()+'?mode=ro',uri=True) as dst:
            topics={tid:name for tid,name,kind in src.execute('SELECT id,name,type FROM topics') if kind in IMAGE_TYPES}
            for tid,name in topics.items():
                expected_count=0
                for ident,received,data in src.execute('SELECT id,timestamp,data FROM messages WHERE topic_id=? ORDER BY id',(tid,)):
                    capture=image_capture_ns(data[:12]);totals['original_images']+=1
                    cut_capture=any(a<=capture<b for a,b,_ in mapping.intervals)
                    cut_receive=any(a<=received<b for a,b,_ in mapping.intervals)
                    row=dst.execute('SELECT timestamp,data FROM messages WHERE id=?',(ident,)).fetchone()
                    if cut_capture:
                        assert row is None,'Image captured inside an idle cut was retained'
                        totals['removed_images']+=1
                        totals['removed_despite_receive_outside_cut']+=int(not cut_receive)
                        continue
                    assert row is not None,'Valid capture was lost at a delayed receive boundary'
                    stamp,new=row
                    assert image_capture_ns(new[:12])==mapping.apply(capture)
                    assert stamp==mapping.apply(received)
                    assert data[:4]==new[:4] and data[12:]==new[12:],'Image bytes changed beyond the recorded header timestamp'
                    totals['kept_images']+=1;expected_count+=1
                    totals['preserved_despite_receive_inside_cut']+=int(cut_receive)
                assert dst.execute('SELECT count(*) FROM messages WHERE topic_id=?',(tid,)).fetchone()[0]==expected_count
                counts[name]=counts.get(name,0)+expected_count
    return {'status':'passed','all_retained_pixel_payloads_identical':True,
            'all_capture_selection_decisions_match_original_headers':True,'counts':counts,**totals}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('cleaned');p.add_argument('--report',required=True)
    a=p.parse_args();r=audit_images(a.source,a.cleaned)
    Path(a.report).write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r),flush=True)
