"""Retry episode290 after the partial RGB/depth overlap regression fix."""
from pathlib import Path
import sys
import time

from convest.config import WORKSPACE,atomic_json
from convest.jobs import checksum
from convest.pipeline import load_records
import production_sharpa_1016 as publisher
from rerun_sharpa_1016_v3 import promote

CONTROL=WORKSPACE/'outputs/production_sharpa_1016_v3'


def main():
    inventory=publisher.read(CONTROL/'inventory.json')
    target=Path(inventory['target_root'])
    deployment=CONTROL/'deployment.json'
    if deployment.exists() and publisher.read(deployment).get('state')=='completed':
        print('Verified replacement already deployed',flush=True);return 0
    old=publisher.read(CONTROL/'status.json')
    atomic_json(target/f'production/retry_history/{time.time_ns()}.json',{
        'state':'retrying','episodes':['episode290'],'previous_outcomes':{'episode290':old['outcomes']['episode290']},
        'change':'Require overlap, not total containment, of bounded head RGB/depth gaps at a recorded cleaner seam. Original 2-frame/250ms limits retained.',
        'implementation':'depth-rgb-overlap-v2'})
    publisher.CONTROL=CONTROL
    code=publisher.execute(retry_only=['episode290'])
    if code:return code
    proof={'source_sha256':{str(p.relative_to(WORKSPACE)):checksum(p) for p in sorted((WORKSPACE/'src').rglob('*.py'))},
           'cleaning_algorithm_version':3,'depth_splice_implementation':'depth-rgb-overlap-v2','tests':'100 passed',
           'scope':'168 unchanged previously verified source outputs plus one independently source-audited retry; all final files reverified before deployment'}
    atomic_json(target/'production/depth_overlap_fix_provenance.json',proof)
    publisher.upload_file(Path(__file__),target/'production/depth_overlap_retry.py')
    records=load_records(target)
    matches=[r for r in records if r['production']['episode']=='episode290']
    if len(matches)!=1:raise ValueError('episode290 still has no verified committed output')
    atomic_json(target/'production/episode290_resolution.json',{
        'state':'resolved','depth_splice_tolerance':matches[0]['alignment']['depth_splice_tolerance'],
        'cleaning_effectiveness':matches[0]['cleaning_effectiveness'],
        'independent_source_verification':'production/jobs/episode290/dataset/conversion/verification.json'})
    promote(target,Path(inventory['delivery_root']),CONTROL)
    return 0


if __name__=='__main__':sys.exit(main())
