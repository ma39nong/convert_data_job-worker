import hashlib
import sqlite3
from pathlib import Path

import pytest
import yaml

from bag_worker.bag import Bag, connect, normalized_ranges


BASE = 1700000000123456789  # Deliberately not exactly representable by a JS float.


def make_bag(root, split=False):
    root.mkdir()
    files = []
    for fi, times in enumerate(([0, 1, 2], [3, 4, 5]) if split else ([0, 1, 2, 3, 4, 5],)):
        path = root / f'input_{fi}.db3'
        files.append(path.name)
        with sqlite3.connect(path) as c:
            c.executescript('''CREATE TABLE schema(schema_version INTEGER PRIMARY KEY,ros_distro TEXT NOT NULL);
                INSERT INTO schema VALUES(3,'humble');
                CREATE TABLE metadata(id INTEGER PRIMARY KEY,metadata_version INTEGER NOT NULL,metadata TEXT NOT NULL);
                CREATE TABLE topics(id INTEGER PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,serialization_format TEXT NOT NULL,offered_qos_profiles TEXT NOT NULL);
                CREATE TABLE messages(id INTEGER PRIMARY KEY,topic_id INTEGER NOT NULL,timestamp INTEGER NOT NULL,data BLOB NOT NULL);
                CREATE INDEX timestamp_idx ON messages(timestamp ASC);''')
            # Topic IDs may vary across split files.
            for tid in range(1, 4):
                c.execute('INSERT INTO topics VALUES(?,?,?,?,?)', (tid+fi*10, f'/topic{tid}', 'custom/msg/Opaque', 'cdr', ''))
                for second in times:
                    c.execute('INSERT INTO messages(topic_id,timestamp,data) VALUES(?,?,?)',
                              (tid+fi*10, BASE+second*10**9, f'{tid}:{second}'.encode()))
    (root/'metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information': {
        'version': 5, 'storage_identifier': 'sqlite3', 'relative_file_paths': files,
        'compression_mode': '', 'compression_format': ''}}))
    (root/'collection_state.json').write_text('{"source":true}')
    return Bag(root)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('ranges', [[], [[1,1]], [[2,1]], [[-1,1]], [[0,6]], [[0,'NaN']], [[0,'Infinity']], [['x',1]], [[1]], 'bad'])
def test_invalid_ranges(ranges):
    with pytest.raises(ValueError):
        normalized_ranges(ranges, 5)


def test_merge_and_nanosecond_precision():
    assert normalized_ranges([[2,3],[0,1],[1,2],[4,4.000000001]],5) == [(0,3_000_000_000),(4_000_000_000,4_000_000_001)]


@pytest.mark.parametrize('split', [False, True])
def test_export_all_topics_boundaries_and_source_unchanged(tmp_path, split):
    bag = make_bag(tmp_path/'source', split)
    before = {p.name: digest(p) for p in bag.root.iterdir()}
    ranges = [[1,2], [2,3], [4,4.5]]
    plan = bag.plan(ranges)
    assert plan['deleted'] == 9 and plan['kept'] == 9
    out = tmp_path/'result'
    report = bag.export(ranges, out, compact=False)
    assert report['kept'] == 9
    result = Bag(out)
    assert result.count == 9
    for name, refs in result.refs.items():
        assert [x[0] for x in refs] == [BASE, BASE+3*10**9, BASE+5*10**9]
        for t,fi,ident in refs:
            with connect(result.files[fi]) as db:
                data = db.execute('SELECT data FROM messages WHERE id=?',(ident,)).fetchone()[0]
                assert data == f'{name[-1]}:{(t-BASE)//10**9}'.encode()
    meta = yaml.safe_load((out/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    assert meta['starting_time']['nanoseconds_since_epoch'] == BASE
    assert meta['duration']['nanoseconds'] == 5*10**9
    assert sum(t['message_count'] for t in meta['topics_with_message_count']) == 9
    assert all(t['message_count'] == 3 for t in meta['topics_with_message_count'])
    with connect(result.files[0]) as db:
        # SQLite's embedded metadata is unwrapped; metadata.yaml is wrapped.
        embedded = yaml.safe_load(db.execute('SELECT metadata FROM metadata').fetchone()[0])
        assert embedded['version'] == 5 and embedded['message_count'] == 9
    assert (out/'source_collection_state.json').read_text() == '{"source":true}'
    assert not (out/'collection_state.json').exists()
    assert {p.name: digest(p) for p in bag.root.iterdir()} == before


def test_delete_to_end_and_empty_split(tmp_path):
    bag = make_bag(tmp_path/'source', True)
    out = tmp_path/'result'
    bag.export([[2,bag.duration]], out, compact=False)
    result=Bag(out)
    assert result.count == 6
    assert len(result.files) == 1
    assert result.end == BASE+10**9


def test_reject_all_messages_and_existing_output(tmp_path):
    bag = make_bag(tmp_path/'source')
    with pytest.raises(ValueError, match='全部'):
        bag.export([[0,bag.duration]],tmp_path/'all')
    assert not (tmp_path/'all').exists()
    with pytest.raises(ValueError, match='内部'):
        bag.export([[0,1]],bag.root/'child')
    out=tmp_path/'exists';out.mkdir();(out/'keep.txt').write_text('keep')
    with pytest.raises(ValueError,match='已存在'):
        bag.export([[0,1]],out)
    assert (out/'keep.txt').read_text()=='keep'


def test_source_change_and_failure_cleanup(tmp_path):
    bag=make_bag(tmp_path/'source')
    out=tmp_path/'failed'
    def fail(**kw): raise RuntimeError('injected failure')
    with pytest.raises(RuntimeError):bag.export([[0,1]],out,fail)
    assert not out.exists() and not out.with_name('failed.partial').exists()
    with sqlite3.connect(bag.files[0]) as db:
        db.execute('DELETE FROM messages WHERE id=1')
    with pytest.raises(ValueError,match='源文件已改变'):
        bag.export([[0,1]],out)
