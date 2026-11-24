"""Single-fruit proposals constrained by explicitly reviewed coarse intervals."""
import copy
import hashlib
import json
import time
from pathlib import Path

from .core import atomic_json, validate_segments
from .vocabulary import FINE_CYCLE_PROFILE

POLICY='tomato-single-fruit-coarse-range-release-cuts-v3'
PREVIOUS_RELEASE_POLICY='tomato-single-fruit-release-boundaries-v2'

LABELS=['pickup','harvest','adjust','harvest','return']

def binding(coarse):
    rows=[{k:r[k] for k in ('start_frame','end_frame','label','reviewed')} for r in coarse['segments']]
    return {'sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
            'revision':coarse['revision']}

def binding_matches(coarse,snapshot):
    return (bool(coarse.get('saved_at')) and not coarse.get('inherited')
        and [r['label'] for r in coarse['segments']]==LABELS
        and all(r.get('reviewed') is True for r in coarse['segments'])
        and binding(coarse)['sha256']==snapshot.get('sha256'))

def reviewed_coarse(ws,ep):
    from .workflow import in_hand_episode
    if not in_hand_episode(ep) or ws.store.entries[ep.id]['variant']!='normal':
        raise ValueError('Qwen 单颗细标注仅处理无放回 normal；speedup 请从已保存 normal 映射')
    coarse=ws.stages(ep)['coarse']
    if not binding_matches(coarse,binding(coarse)):
        raise ValueError('请先审核并保存五阶段粗标注，再生成单颗细标注')
    return copy.deepcopy(coarse)

def validate_alignment(rows,coarse,*,allow_legacy_wait=False):
    for parent in coarse['segments']:
        children=[r for r in rows if r['start_frame']<parent['end_frame'] and r['end_frame']>parent['start_frame']]
        if (not children or children[0]['start_frame']!=parent['start_frame']
            or children[-1]['end_frame']!=parent['end_frame']
            or any(r['label'] not in (({'harvest','harvest_wait','unknown'} if allow_legacy_wait else {'harvest','unknown'}) if parent['label']=='harvest' else {parent['label'],'unknown'}) for r in children)):
            raise ValueError('细阶段必须保持已审核的粗阶段边界和动作类别')
        waits=[i for i,r in enumerate(children) if r['label']=='harvest_wait']
        if waits and (parent['label']!='harvest' or waits!=[len(children)-1]):
            raise ValueError('采摘后等待只能位于这一轮最后投放之后，内部等待应归下一颗周期')
        if parent['label']!='harvest' and (len(children)!=1 or children[0]['label']!=parent['label']):
            raise ValueError('拿起、调整、放回须各保持一段，不能拆分或更换标签')

def validate_layer(ws,ep,doc):
    if doc.get('fine_profile')!=FINE_CYCLE_PROFILE:return
    if not doc.get('saved_at') and doc.get('fine_policy_current') is False:
        raise ValueError('旧规则细草稿不能按新规则审核，请先重新生成单颗细标注')
    projection=doc.get('projection')
    if projection:
        normal=ws.store.episode(projection['normal_id']);source=ws.stages(normal)['fine']
        if source['revision']!=projection['normal_stage_revision']:
            raise ValueError('normal 细标注已更新，请重新映射后导出')
        validate_layer(ws,normal,source)
        return
    coarse=ws.stages(ep)['coarse']
    if not binding_matches(coarse,doc.get('coarse_binding',{})):
        raise ValueError('粗阶段已变化，请基于当前已审核粗阶段重新生成细标注')
    rows=validate_segments(doc['segments'],ep.length,ep.fps)
    validate_alignment(rows,coarse,allow_legacy_wait=bool(doc.get('saved_at') and doc.get('fine_boundary_policy')!=POLICY))

def prompt(ep,coarse):
    template=Path(__file__).with_name('qwen_fine_prompt.txt').read_text()
    parents=[{'phase_index':i+1,'label':r['label'],'start_seconds':r['start_frame']/ep.fps,
              'end_seconds':r['end_frame']/ep.fps} for i,r in enumerate(coarse['segments'])]
    return template+'\n视频总时长（秒）：'+format(ep.length/ep.fps,'.12g')+'\n已人工审核的粗阶段（固定约束）：\n'+json.dumps(parents,ensure_ascii=False)

