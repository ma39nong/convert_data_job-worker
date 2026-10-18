"""Rebuild the 189 eligible sources; retain old delivery until verified replacement."""
from pathlib import Path
import json
import sys
import time
import fcntl

from convest.config import WORKSPACE, atomic_json
from convest.pipeline import load_records
from convest.jobs import checksum
import production_sharpa_1016 as publisher

CONTROL=WORKSPACE/'outputs/production_sharpa_1016_v3'


def promote(staging, final, control):
    staging,final,control=map(Path,(staging,final,control))
    deployment=control/'deployment.json'
    if deployment.exists() and publisher.read(deployment).get('state')=='completed':
        return publisher.read(deployment)
    if staging.is_symlink() or final.is_symlink() or staging.parent!=final.parent or staging==final:
        raise ValueError('Unsafe deployment paths')
    old_owner=publisher.read(final/'production/owner.json')
    new_owner=publisher.read(staging/'production/owner.json')
    if old_owner.get('owner')!=publisher.OWNER or new_owner.get('owner')!=publisher.OWNER:
        raise ValueError('Only owned production outputs can be switched')
    verification=publisher.read(staging/'conversion/verification.json')
    status=publisher.read(staging/'production/status.json')
    if verification['status']!='passed' or status['state']!='completed' or status.get('recovery_assessed'):
        raise ValueError('Incomplete rebuild; deployment refused')
    records=load_records(staging)
    if not records or verification.get('episodes') != sum(len(r['episodes']) for r in records) or verification.get('failed_episodes') != 0:
        raise ValueError('Verification counts differ from committed records')
    for record in records:
        if record.get('production',{}).get('cleaning_algorithm_version')!=3 or record.get('cleaning_effectiveness',{}).get('status')!='passed':
            raise ValueError('Missing independent cleaning effectiveness proof')
        for path,proof in record['production']['artifacts'].items():
            if checksum(staging/path)!=proof['sha256']:
                raise ValueError(f'Artifact changed after verification: {path}')
    old_status=publisher.read(final/'production/status.json')
    new_status=publisher.read(staging/'production/status.json')
    old_good={n for n,o in old_status['outcomes'].items() if o['state']=='published'}
    new_good={n for n,o in new_status['outcomes'].items() if o['state']=='published'}
    comparison={'previous_published':len(old_good),'new_published':len(new_good),
                'newly_accepted':sorted(new_good-old_good),'previously_accepted_now_excluded':sorted(old_good-new_good)}
    atomic_json(control/'comparison.json',comparison)
    # Any new rejection requires investigation; retain the old tree and fixed
    # candidate intact rather than silently reduce delivery coverage.
    if old_good-new_good:
        atomic_json(deployment,{'state':'blocked_by_new_quality_exclusions',**comparison})
        raise ValueError('Rebuild excludes previously published sources; see comparison.json')
    backup=final.with_name(final.name+'_before_idle_v3_'+time.strftime('%Y%m%d_%H%M%S'))
    if backup.exists():raise FileExistsError(backup)
    event={'state':'switching','staging':str(staging),'final':str(final),'backup':str(backup),'started_ns':time.time_ns(),**comparison}
    atomic_json(deployment,event)
    with (final/'production/lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        final.rename(backup)
        try:
            staging.rename(final)
        except BaseException:
            backup.rename(final)
            raise
    event.update(state='completed',finished_ns=time.time_ns())
    atomic_json(final/'production/delivery_notice.json',{'state':'verified_replacement','delivery_ready':True,'cleaning_algorithm_version':3,'independent_cleaning_coverage':'passed_for_all_published_sources','backup':str(backup)})
    atomic_json(final/'production/deployment.json',event)
    atomic_json(deployment,event)
    # Existing manifest records where conversion occurred; record the location
    # change explicitly without rewriting the independently audited provenance.
    print(json.dumps(event,ensure_ascii=False),flush=True)
    return event


def main():
    deployed=CONTROL/'deployment.json'
    if deployed.exists() and publisher.read(deployed).get('state')=='completed':
        print('Verified replacement already deployed',flush=True);return 0
    inventory=publisher.read(CONTROL/'inventory.json')
    publisher.CONTROL=CONTROL
    code=publisher.execute()
    if code:return code
    target=Path(inventory['target_root'])
    publisher.upload_file(Path(__file__),target/'production/rebuild_controller.py')
    provenance={'cleaning_algorithm_version':3,'source_sha256':{str(p.relative_to(WORKSPACE)):checksum(p) for p in sorted((WORKSPACE/'src').rglob('*.py'))},'independent_cleaning_coverage_required':True}
    atomic_json(target/'production/rebuild_provenance.json',provenance)
    promote(target,Path(inventory['delivery_root']),CONTROL)
    return 0


if __name__=='__main__':
    sys.exit(main())
