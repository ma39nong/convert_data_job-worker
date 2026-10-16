"""Conversion, preflight and resumable local job commands."""
import argparse
import json
from pathlib import Path
import sys
from .config import load_config, WORKSPACE


def main(argv=None):
    parser=argparse.ArgumentParser(description='Clean and convert ROS2 bags to multimodal LeRobot v2.1')
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('run','convert','inspect','check-list'):
        p=sub.add_parser(command);p.add_argument('--config',type=Path,required=True)
        if command=='check-list':p.add_argument('episode_list',type=Path)
        if command in ('run','convert'):
            p.add_argument('--resume',action='store_true');p.add_argument('--workers',type=int,choices=range(1,5),default=1)
        if command in ('run','inspect'):
            selection=p.add_mutually_exclusive_group()
            selection.add_argument('--episode',action='append');selection.add_argument('--episode-list',type=Path)
        if command=='run':p.add_argument('--run-root',type=Path,required=True)
        if command=='convert':
            p.add_argument('--output',type=Path);p.add_argument('--episode-list',type=Path)
        if command!='run':p.add_argument('--source-root',type=Path)
        if command in ('inspect','check-list'):p.add_argument('--report',type=Path)
    p=sub.add_parser('verify');p.add_argument('dataset',type=Path);p.add_argument('--compare-source',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='verify':
            from .verify import verify
            verify(args.dataset,full_video=True,compare_source=args.compare_source);return 0
        config=load_config(args.config)
        if getattr(args,'source_root',None):config['source_root']=str(args.source_root.resolve())
        if args.command in ('inspect','check-list'):
            from .preflight import inspect_sources
            target=args.report.resolve() if args.report else None
            if target and (not target.is_relative_to(WORKSPACE/'outputs') or target.exists() or target.is_relative_to(Path(config['source_root']).resolve())):
                raise ValueError('Preflight report must be a new file under workspace outputs/')
            result=inspect_sources(config,getattr(args,'episode',None),getattr(args,'episode_list',None))
            if target:
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('x') as stream:json.dump(result,stream,ensure_ascii=False,indent=2,allow_nan=False)
            print(json.dumps(result,ensure_ascii=False));return 0 if result['status']=='passed' else 2
        if args.command=='run':
            from .jobs import run
            return run(config,args.run_root,args.episode,args.resume,args.workers,args.episode_list)
        from .pipeline import convert
        if args.output:config['output_root']=str(args.output.resolve())
        return convert(config,resume=args.resume,workers=args.workers,episode_list=args.episode_list)
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
