"""Single original-main-video coarse proposal; never a reviewed annotation."""
import base64
import re
import copy
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path

from .core import atomic_json, sha
from .doubao import NoRedirect

MODEL='qwen3.8-flash'
ENDPOINT='https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1/chat/completions'
KEY_PATH=Path.home()/'.config/qwen-recognition-test/token-plan.key'
POLICY='qwen-main-event-boundaries-single-pass-v3'

def configured():
    try:return bool(KEY_PATH.read_text().strip())
    except OSError:return False

def number(value):
    return type(value) in (int,float) and math.isfinite(value)

def parse(doc,ep):
    if doc.get('sequence_supported') is not True:
        raise ValueError('Qwen 未确认五阶段流程，未生成标注；请人工检查视频')
    rows=doc.get('segments');labels=['pickup','harvest','adjust','harvest','return']
    if not isinstance(rows,list) or len(rows)!=5:raise ValueError('Qwen 须返回五个粗阶段')
    duration=ep.length/ep.fps;previous=0;frames=0;result=[]
    for i,(row,label) in enumerate(zip(rows,labels)):
        a,b=row.get('start_seconds'),row.get('end_seconds')
        if row.get('phase_index')!=i+1 or row.get('label')!=label:raise ValueError('Qwen 阶段顺序或标签错误')
        if not number(a) or not number(b):raise ValueError('Qwen 边界尚不能确定，未强行补齐；请人工检查')
        if abs(a-previous)>1e-6 or not 0<=a<b<=duration+1e-6:
            raise ValueError('Qwen 阶段时间未连续覆盖视频，未生成草稿')
        end=round(b*ep.fps)
        if end<=frames:raise ValueError('Qwen 时间转换后产生空段，未生成草稿')
        result.append({'start_frame':frames,'end_frame':end,'label':label,'reviewed':False})
        previous=b;frames=end
    if abs(previous-duration)>1e-6 or frames!=ep.length:
        raise ValueError('Qwen 未覆盖准确视频时长，未自动延长最后一段')
    handoffs=doc.get('handoffs')
    if not isinstance(handoffs,list) or len(handoffs)!=4:raise ValueError('Qwen 缺少四处衔接说明')
    for i,h in enumerate(handoffs):
        if (h.get('from_phase'),h.get('to_phase'))!=(i+1,i+2) or not number(h.get('seconds')) or abs(h['seconds']-rows[i]['end_seconds'])>1e-6:
            raise ValueError('Qwen 衔接时间与阶段不一致')
        if type(h.get('needs_review')) is not bool or not all(isinstance(h.get(k),str) for k in ('observation','reason')):
            raise ValueError('Qwen 衔接疑点格式错误')
    summary=doc.get('harvest_summary',{})
    for name in ('first_harvest','second_harvest'):
        block=summary.get(name,{})
        count=block.get('successful_count');times=block.get('delivery_seconds')
        if count is not None and (type(count) is not int or count<0):raise ValueError('Qwen 投放数量无效')
        if not isinstance(times,list) or any(not number(t) or not 0<=t<=duration for t in times):raise ValueError('Qwen 投放时间无效')
        if times!=sorted(set(times)) or (count is not None and count!=len(times)):raise ValueError('Qwen 投放数量与事件列表不一致')
    if not isinstance(doc.get('review_notes'),list) or any(not isinstance(n,str) for n in doc['review_notes']):raise ValueError('Qwen 疑点列表格式错误')
    return result

