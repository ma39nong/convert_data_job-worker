"""Inventory/cache, local pre-annotation, external prediction import and mapping."""
import argparse
import json
from pathlib import Path

from .core import Store, project
from .learning import predict, train
from .remote import fetch, query
from .behavior import merge_candidates


def main(argv=None):
    p = argparse.ArgumentParser(description='LeRobot 阶段标注数据工具，源数据只读')
    subs = p.add_subparsers(dest='command', required=True)
    inv = subs.add_parser('inventory');inv.add_argument('--host', default='28');inv.add_argument('--dataset', required=True)
    cache = subs.add_parser('fetch');cache.add_argument('--host', default='28');cache.add_argument('--dataset', required=True)
    cache.add_argument('--episode', type=int, action='append', required=True);cache.add_argument('--destination', required=True)
    for name in ('list', 'predict', 'train', 'project', 'import', 'jev-plan', 'jev'):
        cmd = subs.add_parser(name);cmd.add_argument('--root', action='append', required=True);cmd.add_argument('--output', required=True)
        if name in ('predict', 'project', 'import', 'jev-plan', 'jev'):
            cmd.add_argument('--id', required=True);cmd.add_argument('--replace', action='store_true')
        if name in ('jev', 'jev-plan'):cmd.add_argument('--model', default='jev-1.14.0')
        if name == 'jev':cmd.add_argument('--max-requests', type=int, default=256);cmd.add_argument('--allow-remote-images', action='store_true')
        if name == 'project':cmd.add_argument('--target-id', required=True)
        if name == 'train':cmd.add_argument('--allow-draft-pilot', action='store_true', help='仅试验：允许未审核参考，模型会明确标记')
        if name == 'import':cmd.add_argument('--file', required=True, help='含完整 source 身份和 segments 的标准 JSON，不直接调用外部模型')
    args = p.parse_args(argv)
    if args.command == 'inventory': result = query(args.host, args.dataset)
    elif args.command == 'fetch': result = fetch(args.host, args.dataset, args.episode, args.destination)
    else:
        store = Store(args.root, args.output)
        try:
            if args.command == 'list':result = list(store.entries.values())
            elif args.command == 'train':result = train(store, allow_draft=args.allow_draft_pilot)
            else:
                ep = store.episode(args.id);current = store.load(ep)
                target = store.episode(args.target_id) if args.command == 'project' else ep
                existing = store.load(target)
                if args.command != 'jev-plan' and any(s['label'] != 'unknown' for s in existing['segments']) and not args.replace:
                    raise ValueError('目标已有标注；要替换请显式 --replace，历史仍保留')
                if args.command == 'jev-plan':
                    from .jev import plan
                    result = plan(ep, store.output, args.model)
                elif args.command == 'jev':
                    if not args.allow_remote_images:raise ValueError('需显式 --allow-remote-images 才会将所选 episode 抽帧发送 TypeSafe')
                    from .jev import annotate
                    proposal = annotate(ep, store.output, model=args.model, max_requests=args.max_requests)
                    result = store.save(ep, {**current, 'segments':proposal['segments']}, origin=proposal['method'],
                                        provenance={k:v for k,v in proposal.items() if k!='segments'})
                elif args.command == 'predict':
                    proposal = predict(store, ep)
                    if proposal.get('in_reference_set'):raise ValueError('当前是参考样例，请在其他 episode 上运行预标注')
                    result = store.save(ep, {**current, 'segments': proposal['segments']}, origin=proposal['method'],
                                        provenance={k:v for k,v in proposal.items() if k!='segments'})
                elif args.command == 'project':
                    proposal = project(ep, target, current)
                    doc={**existing,'segments':proposal['segments'],'outcome':proposal['outcome']}
                    if 'review_events' in proposal:
                        doc['review_events']=merge_candidates(existing.get('review_events',[]),proposal['review_events'],target.length)
                    result = store.save(target,doc,origin='normal_projection',
                                        provenance={k:v for k,v in proposal.items() if k not in {'segments','source'}})
                else:
                    proposal = json.loads(Path(args.file).read_text())
                    if proposal['source'] != current['source']:raise ValueError('导入标签的来源指纹不匹配')
                    # External output is always unreviewed; importing cannot manufacture human approval.
                    segments = [{**s, 'reviewed': False} for s in proposal['segments']]
                    result = store.save(ep, {**current, 'segments': segments}, origin='external_prediction_import',
                                        provenance={'provider': str(proposal.get('provider', 'unspecified'))})
        finally:store.lockfile.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__': main()
