"""30 Hz multimodal causal alignment; reject internal gaps, trim only invalid edges."""
import numpy as np
from convest.align import align, valid_segments
from convest.quality import RecordingRejected
from convest.sources.robot_rosbag2 import modalities


def validate_config(config):
    if config['robot'] not in ('wuji', 'sharpa') or config['segments'] != 'full-all':
        raise ValueError('Use robot=wuji/sharpa and segments=full-all')
    if config['fps'] != 30 or config['window'] != 'validated':
        raise ValueError('This target uses validated windows at 30 Hz')
    for key in ('max_staleness_ms','camera_short_gap_max_age_ms'):
        if not np.isfinite(config[key]) or config[key] <= 0:
            raise ValueError(f'Invalid {key}')
    if config['camera_short_gap_max_frames'] not in (0,1,2):
        raise ValueError('Camera tolerance must be 0, 1 or 2 frames')
    if not 0 <= config['video_crf'] <= 51 or config['video_threads'] < 1:
        raise ValueError('Invalid encoder settings')


def validate_contract(contract, config):
    expected = (108,54) if config['robot']=='wuji' else (116,58)
    actual = tuple(contract['features'][k]['dimension'] for k in ('observation.state','action'))
    if actual != expected:
        raise ValueError(f'Contract dimensions {actual} do not match {expected}')


def prepare(source, bag, item, config, contract):
    streams = source.read_streams(bag,contract)
    expected_hand_dim = 20 if config['robot']=='wuji' else 22
    if any(len(streams[f'action.{s}_hand'].values[0]) != expected_hand_dim for s in ('left','right')):
        raise ValueError('Input robot differs from configured robot')
    aligned = align(streams,item['source_start_ns'],item['source_end_ns'],config['fps'],
                    int(config['max_staleness_ms']*1e6),config['min_segment_frames'],
                    camera_short_gap_max_frames=config['camera_short_gap_max_frames'],
                    camera_short_gap_max_age_ns=int(config['camera_short_gap_max_age_ms']*1e6),
                    allow_empty_segments=True)
    extra_good={}
    extra_ages={}
    for key in modalities(config['robot']):
        series=streams[key]
        index=series.indices(aligned.timeline)
        age=aligned.timeline-series.times[np.maximum(index,0)]
        good=(index>=0)&(age>=0)&(age<=int(config['max_staleness_ms']*1e6))
        extra_good[key]=good;extra_ages[key]=age
        aligned.source_indices[key]=index
    # Depth is paired with head RGB. A receive-time cut can remove its next
    # exposure even though its header predates the splice. Never re-stamp it.
    # RGB/depth exposures need not coincide: require overlap of the two short
    # gaps, not identical coverage of every depth grid point.
    depth='observation.depths.cam0';accepted=[]
    boundaries=bag.compact_time_map.compact_boundaries if bag.compact_time_map else ()
    other_good=aligned.valid_mask.copy()
    for key,good in extra_good.items():
        if key!=depth:other_good &= good
    rgb_allowed=np.zeros(len(aligned.timeline),dtype=bool)
    for gap in aligned.report['camera_short_gap_tolerance']['gaps']:
        if 'cam0' in gap['cameras']:
            rgb_allowed[slice(*gap['bounds'])]=True
    for a,b in valid_segments(~extra_good[depth],1):
        if a==0 or b==len(aligned.timeline) or b-a>config['camera_short_gap_max_frames']:continue
        nearby=[c for c in boundaries if aligned.timeline[a-1]<=c<=aligned.timeline[b]]
        age=extra_ages[depth][a:b]
        if (not nearby or not rgb_allowed[a:b].any() or not other_good[a-1:b+1].all()
            or (age<0).any() or (age>config['camera_short_gap_max_age_ms']*1e6).any()
            or (aligned.source_indices[depth][a:b]<0).any()):continue
        extra_good[depth][a:b]=True
        accepted.append({'bounds':[a,b],'compact_boundaries_ns':[int(c) for c in nearby],
                         'actual_depth_age_ms':(age/1e6).tolist(),
                         'head_rgb_overlap_frames':int(rgb_allowed[a:b].sum()),
                         'sample_timestamp_ns':streams[depth].times[aligned.source_indices[depth][a:b]].tolist()})
    aligned.report['depth_splice_tolerance']={
        'policy':'only recorded cleaner seams overlapping accepted head RGB gaps; never retimestamp',
        'implementation_version':'depth-rgb-overlap-v2',
        'max_frames':config['camera_short_gap_max_frames'],
        'max_age_ms':config['camera_short_gap_max_age_ms'],
        'accepted_frames':sum(g['bounds'][1]-g['bounds'][0] for g in accepted),'gaps':accepted}
    for key,good in extra_good.items():
        aligned.valid_mask &= good
        aligned.report['unmatched'][key]=int((~good).sum())
        age=extra_ages[key]
        aligned.report['max_age_ms'][key]=float(age[good].max()/1e6) if good.any() else None
    aligned.segments=valid_segments(aligned.valid_mask,config['min_segment_frames'])
    aligned.report.update(invalid_frames=int((~aligned.valid_mask).sum()),
                          retained_frames=sum(b-a for a,b in aligned.segments),segment_count=len(aligned.segments),
                          source_time_adapter=item.get('source_time_adapter'),
                          status_timestamp_policy={k:source.timestamp_policy(k,bag.compact_time_map)
                                                   for k in sorted(source.STATUS_MESSAGE_TYPES)},
                          hand_status_policy=('measured_telemetry' if config['robot']=='wuji' else
                                              'not_recorded; fresh_absolute_commands_required'),
                          extra_modality_policy='strict causal <=150ms; depth-only bounded holds at known cleaner seams, logged separately',
                          timestamp_semantics='source_timestamp_ns is compact header/policy time; audit.original_timestamp_ns inverts cleaner mapping')
    seams=[]
    if bag.compact_time_map:
        for boundary in bag.compact_time_map.compact_boundaries:
            i=int(np.searchsorted(aligned.timeline,boundary))
            if not 0<i<len(aligned.timeline):continue
            delta={};state_offset=action_offset=0
            for group in ('left_arm','right_arm','left_hand','right_hand'):
                n=len(streams[f'action.{group}'].values[0])
                delta[group]={
                    'state_position_max_abs_delta_rad':float(np.max(np.abs(aligned.state[i,state_offset:state_offset+n]-aligned.state[i-1,state_offset:state_offset+n]))),
                    'action_max_abs_delta_rad':float(np.max(np.abs(aligned.action[i,action_offset:action_offset+n]-aligned.action[i-1,action_offset:action_offset+n])))}
                state_offset+=2*n;action_offset+=n
            seams.append({'compact_boundary_ns':int(boundary),'grid_indices':[i-1,i],
                          'both_frames_valid':bool(aligned.valid_mask[i-1:i+1].all()),'groups':delta})
    aligned.report['splice_audit']={'policy':'report only; does not change accepted trimming decisions','seams':seams}
    return streams,aligned


