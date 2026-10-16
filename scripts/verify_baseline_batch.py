"""Full output audit, including equality to the unfiltered episode36 baseline detector."""
import json
from pathlib import Path
import sys

from bag_worker.bag import Bag
from bag_worker.idle import IdleConfig,inspect_bag
from verify_smoke import audit


def verify(root):
    root=Path(root).resolve();batch=json.loads((root/'batch_report.json').read_text())
    assert batch and all(item['status']=='completed' for item in batch)
    results=[]
    for item in batch:
        output=Path(item['output']);report=json.loads((output/'trim_report.json').read_text());d=report['auto_idle_detection']
        assert d['algorithm']=='windowed_motion_causal_state' and d['version']==2
        assert d['config'].get('priority_policy') is None and d['config'].get('command_topic') is None
        assert d['config'].get('hold_bridge_max_s',0)==0
        assert 'priority_policy' not in d and 'hold_confirmation' not in d
        expected=inspect_bag(Bag(item['source']),IdleConfig())
        assert d['ranges_seconds']==expected['ranges_seconds']
        assert [(int(x['source_start_ns']),int(x['source_end_ns'])) for x in report['mapping']]==[(int(x['start_ns']),int(x['end_ns'])) for x in expected['runs']]
        if output.name=='episode36':
            old=json.loads((Path(__file__).resolve().parents[1]/'docs/smoke_trim_report.json').read_text())
            assert report['mapping']==old['mapping']
        result={'episode':output.name,'output':str(output),'baseline_intervals_exact_match':True,
                'deleted_intervals':len(report['mapping']),'deleted_seconds':report['deleted_seconds'],
                'supplemental_hold_detection':'no_independent_source_defined','supplemental_deleted_intervals':0,
                **audit(item['source'],output)}
        results.append(result)
        pending=root/'baseline_verification.json.tmp';pending.write_text(json.dumps(results,ensure_ascii=False,indent=2));pending.replace(root/'baseline_verification.json')
        print(output.name,'baseline and',result['checked_messages'],'retained messages verified',flush=True)
    return results


if __name__=='__main__':verify(sys.argv[1])
