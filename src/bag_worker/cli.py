import argparse
import json
import sys
import sqlite3
import struct
from pathlib import Path

from .bag import Bag
from .idle import IdleConfig, inspect_bag, trim_bag, default_config


def main(argv=None):
    parser = argparse.ArgumentParser(description='ROS2 双臂静止段自动裁剪（源数据只读）')
    parser.add_argument('command', choices=['inspect', 'trim', 'batch'])
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, help='trim 的新 bag 目录，或 batch 的新输出根目录')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--message-types', type=Path, help='对应录制版本的自定义 .msg/.idl 根目录')
    parser.add_argument('--report', type=Path, help='inspect 的 JSON 报告（拒绝覆盖）')
    args = parser.parse_args(argv)
    try:
        import yaml
        settings = yaml.safe_load(args.config.read_text()) if args.config else {}
        config = IdleConfig(**(settings or {})) if args.config else default_config()
        if args.command == 'inspect':
            report = inspect_bag(Bag(args.source, args.message_types), config)
            if args.report:
                # Reports must never overwrite source metadata or existing files.
                args.report.parent.mkdir(parents=True, exist_ok=True)
                with args.report.open('x', encoding='utf-8') as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.output is None:
            parser.error('trim / batch 必须提供 --output')
        if args.command == 'trim':
            report = trim_bag(args.source, args.output, config, args.message_types)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        source = args.source.resolve()
        output = args.output.resolve()
        if not source.is_dir() or output == source or output.is_relative_to(source):
            raise ValueError('batch 输入必须为目录，输出必须位于输入目录外部')
        bags = sorted(p.parent for p in source.rglob('metadata.yaml'))
        if not bags:
            raise ValueError('没有发现 metadata.yaml')
        output.mkdir(parents=True, exist_ok=False)
        results = []
        for bag in bags:
            target = output / bag.relative_to(source)
            # A root that itself is a bag needs a child output directory.
            if target == output:
                target = output / bag.name
            try:
                report = trim_bag(bag, target, config, args.message_types)
                results.append({'source': str(bag), 'output': str(target), 'status': 'completed',
                                'deleted_seconds': report['deleted_seconds']})
            except (ValueError, OSError, RuntimeError, sqlite3.Error, struct.error) as exc:
                results.append({'source': str(bag), 'status': 'failed', 'error': str(exc)})
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
            # Update after every bag so an interrupted batch retains its history.
            pending = output / 'batch_report.json.tmp'
            pending.write_text(json.dumps(results, ensure_ascii=False, indent=2))
            pending.replace(output/'batch_report.json')
        return 2 if any(r['status'] == 'failed' for r in results) else 0
    except (ValueError, TypeError, OSError, RuntimeError, sqlite3.Error, struct.error) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
