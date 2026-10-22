"""Verify all LeRobot tables, mappings, metadata, statistics and video timelines."""
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .algorithm import interpolate
from .lerobot_io import lines, data_path, video_path, values, numeric_stats, aggregate_stats
from .lerobot_video import inspect_video


def require(condition, message):
    if not condition:
        raise ValueError(message)


def same_stats(actual, expected):
    require(actual.keys() == expected.keys(), "statistic feature mismatch")
    for key in expected:
        require(actual[key].keys() == expected[key].keys(), f"statistic fields mismatch: {key}")
        for field in expected[key]:
            require(np.allclose(actual[key][field], expected[key][field], rtol=1e-6, atol=1e-7), f"statistic mismatch: {key}/{field}")


def validate_output(source, output):
    source, output = Path(source), Path(output)
    original = json.loads((source / "meta/info.json").read_text())
    info = json.loads((output / "meta/info.json").read_text())
    metadata, old_metadata = lines(output / "meta/episodes.jsonl"), lines(source / "meta/episodes.jsonl")
    stats = lines(output / "meta/episodes_stats.jsonl")
    for key in ["codebase_version", "fps", "total_episodes", "total_tasks", "total_chunks", "chunks_size", "splits", "data_path", "video_path"]:
        require(info[key] == original[key], f"metadata mismatch: {key}")
    require(len(metadata) == len(stats) == len(old_metadata) == info["total_episodes"], "episode count mismatch")
    require((output / "meta/tasks.jsonl").read_bytes() == (source / "meta/tasks.jsonl").read_bytes(), "tasks changed")
    require(info["features"].keys() == original["features"].keys(), "feature keys changed")
    for key, feature in original["features"].items():
        for field in ["shape", "names", "dtype"]:
            require(info["features"][key][field] == feature[field], f"feature changed: {key}/{field}")
    cameras = [key for key, feature in info["features"].items() if feature["dtype"] == "video"]
    offset, videos, recomputed = 0, 0, []
    for ep, entry in enumerate(metadata):
        n = entry["length"]
        require(entry["episode_index"] == ep and stats[ep]["episode_index"] == ep and n > 0, "episode index/length mismatch")
        table = pq.read_table(data_path(output, info, ep))
        old = pq.read_table(data_path(source, original, ep))
        require(table.schema == old.schema and len(table) == n, "table schema/length mismatch")
        require(data_path(output, info, ep).stat().st_ino != data_path(source, original, ep).stat().st_ino or
                data_path(output, info, ep).stat().st_dev != data_path(source, original, ep).stat().st_dev, "output hardlinks source")
        mapping = json.loads((output / f"processing/episode_{ep:06d}.json").read_text())
        obs, act = np.asarray(mapping["source_index"]), np.asarray(mapping["action_source_index"])
        require(obs.shape == act.shape == (n,) and np.isfinite(obs).all() and np.isfinite(act).all(), "mapping length/finite mismatch")
        require(obs[0] == 0 and act[-1] == len(old) - 1 and np.all(np.diff(obs) > 0) and np.all(act > obs), "mapping range/order mismatch")
        require(np.array_equal(obs[1:], act[:-1]), "next-step mapping mismatch")
        require(np.allclose(values(table, "timestamp"), np.arange(n) / info["fps"], atol=1e-4, rtol=0), "timestamp mismatch")
        require(np.array_equal(values(table, "frame_index"), np.arange(n)), "frame_index mismatch")
        require(np.array_equal(values(table, "index"), np.arange(offset, offset + n)), "global index mismatch")
        require(np.all(values(table, "episode_index") == ep), "episode_index mismatch")
        for key, positions in [("observation.state", obs), ("action", act)]:
            require(np.allclose(values(table, key), interpolate(values(old, key), positions), rtol=1e-6, atol=1e-6), f"numeric mapping mismatch: {key}")
        for key in [name for name in ("observation.engaged", "observation.arm_engaged", "task_index", "source_timestamp_ns") if name in old.column_names]:
            require(np.array_equal(values(table, key), values(old, key)[np.rint(obs).astype(int)]), f"nearest mapping mismatch: {key}")
        expected_stats = numeric_stats(table)
        for key in cameras:
            path = video_path(output, info, ep, key)
            expected_stats[key] = inspect_video(path, n, info["fps"], info["features"][key]["shape"])
            videos += 1
        same_stats(stats[ep]["stats"], expected_stats)
        recomputed.append(expected_stats)
        offset += n
    require(offset == info["total_frames"], "total_frames mismatch")
    require(videos == info["total_videos"], "total_videos mismatch")
    same_stats(json.loads((output / "meta/stats.json").read_text()), aggregate_stats(recomputed))
    return {"episodes": len(metadata), "frames": offset, "videos": videos,
            "all_tables_mappings_stats_and_video_pts_checked": True}
