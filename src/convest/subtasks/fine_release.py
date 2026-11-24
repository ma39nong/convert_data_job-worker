"""Experimental release-event evaluator, NOT enabled in production fine processing.

2026-11-23 pilot failed manual count comparison (8+2 versus 6+1).
Do not enable automatically or treat model-provided release evidence as verified.
"""
import copy
import hashlib
import json
from pathlib import Path
from .core import atomic_json,sha

EVIDENCE_POLICY='qwen-multiview-confirmed-release-events-v1'

def prepare(ep,output):
    from .doubao import make_montage
    source=ep.identity();ep.validate_media(Path(output),source)
    key=hashlib.sha256(json.dumps({'source':source,'policy':EVIDENCE_POLICY,'sample_fps':10,'panel_size':480},sort_keys=True).encode()).hexdigest()
    root=Path(output)/'qwen'/'multiview';root.mkdir(parents=True,exist_ok=True)
    video=root/(key+'.mp4');record=root/(key+'.json')
    if record.exists() and video.exists():
        manifest=json.loads(record.read_text())
        if manifest.get('sha256')==sha(video):return video,manifest
    manifest=make_montage(ep,video,0,ep.length,sample_fps=10,panel_size=480,frame_labels=True)
    manifest.update(policy=EVIDENCE_POLICY,source_fps=ep.fps,source_length=ep.length,frame_labels=True)
    if ep.identity()!=source:raise ValueError('生成多视角输入期间来源变化')
    atomic_json(record,manifest)
    return video,manifest


def prompt(ep,coarse,manifest):
    text=Path(__file__).with_name('qwen_fine_release_prompt.txt').read_text()
    parents=[{'coarse_phase_index':i+1,'label':r['label'],'start_frame':r['start_frame'],'end_frame':r['end_frame']} for i,r in enumerate(coarse['segments'])]
    return text+'\n'+json.dumps({'source_fps':ep.fps,'source_length':ep.length,
        'camera_order':manifest['camera_order'],'presented_frame_step':manifest['source_frame_indices'][1]-manifest['source_frame_indices'][0] if len(manifest['source_frame_indices'])>1 else 1,
        'coarse_phases':parents},ensure_ascii=False)


def convert(doc,ep,coarse,manifest):
    """Cuts derive only from explicit release events, never a model cycle count."""
    if not isinstance(doc,dict) or doc.get('sequence_supported') is not True:raise ValueError('无法确认本条采摘流程，未生成细草稿')
    rounds=doc.get('rounds');notes=doc.get('review_notes',[])
    if not isinstance(rounds,list) or len(rounds)!=2 or not isinstance(notes,list) or any(not isinstance(x,str) for x in notes):raise ValueError('释放事件格式错误')
    frames=set(manifest['source_frame_indices']);views=set(manifest['camera_order'])
    result={'sequence_supported':True,'harvests':[],'review_notes':copy.deepcopy(notes),
        'release_evidence_policy':EVIDENCE_POLICY,'release_rounds':copy.deepcopy(rounds)}
    for item,phase in zip(rounds,(2,4)):
        parent=coarse['segments'][phase-1];a,b=parent['start_frame'],parent['end_frame']
        if not isinstance(item,dict) or item.get('coarse_phase_index')!=phase:raise ValueError('投放轮次与粗阶段不匹配')
        events=item.get('events');uncertainties=item.get('uncertainties')
        if type(item.get('coverage_complete')) is not bool or not isinstance(events,list) or not isinstance(uncertainties,list):raise ValueError('缺少整轮可见性核对')
        if len(events)>ep.length:raise ValueError('投放事件数量无效')
        valid=[];seen=set();errors=[]
        for event in events:
            if not isinstance(event,dict):raise ValueError('投放证据格式错误')
            held,released=event.get('last_held_frame'),event.get('first_released_frame')
            evidence_views=event.get('evidence_views');observation=event.get('observation')
            if (type(held) is not int or type(released) is not int or held not in frames or released not in frames
                or not a<=held<released<=b or event.get('left_hand_empty') is not True or event.get('fruit_in_tray') is not True
                or not isinstance(evidence_views,list) or not evidence_views or any(v not in views for v in evidence_views)
                or not isinstance(observation,str) or not observation.strip()):
                errors.append('投放缺少有效的持果→释放证据');continue
            if (held,released) in seen:continue  # identical repeated event is one fruit
            if valid and held<valid[-1]['first_released_frame']:
                errors.append('投放证据重叠或乱序，不能确认是另一颗');continue
            seen.add((held,released));valid.append(event)
        complete=item['coverage_complete'] and not uncertainties and not errors and bool(valid)
        cycles=[]
        if not complete:
            cycles=[{'start_seconds':a/ep.fps,'end_seconds':b/ep.fps,'delivery_seconds':None,'successful':None,'needs_review':True,'observation':'本轮投放证据不完整，保留整轮待确认；不硬拆颗数。'}]
            result['review_notes'].append(f'第{phase}粗阶段投放证据不完整，整轮待确认；'+ '；'.join(errors))
        else:
            start=a
            for i,event in enumerate(valid):
                release=event['first_released_frame'];end=b if i==len(valid)-1 else release
                if end<=start:raise ValueError('释放事件产生空周期')
                cycles.append({'start_seconds':start/ep.fps,'end_seconds':end/ep.fps,'delivery_seconds':release/ep.fps,
                    'successful':True,'needs_review':True,'observation':event['observation'],
                    'release_bracket_frames':[event['last_held_frame'],release],'evidence_views':event['evidence_views']})
                start=end
        result['harvests'].append({'coarse_phase_index':phase,'cycles':cycles})
    result['review_notes'].append('同一次请求使用同步多视角与源帧标记；内部按模型首次确认已释放入盘的采样帧切分，非关节起点。模型证据尚需人工核对，不能证明原始帧首个释放时刻。')
    return result