def parse(doc,ep,coarse):
    from .qwen import number
    if not isinstance(doc,dict) or doc.get('sequence_supported') is not True:
        raise ValueError('Qwen 无法确认单颗采摘流程，保留原结果，需人工检查')
    harvests=doc.get('harvests')
    if not isinstance(harvests,list) or len(harvests)!=2:raise ValueError('Qwen 须返回两轮采摘周期')
    if not isinstance(doc.get('review_notes'),list) or any(not isinstance(s,str) for s in doc['review_notes']):
        raise ValueError('Qwen 细标注疑点格式错误')
    by_phase={}
    for h,phase in zip(harvests,(2,4)):
        if not isinstance(h,dict) or type(h.get('coarse_phase_index')) is not int or h['coarse_phase_index']!=phase:
            raise ValueError('Qwen 采摘轮次与粗阶段不一致')
        cycles=h.get('cycles');parent=coarse['segments'][phase-1]
        if not isinstance(cycles,list) or not cycles:raise ValueError('Qwen 未返回可核对的单颗周期')
        end=parent['start_frame'];out=[]
        for cycle_index,cycle in enumerate(cycles):
            a,b,t=(cycle.get(k) for k in ('start_seconds','end_seconds','delivery_seconds'))
            if not number(a) or not number(b) or a>=b:raise ValueError('Qwen 单颗周期时间无效')
            start,stop=round(a*ep.fps),round(b*ep.fps)
            if start!=end or not start<stop<=parent['end_frame']:raise ValueError('Qwen 单颗周期越出粗阶段或存在重叠空缺')
            success=cycle.get('successful')
            if success is not True and success is not None:raise ValueError('不成功或不确定的动作请标记 successful=null，不能计作成功')
            if type(cycle.get('needs_review')) is not bool or not isinstance(cycle.get('observation'),str):
                raise ValueError('Qwen 单颗周期缺少观察和复核标记')
            last=cycle_index==len(cycles)-1
            if success is True and (not number(t) or not a<t<=b or (not last and round(t*ep.fps)!=stop)):
                raise ValueError('中间单颗须以释放入盘为界；最后一颗保留至粗阶段终点，投放时间须在该段内')
            if success is None and t is not None:raise ValueError('不确定周期不能编造成功投放时间')
            out.append({'start_frame':start,'end_frame':stop,'label':'harvest' if success else 'unknown','reviewed':False})
            end=stop
        if end!=parent['end_frame']:raise ValueError('每轮细阶段必须完整覆盖粗阶段，最后一颗结束于粗阶段终点')
        if h.get('tail') is not None:raise ValueError('不再单独划等待或收尾，末次投放后的时间归最后一颗')
        by_phase[phase]=out
    rows=[]
    for i,r in enumerate(coarse['segments'],1):
        rows.extend(by_phase[i] if i in by_phase else [{**r,'reviewed':False}])
    rows=validate_segments(rows,ep.length,ep.fps);validate_alignment(rows,coarse)
    return rows

def audit_notes(analysis,coarse):
    """Local discrepancy hints only; never turn reference counts into cycle cuts."""
    notes=[]
    references=(coarse.get('model_analysis') or {}).get('visual_timing_resolution',{}).get('phases',{})
    for h in analysis.get('harvests',[]):
        phase=h['coarse_phase_index'];cycles=h['cycles']
        for cycle in cycles:
            if cycle.get('successful') is None:
                notes.append(f"第{phase}粗阶段 {cycle['start_seconds']:.2f}–{cycle['end_seconds']:.2f}秒为待确认区间，不能计作成功一颗。")
        ref=references.get('first_harvest' if phase==2 else 'second_harvest',{})
        if not ref.get('complete'):continue
        times=[(a+b)/2 for a,b in ref.get('release_intervals_seconds',[])]
        for t in times:
            matching=[c for c in cycles if c['start_seconds']<t<=c['end_seconds']]
            if len(matching)!=1 or matching[0].get('successful') is not True:
                notes.append(f"第{phase}粗阶段：此前视频核对投放线索 {t:.2f}秒未落入模型成功周期，请看片核对；不自动改标签。")
        for cycle in cycles:
            n=sum(cycle['start_seconds']<t<=cycle['end_seconds'] for t in times)
            if cycle.get('successful') is True and n!=1:
                notes.append(f"第{phase}粗阶段 {cycle['start_seconds']:.2f}–{cycle['end_seconds']:.2f}秒模型成功周期与此前视频核对投放线索不一致，需复核；不自动移动边界。")
    return notes

