"""Independent coverage oracle: no detector state machine, no missing-data bridge."""
import numpy as np


def audit_windows(times, positions, pair_valid, cuts, config, *, raise_on_error=True):
    count=len(times)
    valid=np.asarray(pair_valid,dtype=bool).copy()
    quiet=np.zeros(count,dtype=bool)
    half=round(config.motion_window_s*1e9/2)
    # Recompute each complete window directly, independently of detect_runs.
    for i in range(count):
        if half:
            lo=max(0,int(np.searchsorted(times,int(times[i])-half,side='right'))-1)
            hi=min(count-1,int(np.searchsorted(times,int(times[i])+half)))
            complete=times[lo]<=times[i]-half and times[hi]>=times[i]+half
        else:
            lo,hi,complete=max(0,i-1),i,True
        if not complete or not np.asarray(pair_valid[lo:hi+1]).all():
            valid[i]=False
            continue
        quiet[i]=np.ptp(positions[lo:hi+1],axis=0).max()<=config.position_tolerance_rad
    movement=np.flatnonzero(valid & ~quiet)
    mask=valid & quiet
    edges=np.diff(np.r_[False,mask,False].astype(np.int8))
    ranges=sorted((int(a),int(b)) for a,b,*_ in cuts)
    missed=[];eligible=[];unsafe=[]
    for a,b in zip(np.flatnonzero(edges==1),np.flatnonzero(edges==-1)):
        # Protect recording-leading/trailing waits. The interior of every
        # sufficiently long, fully observed internal quiet block must be cut.
        if b-a<=config.stationary_frames or not (movement<a).any() or not (movement>=b).any():
            continue
        begin,end=int(times[a+1]),int(times[b-1])
        if begin>=end:continue
        block={'source_start_ns':str(begin),'source_end_ns':str(end),'observed_frames':int(b-a)}
        eligible.append(block)
        cursor=begin
        for left,right in ranges:
            if left<=cursor<right:cursor=max(cursor,right)
        if cursor<end:missed.append(block)
    for left,right in ranges:
        selected=(times>=left)&(times<right)
        if left>=right or not selected.any() or not mask[selected].all():
            unsafe.append({'source_start_ns':str(left),'source_end_ns':str(right)})
    result={'version':'independent-quiet-blocks-v1','status':'failed' if missed or unsafe else 'passed',
            'criteria':'same measured-arm noise/freshness limits; independent quiet-block coverage and cut safety; no duration split or hand condition',
            'checked_internal_quiet_blocks':len(eligible),'eligible_blocks':eligible,
            'missed_blocks':missed,'unsafe_cuts':unsafe,
            'invalid_pair_frames':int((~np.asarray(pair_valid,dtype=bool)).sum()),
            'invalid_window_frames':int((~valid).sum())}
    if result['status']!='passed' and raise_on_error:
        raise ValueError(f'Independent cleaning audit failed: {len(missed)} missed blocks, {len(unsafe)} unsafe cuts')
    return result
