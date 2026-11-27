"""Deliver original shared assets before allowing MCAP original deletion."""
import os
from pathlib import Path
import posixpath
from convest.config import digest
from convest.ingest.batches import durable_json
from convest.capture.transfer import manifest,ensure_real_path
from convest.mcap_input import assets,check_assets
from .transport import transfer,verify_backup


def archive_assets(source):
    source=Path(source).resolve();roots={}
    for entry in assets(source):
        target=Path(entry['source'])
        if target.is_relative_to(source):continue  # already part of the episode's exact manifest
        if '_shared' in target.parts:
            target=Path(*target.parts[:target.parts.index('_shared')+1])
        elif not entry['directory']:target=target.parent
        ensure_real_path(target)
        roots[str(target)]={'source':str(target),'relative':os.path.relpath(target,source),
                            'directory':True,'files':manifest(target)}
    return list(roots.values())


def deliver(spec,source,destination,entries=None,verify_only=False):
    entries=archive_assets(source) if entries is None else entries
    if not verify_only:check_assets(entries)
    root=Path(spec['task']['raw_root'])
    # Standard mcap tree is the allowed shared/model namespace. Custom targets
    # may only resolve shared references within their explicitly configured root.
    boundary=next((p for p in [root,*root.parents] if p.name=='mcap'),root)
    for entry in entries:
        target=Path(posixpath.normpath(str(Path(destination)/entry['relative'])))
        if target==boundary or not target.is_relative_to(boundary):raise ValueError('共享资产远端引用超出MCAP根目录，请选择保留原层级的输入目录')
        identity=digest(['mcap-assets-v1',str(target),entry['files']])
        if verify_only:
            verify_backup(spec['data_server'],target,entry['files'],identity)
        else:
            receipt=transfer(spec['data_server'],entry['source'],target,entry['files'],identity,
                             guard=lambda:check_assets(entries))
            durable_json(Path(spec['root'])/'mcap-assets'/f'{identity}.json',
                         {'destination':str(target),'files':entry['files'],'receipt':receipt})
    return entries