def process(ws,ep,current,*,retry_namespace=None,allow_network=True):
    from . import qwen
    with ws.lock,ws.store.lock:
        existing=ws.stages(ep,current)['fine']
        if existing.get('saved_at'):return 'preserved'
        coarse=reviewed_coarse(ws,ep);snapshot=binding(coarse)
        proposal_path=ws.root/'fine-proposals'/(ep.id+'.json')
        if proposal_path.exists():
            old=json.loads(proposal_path.read_text())
            if (old['source']==current['source'] and old['base_annotation_revision']==current['revision']
                and old.get('fine_profile')==FINE_CYCLE_PROFILE and old.get('provenance',{}).get('policy')==POLICY and binding_matches(coarse,old.get('coarse_binding',{}))):return 'preserved'
    text=prompt(ep,coarse)
    if retry_namespace:text+='\n失败重试：只返回一个完整 JSON 对象；两轮周期必须连续覆盖给定采摘区间，不确定事件明确标 null，禁止补造成功。'
    raw,path,digest,cached,elapsed,identity=qwen.video_response(ep,ws.store.output,text,POLICY,
        retry_namespace=retry_namespace,allow_network=allow_network)
    content=raw['choices'][0]['message'].get('content','').strip()
    if content.startswith('```') and content.endswith('```'):
        content=content[content.index('\n')+1:-3].strip()
    try:analysis=json.loads(content)
    except (TypeError,ValueError):raise ValueError('Qwen 单颗细标注不是完整 JSON；原响应已缓存，可检查后恢复') from None
    rows=parse(analysis,ep,coarse)
    from .fine_local import analyze
    local_report=analyze(ep,coarse,{'segments':rows,'model_analysis':analysis})
    analysis['review_notes'].extend(local_report['notes'])
    provenance={'fine_local_evidence':local_report,'provider':'qwen','model':qwen.MODEL,'policy':POLICY,'model_analysis':analysis,
        'source':identity,'cached':cached,'elapsed_seconds':elapsed,'network_requests':int(not cached),
        'requests':[{'key':digest,'cached':cached,'usage':raw.get('usage',{})}]}
    warning='Qwen 单颗细草稿：首颗从粗起点开始，中间以释放入盘为界，末颗保留至粗终点；不单独拆等待或收尾，每颗 prompt 相同，仍需人工审核。'
    if any(r['label']=='unknown' for r in rows):warning+=' 存在无法确认的周期，已标待确认，不计为成功一颗。'
    with ws.lock,ws.store.lock:
        fresh=ws.store.load(ep)
        from .quarantine import assert_included
        assert_included(ws,ep.id)
        if fresh['source']!=current['source'] or fresh['revision']!=current['revision'] or ep.identity()!=identity:
            raise ValueError('处理期间来源或草稿变化，未覆盖')
        if ws.stage_path(ep.id,'fine').exists():return 'preserved'
        if not binding_matches(ws.stages(ep)['coarse'],snapshot):raise ValueError('分析期间粗阶段变化，未保存旧细草稿')
        if proposal_path.exists():atomic_json(ws.root/'fine-proposal-history'/ep.id/(str(time.time_ns())+'.json'),json.loads(proposal_path.read_text()))
        proposal={'source':identity,'base_annotation_revision':current['revision'],'fine_profile':FINE_CYCLE_PROFILE,
            'coarse_binding':snapshot,'segments':rows,'provenance':provenance,'warning':warning,'created_at':time.time()}
        atomic_json(proposal_path,proposal)
        if not cached:atomic_json(path,raw)
    return 'ready'


