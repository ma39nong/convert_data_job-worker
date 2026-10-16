"""Read-only source/list preflight, including incomplete episode directories."""
from pathlib import Path
import yaml
from .sources.gello_rosbag2 import discover, snapshot, natural_key
from .selection import read_episode_list
from .recipes.multimodal import validate_contract


def select_names(source,episodes=None,episode_list=None):
    source=Path(source).resolve()
    if episodes and episode_list:raise ValueError('Use --episode or --episode-list, not both')
    candidates=([source] if (source/'metadata.yaml').exists() else
                sorted((p for p in source.iterdir() if p.is_dir() and p.name.startswith('episode')),key=natural_key))
    by_name={p.name:p for p in candidates}
    requested=read_episode_list(episode_list)['requested_episodes'] if episode_list else episodes
    names=list(requested) if requested else list(by_name)
    if not names or len(set(names))!=len(names):raise ValueError('Empty or duplicate selection')
    if any(n not in by_name for n in names):raise ValueError('Missing episode: '+', '.join(n for n in names if n not in by_name))
    return [by_name[n] for n in names]



def validate_media_endpoints(bag,streams,robot):
    """Check actual codecs and shapes before any output bag is created."""
    from .sources.robot_rosbag2 import modalities
    sensors={}
    for key,spec in modalities(robot).items():
        if spec['kind']=='wrench':continue
        images=[bag.raw_image(ref,spec['kind']) for ref in (streams[key].values[0],streams[key].values[-1])]
        if images[0].shape!=images[1].shape or any(v<=0 for v in images[0].shape):raise ValueError(f'Sensor shape changed: {key}')
        sensors[key]={'shape':list(images[0].shape),'dtype':spec['dtype']}
    rgb={}
    for key in ('cam0','cam1','cam2'):
        first=bag.image(streams[key].values[0]);last=bag.image(streams[key].values[-1])
        if first.shape!=last.shape or any(v<=0 or v%2 for v in first.shape[:2]):
            raise ValueError(f'Invalid H264 camera shape: {key}')
        rgb[key]=list(first.shape)
    return rgb,sensors

def inspect_sources(config,episodes=None,episode_list=None):
    from .jobs import check_ready
    paths=select_names(config['source_root'],episodes,episode_list)
    contract=yaml.safe_load(Path(config['contract']).read_text());validate_contract(contract,config)
    results=[]
    for path in paths:
        try:
            before=snapshot(path)
            profile={}
            ready=check_ready(path,contract,config['schema_dir'],profile)
            item=discover(path)[0]
            if not item['eligible']:raise ValueError(item['reason'])
            rgb,sensors,profiles=profile['rgb_shapes'],profile['sensor_shapes'],profile['streams']
            if snapshot(path)!=before:raise ValueError('Source changed during preflight')
            results.append({'source':str(path),'status':'ready','robot':config['robot'],
                            'readiness':ready,'validated_start_ns':item['source_start_ns'],
                            'validated_end_ns':item['source_end_ns'],'streams':profiles,
                            'rgb_shapes':rgb,'sensor_shapes':sensors})
            print(f'{path.name}: ready',flush=True)
        except Exception as exc:
            results.append({'source':str(path),'status':'not_ready','error':f'{type(exc).__name__}: {exc}'})
            print(f'{path.name}: not ready: {exc}',flush=True)
    return {'status':'passed' if all(r['status']=='ready' for r in results) else 'failed',
            'robot':config['robot'],'selected':len(results),'ready':sum(r['status']=='ready' for r in results),
            'not_ready':sum(r['status']!='ready' for r in results),'sources':results,
            'limits':'Sensor encoding/shape checked at endpoints; selected output payloads and timing continuity are checked during conversion/verification; unselected image payloads are not exhaustively decoded. No source loss rate inferred.'}