def decode_response(content,ep):
    # Accept a single fenced JSON object with a prose preface; never guess cuts.
    blocks=re.findall(r'```(?:json)?\s*(.*?)```',content,flags=re.S)
    if len(blocks)>1:raise ValueError('Qwen 返回多个 JSON 代码块，需人工检查')
    corrections=[]
    try:doc=json.loads(blocks[0] if blocks else content)
    except (TypeError,ValueError):
        # Recover complete mandatory values only. Never repair guessed labels,
        # missing cuts or truncated arrays; optional commentary cannot veto cuts.
        try:
            if not content.lstrip().startswith('{'):raise ValueError('not an object')
            decoder=json.JSONDecoder();doc={}
            for key in ('sequence_supported','segments','handoffs'):
                matches=list(re.finditer('"'+key+'"'+r'\s*:',content))
                if len(matches)!=1:raise ValueError('ambiguous mandatory field')
                doc[key]=decoder.raw_decode(content[matches[0].end():].lstrip())[0]
            doc['harvest_summary']={k:{'successful_count':None,'delivery_seconds':[]} for k in ('first_harvest','second_harvest')}
            summary_matches=list(re.finditer(r'"harvest_summary"\s*:',content))
            if len(summary_matches)==1:
                try:
                    summary=decoder.raw_decode(content[summary_matches[0].end():].lstrip())[0]
                    if isinstance(summary,dict):doc['harvest_summary']=summary
                except ValueError:pass
            doc['review_notes']=['原始辅助说明格式损坏；保留可解析的投放时间，数量与时间列表矛盾时以有效时间列表为准；粗边界仍需人工复核。']
            for h in doc['handoffs']:
                h['needs_review']=True;h['reason']='原始辅助输出损坏或矛盾，需要人工检查实际交接证据'
            corrections.append({'field':'auxiliary_output','reason':'recovered complete explicit mandatory fields; discarded malformed commentary; retained complete delivery-time fields; no inferred cuts'})
        except (TypeError,ValueError,KeyError):
            raise ValueError('Qwen 未返回可恢复的阶段 JSON，已保留原始响应') from None
    if not isinstance(doc,dict):raise ValueError('Qwen 返回格式不是阶段标注对象，未根据描述猜测标签')
    doc=copy.deepcopy(doc)
    rows=doc.get('segments',[]);duration=ep.length/ep.fps
    # User rule: explicit valid delivery times take priority over a conflicting
    # integer count. Null still means an incomplete/uncertain tally, not zero.
    if isinstance(rows,list) and len(rows)==5 and isinstance(doc.get('harvest_summary'),dict):
        for key,index in (('first_harvest',1),('second_harvest',3)):
            block=doc['harvest_summary'].get(key)
            if not isinstance(block,dict):continue
            count=block.get('successful_count');times=block.get('delivery_seconds')
            interval=rows[index];a=interval.get('start_seconds');b=interval.get('end_seconds')
            if (type(count) is int and count>=0 and isinstance(times,list) and times
                and number(a) and number(b) and all(number(t) and a<=t<=b for t in times)
                and times==sorted(set(times)) and count!=len(times)):
                block['successful_count']=len(times)
                corrections.append({'field':'harvest_summary.'+key+'.successful_count','original':count,
                    'normalized':len(times),'reason':'user rule: valid delivery_seconds list takes priority; times unchanged'})
    if len(rows)==5:
        end=rows[-1].get('end_seconds')
        # Video end is known independently. Only normalize <= one source frame
        # of decimal rounding; internal action boundaries remain untouched.
        if number(end) and 1e-6<abs(end-duration)<=1/ep.fps+1e-6:
            rows[-1]['end_seconds']=duration
            corrections.append({'field':'segments[4].end_seconds','original':end,'normalized':duration,'reason':'known video end within one-frame decimal rounding'})
    return doc,corrections