def _continuous_bounds(aligned, start, end, label, minimum):
    """Trim invalid range edges, but reject every invalid point inside the retained range."""
    count = len(aligned.timeline)
    if not 0 <= start < end <= count:
        raise ValueError(f"{label}: invalid requested frame range [{start}, {end})")
    valid = np.asarray(aligned.valid_mask, dtype=bool)
    if valid.shape != (count,):
        raise ValueError("Aligned validity mask does not match the output timeline")
    positions = np.flatnonzero(valid[start:end])
    if not len(positions):
        raise RecordingRejected(
            f"Whole recording excluded: {label} has no valid aligned frames",
            {
                "policy": "continuous_export_range",
                "export_range": label,
                "requested_bounds": [start, end],
                "trimmed_bounds": None,
                "gaps": [],
                "failure": "no_valid_frames",
            },
        )
    trimmed_start = start + int(positions[0])
    trimmed_end = start + int(positions[-1]) + 1
    internal_invalid = ~valid[trimmed_start:trimmed_end]
    if internal_invalid.any():
        edges = np.diff(np.r_[False, internal_invalid, False].astype(np.int8))
        gaps = []
        for relative_start, relative_end in zip(
                np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
            gap_start = trimmed_start + int(relative_start)
            gap_end = trimmed_start + int(relative_end)
            gaps.append({
                "bounds": [gap_start, gap_end],
                "frames": gap_end - gap_start,
                "source_start_ns": int(aligned.timeline[gap_start]),
                "source_end_ns": int(aligned.timeline[gap_end]),
            })
        raise RecordingRejected(
            f"Whole recording excluded: {label} contains an internal invalid interval",
            {
                "policy": "continuous_export_range",
                "export_range": label,
                "requested_bounds": [start, end],
                "trimmed_bounds": [trimmed_start, trimmed_end],
                "gaps": gaps,
            },
        )
    if trimmed_end - trimmed_start < minimum:
        raise RecordingRejected(
            f"Whole recording excluded: {label} is shorter than min_segment_frames",
            {
                "policy": "continuous_export_range",
                "export_range": label,
                "requested_bounds": [start, end],
                "trimmed_bounds": [trimmed_start, trimmed_end],
                "gaps": [],
                "failure": "shorter_than_min_segment_frames",
                "minimum_frames": minimum,
                "actual_frames": trimmed_end - trimmed_start,
            },
        )
    return trimmed_start, trimmed_end



def output_segments(item,aligned,config):
    bounds=_continuous_bounds(aligned,0,len(aligned.timeline),'full-all',config['min_segment_frames'])
    aligned.report['selected_export']={'mode':'full-all','frames':bounds[1]-bounds[0],
                                     'episode_count':1,'ranges':[{'bounds':list(bounds)}]}
    return [{'bounds':bounds,'segment_id':'full_all','task':config['task'],
             'source_recording_id':item.get('source_recording_id') or item['path']}]
