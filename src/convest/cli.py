"""Conversion and local job entry point."""
import argparse
from pathlib import Path
import sys
from .config import load_config


def main(argv=None):
    parser=argparse.ArgumentParser(description='Trim and convert robot bags to multimodal LeRobot v2.1')
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('run','convert'):
        p=sub.add_parser(command);p.add_argument('--config',type=Path,required=True)
        p.add_argument('--resume',action='store_true');p.add_argument('--workers',type=int,choices=range(1,5),default=1)
        if command=='run':
            p.add_argument('--run-root',type=Path,required=True);p.add_argument('--episode',action='append')
        else:
            p.add_argument('--source-root',type=Path);p.add_argument('--output',type=Path)
            p.add_argument('--episode-list',type=Path)
    p=sub.add_parser('verify');p.add_argument('dataset',type=Path);p.add_argument('--compare-source',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='verify':
            from .verify import verify
            verify(args.dataset,full_video=True,compare_source=args.compare_source);return 0
        config=load_config(args.config)
        if args.command=='run':
            from .jobs import run
            return run(config,args.run_root,args.episode,args.resume,args.workers)
        from .pipeline import convert
        if args.source_root:config['source_root']=str(args.source_root.resolve())
        if args.output:config['output_root']=str(args.output.resolve())
        return convert(config,resume=args.resume,workers=args.workers,episode_list=args.episode_list)
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
