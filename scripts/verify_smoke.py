"""Read-only, full-payload audit for a single-volume smoke export.

All known message types in the local sample have a leading Header. The auditor
allows only its Time bytes to change and independently checks the time mapping.
Unknown payload types must be completely byte-identical.
"""
import hashlib
import json
import sqlite3
import struct
import sys
from pathlib import Path

from rosbags.rosbag2 import Reader
import yaml


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def audit(source, output):
    source, output = Path(source), Path(output)
    report = json.loads((output/'trim_report.json').read_text())
    ranges = [(int(x['source_start_ns']), int(x['source_end_ns'])) for x in report['mapping']]

    def mapped(t):
        return t-sum(max(0, min(t,b)-a) for a,b in ranges)

    original = sorted(source.glob('*.db3'))
    exported = sorted(output.glob('*.db3'))
    assert len(original) == len(exported) == 1, 'This auditor expects one volume'
    hashes = {str(p): sha(p) for p in source.iterdir() if p.is_file()}
    checked, counts = 0, {}
    with sqlite3.connect(original[0].resolve().as_uri()+'?mode=ro', uri=True) as src, sqlite3.connect(exported[0].resolve().as_uri()+'?mode=ro', uri=True) as dst:
        kinds = dict(src.execute('SELECT id,type FROM topics'))
        names = dict(src.execute('SELECT id,name FROM topics'))
        rows = iter(dst.execute('SELECT id,topic_id,timestamp,data FROM messages ORDER BY id'))
        for ident, tid, t, payload in src.execute('SELECT id,topic_id,timestamp,data FROM messages ORDER BY id'):
            if any(a <= t < b for a,b in ranges):
                continue
            new_id, new_tid, new_t, new_payload = next(rows)
            assert (new_id,new_tid,new_t) == (ident,tid,mapped(t))
            kind = kinds[tid]
            if kind in report['unmodified_payload_types']:
                assert new_payload == payload
            else:
                assert kind in {'sensor_msgs/msg/Image','sensor_msgs/msg/CameraInfo',
                                'sensor_msgs/msg/JointState','geometry_msgs/msg/WrenchStamped'}, kind
                assert new_payload[:4] == payload[:4] and new_payload[12:] == payload[12:]
                endian = '<' if payload[1] else '>'
                sec,ns=struct.unpack_from(endian+'iI',payload,4)
                new_sec,new_ns=struct.unpack_from(endian+'iI',new_payload,4)
                old_stamp=sec*10**9+ns
                assert new_sec*10**9+new_ns == (mapped(old_stamp) if old_stamp else 0)
            checked += 1
            counts[names[tid]]=counts.get(names[tid],0)+1
        assert next(rows,None) is None
        assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert checked == report['kept']
    meta=yaml.safe_load((output/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    assert meta['message_count'] == checked
    assert all(t['message_count']==counts.get(t['topic_metadata']['name'],0) for t in meta['topics_with_message_count'])
    with Reader(output) as reader:
        assert reader.message_count == checked
    assert hashes == {str(p):sha(p) for p in source.iterdir() if p.is_file()}
    return {'checked_messages': checked, 'topics': counts, 'source_sha256': hashes,
            'source_unchanged_during_verification': True,
            'all_retained_non_time_payload_bytes_identical': True,
            'all_record_and_known_header_timestamps_verified': True,
            'rosbags_reader_metadata_valid': True, 'sqlite_integrity_check': 'ok',
            'unknown_payload_types': report['unmodified_payload_types']}


if __name__ == '__main__':
    result = audit(sys.argv[1],sys.argv[2])
    print(json.dumps(result,ensure_ascii=False,indent=2))