def resegment_from_deliveries(analysis,ep,coarse):
    """Reuse explicit successful release events only; never infer fruit or timestamps."""
    doc=copy.deepcopy(analysis)
    if not isinstance(doc.get('harvests'),list) or len(doc['harvests'])!=2:
        raise ValueError('缺少两轮完整投放事件')
    for h,phase in zip(doc['harvests'],(2,4)):
        if h.get('coarse_phase_index')!=phase:raise ValueError('轮次不匹配')
        if h.get('tail') and h['tail'].get('kind')!='wait':raise ValueError('存在不确定尾段，不能按成功投放重组')
        cycles=h.get('cycles')
        if not isinstance(cycles,list) or not cycles or any(c.get('successful') is not True for c in cycles):
            raise ValueError('存在未确认周期，不能自动转换')
        from .qwen import number
        parent=coarse['segments'][phase-1];start=parent['start_frame']/ep.fps;stop=parent['end_frame']/ep.fps
        times=[c.get('delivery_seconds') for c in cycles]
        if any(not number(t) or not start<t<=stop for t in times) or times!=sorted(set(times)):
            raise ValueError('投放事件不完整或顺序无效')
        for i,c in enumerate(cycles):
            c.update(start_seconds=start if i==0 else times[i-1],
                end_seconds=stop if i==len(cycles)-1 else times[i],needs_review=True)
        h.pop('tail',None)
    if not isinstance(doc.get('review_notes'),list):raise ValueError('原始疑点格式不完整')
    doc['review_notes'].append('已在本地复用明确投放时间重组区间：首颗从粗起点开始，内部按投放分界，末颗延续至粗终点；未重新调用模型，原始观察未重算，仍需人工复核。')
    return doc,parse(doc,ep,coarse)


def migrate_release_proposals(ws):
    """Upgrade unsaved v2 candidates without a model call; history remains recoverable."""
    import os
    import tempfile
    migrated=[]
    with ws.lock,ws.store.lock:
        for path in (ws.root/'fine-proposals').glob('*.json'):
            old=json.loads(path.read_text());id_=path.stem
            if id_ not in ws.store.entries or old.get('provenance',{}).get('policy')!=PREVIOUS_RELEASE_POLICY:continue
            if ws.stage_path(id_,'fine').exists():continue
            ep=ws.store.episode(id_);current=ws.store.load(ep)
            if current.get('status')=='reviewed':continue
            if old['source']!=current['source'] or old['base_annotation_revision']!=current['revision']:continue
            coarse_path=ws.stage_path(id_,'coarse')
            if not coarse_path.exists():continue
            coarse=json.loads(coarse_path.read_text())
            if not binding_matches(coarse,old.get('coarse_binding',{})):continue
            try:analysis,rows=resegment_from_deliveries(old['provenance']['model_analysis'],ep,coarse)
            except (ValueError,TypeError,KeyError):continue
            raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
            history=ws.root/'fine-proposal-history'/id_/('before-v3-'+digest+'.json')
            history.parent.mkdir(parents=True,exist_ok=True)
            if history.exists():
                if history.read_bytes()!=raw:raise ValueError('细草稿迁移历史校验失败')
            else:
                fd,tmp=tempfile.mkstemp(prefix='.migration-',dir=history.parent)
                try:
                    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
                    os.replace(tmp,history)
                    directory_fd=os.open(history.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(directory_fd)
                    finally:os.close(directory_fd)
                finally:Path(tmp).unlink(missing_ok=True)
            if history.read_bytes()!=raw or path.read_bytes()!=raw:raise ValueError('迁移期间旧细草稿变化')
            prov=copy.deepcopy(old['provenance'])
            prov.update(policy=POLICY,method='local_resegment_explicit_deliveries_v3',model_analysis=analysis,
                original_policy=PREVIOUS_RELEASE_POLICY,network_requests=0,source_requests=prov.get('requests',[]),requests=[],
                original_elapsed_seconds=prov.get('elapsed_seconds'),elapsed_seconds=0,
                local_resegmentation={'old_proposal_sha256':digest,'history':str(history),'delivery_times_unchanged':True})
            updated={**old,'segments':rows,'provenance':prov,'created_at':time.time(),
                'warning':'本地复用已有投放事件：内部按释放入盘分界，末颗保留至粗终点；未调用 API，仍需人工审核。'}
            atomic_json(path,updated);migrated.append(id_)
        if migrated:atomic_json(ws.root/'fine-migrations'/(POLICY+'.json'),{'ids':migrated,'at':time.time(),'network_requests':0})
    return migrated
