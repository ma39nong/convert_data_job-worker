"""Directory import, sequential processing and independent coarse/fine saves.

Saved stage documents never rewrite source data or historical annotation heads.
They are the review workspace's explicit, versioned user results.
"""
import hashlib
import json
from pathlib import Path
import threading
import time

from .core import atomic_json, validate_segments

COARSE_LABELS={'unknown':'待标注','pickup':'拿起番茄','harvest':'摘取番茄','return':'放回番茄','adjust':'调整番茄'}
COARSE_FROM_FINE={'grasp_truss':'pickup','lift_truss':'pickup','grasp_fruit':'harvest','pick_and_place':'harvest',
    'return_truss':'return','return_stem':'return','rotate_stem':'adjust'}


def coarse_segments(segments):
    result=[]
    for s in segments:
        label=COARSE_FROM_FINE.get(s['label'],'unknown')
        if result and result[-1]['label']==label:
            result[-1]['end_frame']=s['end_frame'];result[-1]['reviewed'] &= s.get('reviewed',False)
        else:result.append({'start_frame':s['start_frame'],'end_frame':s['end_frame'],'label':label,'reviewed':s.get('reviewed',False)})
    return result


def validate_coarse(rows,length,fps):
    if not isinstance(rows,list) or not rows or len(rows)>length:raise ValueError('粗阶段必须覆盖整个片段')
    out=[];end=0
    for s in rows:
        a,b,label=s.get('start_frame'),s.get('end_frame'),s.get('label');reviewed=s.get('reviewed',False)
        if type(a) is not int or type(b) is not int or a!=end or not a<b<=length:raise ValueError('粗阶段须连续、无重叠、无空段')
        if label not in COARSE_LABELS or type(reviewed) is not bool or (label=='unknown' and reviewed):raise ValueError('粗阶段标签或确认状态无效')
        out.append({'start_frame':a,'end_frame':b,'start_seconds':a/fps,'end_seconds':b/fps,'label':label,'text':COARSE_LABELS[label],'reviewed':reviewed});end=b
    if end!=length:raise ValueError('粗阶段须覆盖片段结尾')
    return out


class WaitingForNormal(ValueError):
    """A dependency is not a failed video/model request."""