def annotate(ep,output,*,retry_namespace=None,allow_network=True):
    from .workflow import in_hand_episode
    if not in_hand_episode(ep):raise ValueError('Qwen 当前仅支持无放回摘一串的粗阶段')
    identity=ep.identity();video=ep.cameras.get('observation.images.cam0')
    if video is None:raise ValueError('Qwen 需要原始主视频 cam0')
    prompt=Path(__file__).with_name('qwen_prompt.txt').read_text().replace('{duration_seconds}',format(ep.length/ep.fps,'.12g'))
    if retry_namespace is not None:
        prompt+='\n本次为失败项纠错：严格只输出一个符合上方结构的JSON对象，首字符为{，末字符为}。不要输出分析文字、描述列表或代码围栏。五段必须都有phase_index和label，四处边界复用同一数值。视频总时长不是动作边界估计，最后end_seconds直接使用给定时长。无法确认流程请输出sequence_supported=false并说明，不能虚构动作。'
    raw,path,digest,cached,elapsed,identity=video_response(ep,output,prompt,POLICY,retry_namespace=retry_namespace,allow_network=allow_network)
    doc,normalizations=decode_response(raw['choices'][0]['message'].get('content',''),ep)
    rows=parse(doc,ep)
    if ep.identity()!=identity:raise ValueError('Qwen 分析期间来源变化，未生成标注')
    if not cached:atomic_json(path,raw)
    return {'method':'qwen_main_event_boundaries_single_pass_v3','provider':'qwen','model':MODEL,'policy':POLICY,
            'coarse_segments':rows,'warning':'Qwen 原始主视频单次粗阶段草稿，尚未经逐帧复核；请人工检查边界及投放计数。',
            'boundary_audit':{'scope':'single_pass_main_video','all_boundaries_checked':False},
            'unresolved_boundaries':[i for i,h in enumerate(doc['handoffs']) if h['needs_review']],
            'model_analysis':doc,'local_normalizations':normalizations,'source':identity,'elapsed_seconds':elapsed,'cached':cached,
            'requests':[{'key':digest,'cached':cached,'usage':raw.get('usage',{})}],
            'network_requests':int(not cached)}

def video_response(ep,output,prompt,policy,*,retry_namespace=None,allow_network=True,media_path=None,media_manifest=None):
    """One paid request per source/prompt identity; retain raw output before parsing."""
    identity=ep.identity();video=Path(media_path) if media_path is not None else ep.cameras.get('observation.images.cam0')
    if video is None:raise ValueError('Qwen 需要原始主视频 cam0')
    ep.validate_media(Path(output),identity)
    if video.stat().st_size>45*1024*1024:raise ValueError('Qwen 原始主视频超过45MiB本机请求限制')
    request_id={'provider':'qwen','model':MODEL,'endpoint':ENDPOINT,'policy':policy,'source':identity,
                'video_sha256':sha(video),'prompt':prompt,'enable_thinking':False,'max_tokens':6000,
                'sampling_fps_parameter':None,'retry_namespace':retry_namespace}
    if media_path is not None:
        if not isinstance(media_manifest,dict) or media_manifest.get('sha256')!=request_id['video_sha256']:
            raise ValueError('多视角输入清单与视频不一致，未调用API')
        request_id['input_media']=media_manifest
    digest=hashlib.sha256(json.dumps(request_id,sort_keys=True).encode()).hexdigest()
    root=Path(output)/'qwen';path=root/'responses'/(digest+'.json')
    raw_path=root/'raw'/(digest+'.json')
    cached=path.exists() or raw_path.exists();elapsed=0
    if cached:raw=json.loads((path if path.exists() else raw_path).read_text())
    else:
        if not allow_network:raise ValueError('本地恢复未找到同源原始响应，已阻止 API 调用')
        if not configured():raise ValueError('Qwen 密钥未配置，请检查本机安全密钥文件')
        key=KEY_PATH.read_text().strip()
        content=[{'type':'video_url','video_url':{'url':'data:video/mp4;base64,'+base64.b64encode(video.read_bytes()).decode()}},
                 {'type':'text','text':prompt}]
        payload={'model':MODEL,'messages':[{'role':'user','content':content}],
                 'enable_thinking':False,'max_tokens':6000,'stream':False}
        atomic_json(root/'requests'/(digest+'.json'),request_id)
        request=urllib.request.Request(ENDPOINT,data=json.dumps(payload).encode(),headers={
            'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        start=time.monotonic()
        try:
            with urllib.request.build_opener(NoRedirect).open(request,timeout=420) as r:raw=json.loads(r.read(16*1024*1024))
        except urllib.error.HTTPError as exc:
            raise ValueError(f'Qwen HTTP {exc.code}，未生成标注；请检查密钥、额度或接口状态') from None
        except Exception:raise ValueError('Qwen 连接或响应失败，未生成标注') from None
        elapsed=time.monotonic()-start
        atomic_json(root/'raw'/(digest+'.json'),raw)
    if not raw.get('choices') or raw['choices'][0].get('finish_reason')!='stop':raise ValueError('Qwen 输出未完成，未生成标注')
    return raw,path,digest,cached,elapsed,identity
