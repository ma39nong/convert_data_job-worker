from convest.subtasks.remote import seed_verified_cache
from convest.subtasks.core import atomic_json,sha


def test_reuse_is_hash_verified_and_never_hardlinks_or_changes_existing_files(tmp_path):
    cache=tmp_path/'old';cache.mkdir();old=cache/'wrist.mp4';old.write_bytes(b'good wrist video')
    digest=sha(old);atomic_json(cache/'cache-source.json',{'all_selected_sha256_verified':True,'manifest':{'files':{'wrist.mp4':{'sha256':digest}}}})
    partial=tmp_path/'.new.partial-old';partial.mkdir();(partial/'main.mp4').write_bytes(b'good main video')
    stage=tmp_path/'.new.partial-next';stage.mkdir()
    expected={'files':{'wrist.mp4':{'sha256':digest,'size':old.stat().st_size,'mtime_ns':1000000000},'main.mp4':{'sha256':sha(partial/'main.mp4'),'size':15,'mtime_ns':1000000000}}}
    assert set(seed_verified_cache(expected,tmp_path,stage,'new'))=={'wrist.mp4','main.mp4'}
    assert old.stat().st_ino!=(stage/'wrist.mp4').stat().st_ino
    assert (stage/'wrist.mp4').stat().st_mtime_ns==1000000000
    (stage/'wrist.mp4').write_bytes(b'changed')
    assert old.read_bytes()==b'good wrist video'


def test_modified_cache_or_incomplete_partial_is_not_reused(tmp_path):
    cache=tmp_path/'old';cache.mkdir();video=cache/'wrist.mp4';video.write_bytes(b'correct')
    digest=sha(video);atomic_json(cache/'cache-source.json',{'all_selected_sha256_verified':True,'manifest':{'files':{'wrist.mp4':{'sha256':digest}}}})
    video.write_bytes(b'incorrect')
    partial=tmp_path/'.new.partial-old';partial.mkdir();(partial/'wrist.mp4').write_bytes(b'partial')
    stage=tmp_path/'.new.partial-next';stage.mkdir()
    assert seed_verified_cache({'files':{'wrist.mp4':{'sha256':digest,'size':7}}},tmp_path,stage,'new')==[]
    assert not (stage/'wrist.mp4').exists()