class Workspace:
    def __init__(self,store):
        self.store=store;self.root=store.output/'workspace';self.lock=threading.RLock()
        self.cache_root=store.output.parent/(store.output.name+'-imports')
        if self.cache_root.is_dir() and self.cache_root not in store.roots:
            store.roots.append(self.cache_root.resolve());store.scan()
        self.root.mkdir(exist_ok=True);self.queue_path=self.root/'queue.json'
        self.queue=json.loads(self.queue_path.read_text()) if self.queue_path.exists() else {}
        for item in self.queue.values():
            if item['state']=='running':item.update(state='interrupted',error='上次处理中断，可重新开始')
        self._checkpoint()
        self.organizing_ids=set()
        from .organization import recover
        recover(self)

    def _checkpoint(self):atomic_json(self.queue_path,self.queue)

    def stage_path(self,id_,stage):
        if id_ not in self.store.entries or stage not in {'coarse','fine'}:raise ValueError('未知片段或标注阶段')
        return self.root/'stages'/id_/f'{stage}.json'

    def stages(self,ep,doc=None):
        doc=doc or self.store.load(ep);out={}
        for stage in ('coarse','fine'):
            path=self.stage_path(ep.id,stage)
            if path.exists():
                value=json.loads(path.read_text())
                if value['source']!=doc['source']:raise ValueError('保存阶段的来源已变化，不能复用旧标注')
            else:
                inherited=stage=='fine' and doc['status']=='reviewed'
                value={'schema_version':2,'stage':stage,'revision':0,'source':doc['source'],
                    'saved_at':self.store.path(ep.id).stat().st_mtime if inherited else None,'note':'',
                    'segments':coarse_segments(doc['segments']) if stage=='coarse' else validate_segments(doc['segments'],ep.length,ep.fps),
                    'base_annotation_revision':doc['revision'],'inherited':inherited}
                if inherited:
                    for key in ('review_events','training_review','outcome'):
                        if key in doc:value[key]=doc[key]
            if stage=='coarse' and not path.exists():
                proposal_path=self.root/'coarse-proposals'/(ep.id+'.json')
                if proposal_path.exists():
                    proposal=json.loads(proposal_path.read_text())
                    if proposal['source']==doc['source'] and proposal['base_annotation_revision']==doc['revision']:
                        value={**value,'segments':proposal['segments'],'processing_mode':'coarse','processing_warning':proposal['warning'],'boundary_audit':proposal.get('provenance',{}).get('boundary_audit'),'unresolved_boundaries':proposal.get('provenance',{}).get('unresolved_boundaries',[])}
                        if proposal.get('provenance',{}).get('method') in {'doubao_coarse_overview_v1','doubao_coarse_source_frames_v2'}:
                            value['processing_warning']='旧版粗处理草稿已停用，保留作对照；等待完整分析重做，请勿将它视为新结果。'
            out[stage]=value
        return out

    def save_stage(self,ep,stage,body,*,projection=None):
        with self.lock,self.store.lock:
            from .organization import registry
            if ep.id in self.organizing_ids:raise ValueError('本包正在整理，请完成后再保存')
            if ep.id in registry(self)['retired']:raise ValueError('片段已重新编号，请重新载入后保存')
            doc=self.store.load(ep);current=self.stages(ep,doc)[stage]
            if type(body.get('revision')) is not int or body['revision']!=current['revision']:raise ValueError('这一阶段已在其他窗口更新，请重新载入')
            if body.get('source')!=doc['source']:raise ValueError('标注来源不一致')
            if body.get('base_annotation_revision')!=current['base_annotation_revision']:raise ValueError('草稿已更新，请重新载入后保存')
            note=body.get('note','')
            if not isinstance(note,str) or len(note)>10000:raise ValueError('备注最长10000字')
            if stage=='fine':
                from .actions import ACTION_LABELS
                rows=validate_segments(body.get('segments'),ep.length,ep.fps)
                if any(s['label'] not in ACTION_LABELS for s in rows):raise ValueError('仅保存主要细动作')
            else:rows=validate_coarse(body.get('segments'),ep.length,ep.fps)
            saved={**current,'revision':current['revision']+1,'segments':rows,'note':note,'saved_at':time.time(),'inherited':False}
            if projection is not None:saved['projection']=projection
            if stage=='fine':
                for key in ('review_events','training_review','outcome'):
                    if key in doc:saved[key]=doc[key]
            atomic_json(self.root/'history'/ep.id/stage/f"{saved['revision']:06d}.json",saved)
            atomic_json(self.stage_path(ep.id,stage),saved)
            return saved

    def normal_dependency(self,id_,stage='fine'):
        from .organization import registry
        retired=registry(self)['retired']
        entry=self.store.entries[id_]
        if entry['variant']!='speedup':return None
        keys=('source_recording_id','source_start_ns','source_end_ns')
        matches=[e for e in self.store.entries.values() if e['id'] not in retired and e['variant']=='normal' and all(entry.get(k) is not None and entry[k]==e.get(k) for k in keys)]
        if len(matches)>1:raise ValueError('同源normal不唯一，请核对数据包')
        if not matches:return {'id':None,'ready':False,'reason':'等待导入同源normal'}
        normal=matches[0];path=self.stage_path(normal['id'],stage)
        if not path.exists():path=self.store.path(normal['id'])
        doc=json.loads(path.read_text()) if path.exists() else None
        ready=bool(doc and doc['segments'] and all(s['label']!='unknown' for s in doc['segments']))
        if stage=='coarse' and not ready:
            proposal_path=self.root/'coarse-proposals'/(normal['id']+'.json')
            if proposal_path.exists():
                p=json.loads(proposal_path.read_text())
                ready=bool(doc and p['source']==doc['source'] and p['base_annotation_revision']==doc['revision'] and
                    p.get('provenance',{}).get('method') not in {'doubao_coarse_overview_v1','doubao_coarse_source_frames_v2'} and
                    all(r['label']!='unknown' for r in p['segments']))
        return {'id':normal['id'],'ready':ready,'reason':'' if ready else '等待同源normal处理完成，再处理speedup'}

    def catalog(self):
        with self.lock,self.store.lock:
            from .quarantine import read
            exclusions=read(self)['entries']
            from .organization import registry
            organized=registry(self)
            groups={};episodes=[]
            for entry in self.store.entries.values():
                if entry['id'] in organized['retired']:continue
                override=organized['entries'].get(entry['id'],{})
                receipt=Path(entry['dataset'])/'cache-source.json'
                remote=json.loads(receipt.read_text()) if receipt.exists() else {}
                directory=remote.get('manifest',{}).get('dataset',entry['dataset']);host=remote.get('host','local')
                group_directory=override.get('directory_override',directory)
                group_id=hashlib.sha256((host+':'+group_directory).encode()).hexdigest()[:16]
                doc_path=self.store.path(entry['id']);doc=json.loads(doc_path.read_text()) if doc_path.exists() else None
                ready=bool(doc and all(s['label']!='unknown' for s in doc['segments']))
                fine_path=self.stage_path(entry['id'],'fine')
                explicit_fine=json.loads(fine_path.read_text()) if fine_path.exists() else None
                reviewed=all(s['reviewed'] for s in explicit_fine['segments']) if explicit_fine else bool(doc and doc['status']=='reviewed')
                saved={s:self.stage_path(entry['id'],s).exists() for s in ('coarse','fine')}
                saved['fine'] |= bool(doc and doc['status']=='reviewed')
                proposal_path=self.root/'coarse-proposals'/(entry['id']+'.json')
                proposal=json.loads(proposal_path.read_text()) if proposal_path.exists() else None
                rough_ready=bool(proposal and proposal.get('provenance',{}).get('method') not in {'doubao_coarse_overview_v1','doubao_coarse_source_frames_v2'} and ((doc and proposal['source']==doc['source'] and proposal['base_annotation_revision']==doc['revision']) or (not doc and proposal['base_annotation_revision']==0 and proposal['source']['dataset']==entry['dataset'] and proposal['source']['episode_index']==entry['episode_index'])))
                fine_ready=ready or bool(explicit_fine and all(s['label']!='unknown' for s in explicit_fine['segments']))
                coarse_ready=ready or saved['coarse'] or rough_ready
                q=self.queue.get(entry['id'],{})
                mode=q.get('mode') or ('fine' if fine_ready else 'coarse' if coarse_ready else 'fine')
                legacy_wait=q.get('error')=='请先处理同源normal，再处理speedup'
                states={};dependencies={}
                for stage,available in (('coarse',coarse_ready),('fine',fine_ready or saved['fine'])):
                    status=q.get('state','pending') if mode==stage else 'pending'
                    if available and status not in {'running','failed','interrupted'}:status='ready'
                    elif not available and status=='ready':status='pending'
                    dependencies[stage]=None
                    if entry['variant']=='speedup' and not available and (status in {'pending','queued','interrupted','waiting_normal'} or (legacy_wait and mode==stage)):
                        dependency=dependencies[stage]=self.normal_dependency(entry['id'],stage)
                        if not dependency['ready']:status='waiting_normal'
                        elif status=='waiting_normal' or legacy_wait:status='pending'
                    states[stage]=status
                status=states[mode];dependency=dependencies[mode]
                item={**entry,'directory_id':group_id,'remote_directory':directory,'state':status,
                    'archived':override.get('archived',False),'original_episode_index':override.get('original_episode_index',entry['episode_index']),'coarse_saved':saved['coarse'],'fine_saved':saved['fine'],'error':q.get('error',''),
                    'fine_ready':fine_ready,'coarse_ready':coarse_ready,'coarse_state':states['coarse'],'fine_state':states['fine'],
                    'processing_mode':mode,'human_reviewed':reviewed,'depends_on':dependency['id'] if dependency else None}
                if status=='waiting_normal':item['error']=dependency['reason']
                elif legacy_wait:item['error']=''
                for key in ('source_start_ns','source_end_ns'):
                    if item.get(key) is not None:item[key]=str(item[key])
                exclusion=exclusions.get(entry['id'],{})
                item['excluded']=bool(exclusion.get('excluded'));item['exclusion']=exclusion
                if item['excluded']:status=item['state']=item['coarse_state']=item['fine_state']='excluded'
                episodes.append(item)
                group=groups.setdefault(group_id,{'id':group_id,'path':group_directory,'host':host,'total':0,'ready':0,'pending':0,'running':0,'failed':0,'coarse_saved':0,'fine_saved':0,'excluded':0})
                group['total']+=1
                if status in {'ready','running','failed','excluded'}:group[status]+=1
                else:group['pending']+=1
                for s in saved:group[s+'_saved']+=int(saved[s])
            for item in episodes:
                opposite='speedup' if item['variant']=='normal' else 'normal'
                keys=('source_recording_id','source_start_ns','source_end_ns')
                matches=[other for other in episodes if other['variant']==opposite and all(item.get(k) is not None and item.get(k)==other.get(k) for k in keys)]
                item['pair_ids']=[other['id'] for other in matches]
                item['pair_state']='matched' if len(matches)==1 else 'ambiguous' if matches else 'missing'
            batch_path=self.root/'batch.json'
            return {'redirects':organized['retired'],'directories':list(groups.values()),'episodes':episodes,'batch':json.loads(batch_path.read_text()) if batch_path.exists() else None}

    def import_directory(self,value,selection=None):
        result=self._import_directory(value,selection)
        if 'normal' in Path(result['directory']).parts:
            from .pairing import attach_speedup
            try:pairing=attach_speedup(self,result['directory'])
            except Exception as exc:pairing={'matched':0,'imported':0,'issues':[{'reason':str(exc)}]}
            result['pairing']=pairing
            atomic_json(self.root/'pairing-status'/(hashlib.sha256(result['directory'].encode()).hexdigest()+'.json'),pairing)
        return result

    def _import_directory(self,value,selection=None):
        from .remote import dataset_path,web_inventory,fetch
        path=dataset_path(value);inventory=web_inventory(path);canonical=dataset_path(inventory['dataset'])
        available=[e['episode_index'] for e in inventory['episodes']]
        selected=available if selection is None else selection
        if not isinstance(selected,list) or not selected or any(type(i) is not int or i not in available for i in selected):raise ValueError('请选择目录中实际存在的片段')
        selected=sorted(set(selected));known={(e['remote_directory'],e['episode_index']) for e in self.catalog()['episodes']}
        missing=[i for i in selected if (canonical,i) not in known]
        if missing:
            digest=hashlib.sha256(json.dumps([canonical,missing]).encode()).hexdigest()[:24]
            destination=self.cache_root/digest
            fetch('28',canonical,missing,destination)
            with self.store.lock:
                if self.cache_root.resolve() not in self.store.roots:self.store.roots.append(self.cache_root.resolve())
                self.store.scan()
        matches=[e for e in self.catalog()['episodes'] if e['remote_directory']==canonical and e['episode_index'] in selected]
        if len({e['episode_index'] for e in matches})!=len(selected):raise ValueError('缓存片段未完整注册')
        return {'directory':canonical,'imported':len(missing),'reused':len(selected)-len(missing),'episodes':len(matches)}

    def import_many(self,items):
        if not isinstance(items,list) or not 1<=len(items)<=100:raise ValueError('请选择1–100个数据包')
        results=[]
        for item in items:
            try:results.append({'path':item['path'],**self.import_directory(item['path'],item.get('episodes'))})
            except Exception as exc:results.append({'path':item.get('path',''),'error':str(exc)})
        return {'packages':results,'imported':sum(r.get('imported',0) for r in results),'reused':sum(r.get('reused',0) for r in results),'failed':sum('error' in r for r in results)}

    def map_saved(self,normal_id,speedup_id,stages,*,replace=False):
        from .quarantine import assert_included
        from .core import project
        if not isinstance(stages,list) or not stages or any(s not in {'coarse','fine'} for s in stages):raise ValueError('请选择粗阶段或细阶段')
        with self.lock,self.store.lock:
            assert_included(self,normal_id);assert_included(self,speedup_id)
            normal=self.store.episode(normal_id);speedup=self.store.episode(speedup_id)
            if normal.entry.get('source_recording_id')!=speedup.entry.get('source_recording_id'):raise ValueError('不是同一录制')
            if self.store.entries[normal_id]['variant']!='normal' or self.store.entries[speedup_id]['variant']!='speedup':raise ValueError('映射方向必须normal到speedup')
            sources=self.stages(normal);targets=self.stages(speedup);proposals=[]
            coarse_labels={'pickup':'grasp_truss','harvest':'grasp_fruit','return':'return_truss','adjust':'rotate_stem','unknown':'unknown'}
            for stage in dict.fromkeys(stages):
                source=sources[stage];target=targets[stage]
                if not source['saved_at']:raise ValueError('请先保存normal的'+stage+'阶段')
                if target['saved_at'] and not replace:raise ValueError('speedup这一层已有保存结果，未覆盖；如需更新请勾选替换并保留历史')
                rows=source['segments']
                if stage=='coarse':rows=[{**r,'label':coarse_labels[r['label']]} for r in rows]
                annotation={**self.store.load(normal),'segments':rows,'revision':source['revision'],'status':'reviewed' if all(r['reviewed'] for r in rows) else 'draft'}
                mapped=project(normal,speedup,annotation)
                rows=mapped['segments'] if stage=='fine' else coarse_segments(mapped['segments'])
                projection={k:v for k,v in mapped.items() if k not in {'segments','source'}}
                projection.update(normal_id=normal_id,normal_stage=stage,normal_stage_revision=source['revision'],normal_stage_inherited=source.get('inherited',False))
                proposals.append((stage,{**target,'segments':rows},projection))
            saved={stage:self.save_stage(speedup,stage,doc,projection=projection) for stage,doc,projection in proposals}
            return {'normal_id':normal_id,'speedup_id':speedup_id,'stages':saved,'warning':'按实际来源帧映射，speedup需看片确认；没有按时间比例缩放。'}

    def _proposal(self,ep,current,server,client):
        from .organization import registry
        retired=registry(self)['retired']
        from .doubao import annotate
        from .core import project
        from .template import POLICY
        from .boundaries import refine_all
        matches=[e for e in self.store.entries.values() if e['id'] not in retired and e['variant']=='normal' and e['source_recording_id'] and e['source_recording_id']==ep.entry.get('source_recording_id')]
        if self.store.entries[ep.id]['variant']!='speedup' or not matches:
            return annotate(ep,self.store.output,api_key=server.doubao_key,model=server.doubao_model,client=client,max_requests=128,progress=server.progress)
        if len(matches)!=1:raise ValueError('存在多个同源normal，请先核对导入目录')
        normal=self.store.episode(matches[0]['id']);normal_doc=self.store.load(normal)
        fine_path=self.stage_path(normal.id,'fine');fine=json.loads(fine_path.read_text()) if fine_path.exists() else None
        if fine:
            if fine['source']!=normal_doc['source']:raise ValueError('normal保存阶段来源变化')
            normal_doc={**normal_doc,'segments':fine['segments'],'outcome':fine.get('outcome',normal_doc['outcome'])}
        if any(s['label']=='unknown' for s in normal_doc['segments']):raise WaitingForNormal('等待同源normal处理完成，再处理speedup')
        mapped=project(normal,ep,normal_doc);records=[]
        def record(response,key,cached,stage):records.append({'key':key,'cached':cached,'stage':stage})
        checked,evidence,_=refine_all(ep,mapped['segments'],self.root/'processing'/ep.id,client,record,records,128,server.progress)
        disagreements=[i for i,(a,b) in enumerate(zip(mapped['segments'][:-1],checked[:-1])) if a['end_frame']!=b['end_frame']]
        projection={k:v for k,v in mapped.items() if k not in {'source','segments'}}
        projection.update(normal_id=normal.id,normal_workspace_revision=fine['revision'] if fine else None)
        return {'method':'doubao_template_source_frames_v1','policy':POLICY,'segments':mapped['segments'],
            'projection':projection,'independent_visual_segments':checked,'boundary_evidence':evidence,'requests':records,
            'mapping_visual_disagreements':disagreements,'unresolved_boundaries':sorted(set(disagreements)|{int(i) for i,v in evidence.items() if v['source_frame']['needs_review']}),
            'warning':'按同源normal映射，speedup已另查连续原帧；分歧保留待核。'}

    def model_client(self,ep,server,retry_namespace=None):
        from .doubao import Client
        from .style import transition_examples
        from .quarantine import read
        from .organization import registry
        excluded=read(self)['entries']
        docs={key:self.store.load(self.store.episode(key)) for key in self.store.entries if not excluded.get(key,{}).get('excluded') and key not in registry(self)['retired']}
        for key in docs:
            saved=self.stage_path(key,'fine')
            if saved.exists():
                fine=json.loads(saved.read_text())
                if fine['source']!=docs[key]['source']:raise ValueError('人工样例来源变化')
                docs[key]={**docs[key],'segments':fine['segments'],'status':'reviewed' if all(s['reviewed'] for s in fine['segments']) else 'draft'}
        client=Client(self.store.output/'doubao',server.doubao_key,server.doubao_model,style_examples=transition_examples(docs,ep))
        if retry_namespace:client.retry_namespace=retry_namespace
        return client

    def process_coarse_episode(self,ep,current,server,retry_namespace=None):
        """Generate an independent unsaved coarse proposal; never fabricate fine labels."""
        before=self.stages(ep,current)
        if before['coarse']['saved_at']:
            return 'preserved'
        fine=before['fine']['segments']
        normal_snapshot=None
        if all(s['label']!='unknown' for s in fine):
            result={'coarse_segments':coarse_segments(fine),'method':'derived_from_existing_fine',
                    'warning':'粗阶段来自已有细阶段，没有重新调用模型。'}
        elif self.store.entries[ep.id]['variant']=='speedup':
            from .core import project
            pair=next(e for e in self.catalog()['episodes'] if e['id']==ep.id)
            if pair['pair_state']!='matched':raise WaitingForNormal('请先导入唯一对应normal并处理粗阶段')
            normal=self.store.episode(pair['pair_ids'][0]);normal_doc=self.store.load(normal)
            normal_stage=self.stages(normal,normal_doc)['coarse']
            if any(r['label']=='unknown' for r in normal_stage['segments']):raise WaitingForNormal('请先处理对应normal粗阶段')
            normal_snapshot=(normal.id,normal_stage)
            labels={'pickup':'grasp_truss','harvest':'grasp_fruit','return':'return_truss','adjust':'rotate_stem'}
            annotation={**normal_doc,'segments':[{**r,'label':labels[r['label']]} for r in normal_stage['segments']]}
            mapped=project(normal,ep,annotation)
            result={'coarse_segments':coarse_segments(mapped['segments']),'method':'source_frame_coarse_projection',
                    'warning':'按normal来源帧映射粗阶段，需人工复核；没有生成细阶段。'}
        else:
            client=self.model_client(ep,server,retry_namespace)
            analysis=self._proposal(ep,current,server,client)
            result={**analysis,'method':'coarse_from_full_analysis_v3',
                    'analysis_method':analysis['method'],'coarse_segments':coarse_segments(analysis['segments']),
                    'warning':'沿用完整细动作分析、人工参考及原帧边界复核后汇总粗阶段；结果仍需人工审核。'}
            from .behavior import scan
            try:
                behavior=scan(ep,self.store.output,api_key=server.doubao_key,model=server.doubao_model)
                result['behavior_review']=behavior
            except Exception as exc:result['behavior_review_error']=str(exc)
            # Fine actions are evidence, not a published fine-stage layer.
            result['internal_fine_segments']=result.pop('segments')
            fine_rows=result['internal_fine_segments']
            cuts=[i for i in range(len(fine_rows)-1) if COARSE_FROM_FINE.get(fine_rows[i]['label'])!=COARSE_FROM_FINE.get(fine_rows[i+1]['label'])]
            internal_evidence=result.pop('boundary_evidence',{})
            result['internal_boundary_evidence']=internal_evidence
            result['boundary_evidence']={str(i):internal_evidence[str(j)] for i,j in enumerate(cuts) if str(j) in internal_evidence}
            unresolved=result.pop('unresolved_boundaries',[])
            result['unresolved_fine_boundaries']=unresolved
            result['unresolved_boundaries']=[i for i,j in enumerate(cuts) if j in unresolved]
            result['analysis_boundary_audit']=result.get('boundary_audit')
            result['boundary_audit']={'scope':'coarse_from_full_analysis','coarse_boundary_to_fine_boundary':cuts,
                'coarse_boundaries':len(cuts),'all_boundaries_checked':bool((result.get('analysis_boundary_audit') or {}).get('all_boundaries_checked'))}
            short=[r for r in result['coarse_segments'] if r['label']=='return' and r['end_frame']-r['start_frame']<ep.fps*.5]
            if short:
                error='完整分析后出现不足0.5秒的可疑放回阶段，需重查，未发布新粗结果'
                atomic_json(self.root/'processing-errors'/ep.id/(str(time.time_ns())+'.json'),{'error':error,'result':result})
                raise ValueError(error)
            if analysis.get('unresolved_boundaries'):
                result['warning']+=f" 内部分析有{len(analysis['unresolved_boundaries'])}个分界仍需复核。"

        with self.lock,self.store.lock:
            fresh=self.store.load(ep)
            if fresh['source']!=current['source'] or fresh['revision']!=current['revision']:raise ValueError('处理期间草稿变化，未覆盖')
            if self.stage_path(ep.id,'coarse').exists():return 'preserved'
            if normal_snapshot:
                normal=self.store.episode(normal_snapshot[0])
                if self.stages(normal)['coarse']!=normal_snapshot[1]:raise ValueError('normal粗阶段在映射期间变化')
            proposal={'source':ep.identity(),'base_annotation_revision':current['revision'],
                      'segments':validate_coarse(result['coarse_segments'],ep.length,ep.fps),
                      'warning':result['warning'],'provenance':result,'created_at':time.time()}
            proposal_path=self.root/'coarse-proposals'/(ep.id+'.json')
            if proposal_path.exists():
                atomic_json(self.root/'coarse-proposal-history'/ep.id/(str(time.time_ns())+'.json'),json.loads(proposal_path.read_text()))
            atomic_json(proposal_path,proposal)
        return 'ready'

    def process(self,ids,server,mode='fine',retry_namespace=None):
        if mode not in {'coarse','fine'}:raise ValueError('请选择粗阶段或完整细阶段处理')
        from .doubao import Client
        from .style import transition_examples
        if not server.doubao_key:raise ValueError('豆包连接不可用，请检查启动凭据')
        results=[]
        ids=sorted(ids,key=lambda id_:self.store.entries[id_]['variant']=='speedup')
        for id_ in ids:
            from .quarantine import read
            with self.lock:
                from .organization import registry
                if id_ in registry(self)['retired'] or id_ in self.organizing_ids or read(self)['entries'].get(id_,{}).get('excluded'):
                    results.append({'id':id_,'state':'excluded'});continue
                self.queue[id_]={'state':'running','mode':mode};self._checkpoint()
            try:
                with self.lock,self.store.lock:
                    ep=self.store.episode(id_);current=self.store.load(ep)
                if mode=='coarse':
                    status=self.process_coarse_episode(ep,current,server,retry_namespace)
                    with self.lock:
                        self.queue[id_]={'state':'ready','mode':'coarse'};self._checkpoint()
                    results.append({'id':id_,'state':status,'mode':'coarse'});continue
                with self.lock,self.store.lock:
                    if self.stage_path(id_,'fine').exists() or current['status']=='reviewed':
                        self.queue[id_]={'state':'ready'};self._checkpoint()
                        results.append({'id':id_,'state':'preserved'});continue
                dependency=self.normal_dependency(id_)
                if dependency and not dependency['ready']:raise WaitingForNormal(dependency['reason'])
                client=self.model_client(ep,server,retry_namespace)
                result=self._proposal(ep,current,server,client)
                from .behavior import scan,merge_candidates
                events=current.get('review_events',[])
                try:
                    behavior=scan(ep,self.store.output,api_key=server.doubao_key,model=server.doubao_model)
                    events=merge_candidates(events,behavior['events'],ep.length)
                    result['behavior_review']={k:v for k,v in behavior.items() if k not in {'events','source'}}
                except Exception as exc:
                    result['behavior_review_error']='异常候选尚未检查完成：'+str(exc)

                with self.lock,self.store.lock:
                    if self.stage_path(id_,'fine').exists():raise ValueError('已保存人工细阶段，未覆盖新修改')
                    projection=result.get('projection')
                    if projection:
                        source=self.store.episode(projection['normal_id']);fresh=self.store.load(source)
                        p=self.stage_path(source.id,'fine');stage=json.loads(p.read_text()) if p.exists() else None
                        if fresh['revision']!=projection['normal_revision'] or (stage['revision'] if stage else None)!=projection['normal_workspace_revision']:
                            raise ValueError('normal标注在处理期间变化，未保存旧映射')
                    self.store.save(ep,{**current,'segments':result['segments'],'review_events':events},origin=result['method'],provenance={k:v for k,v in result.items() if k!='segments'})
                    self.queue[id_]={'state':'ready'};self._checkpoint()
            except WaitingForNormal as exc:
                with self.lock:self.queue[id_]={'state':'waiting_normal','error':str(exc)};self._checkpoint()
            except Exception as exc:
                with self.lock:self.queue[id_]={'state':'failed','error':str(exc)};self._checkpoint()
            results.append({'id':id_,**self.queue[id_]})
        return {'episodes':results}
