"""LeRobot v2.1 numeric data: preserve field schema and recorded commands."""
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .algorithm import interpolate

FIELDS = {"observation.state", "action", "observation.engaged", "timestamp",
          "frame_index", "episode_index", "index", "task_index", "source_timestamp_ns"}


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False, indent=2) + "\n")


def lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def data_path(root, info, ep):
    return safe_path(root, info["data_path"].format(episode_chunk=ep // info["chunks_size"], episode_index=ep))


def video_path(root, info, ep, key):
    return safe_path(root, info["video_path"].format(episode_chunk=ep // info["chunks_size"], episode_index=ep, video_key=key))


def safe_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if root not in path.resolve().parents:
        raise ValueError(f"dataset path escapes root: {relative}")
    return path


def values(table, key):
    return np.asarray(table[key].to_pylist())


def read_source(root):
    root = Path(root)
    info = json.loads((root / "meta/info.json").read_text())
    if info["codebase_version"] != "v2.1" or info["fps"] <= 0:
        raise ValueError("LeRobot v2.1 with positive fps required")
    numeric = {key for key, feature in info["features"].items() if feature["dtype"] != "video"}
    expected_fields = (FIELDS - {"observation.engaged"}) | ({"observation.arm_engaged"} if "observation.arm_engaged" in numeric else {"observation.engaged"})
    if numeric != expected_fields:
        raise ValueError(f"unsupported or missing fields: {numeric ^ expected_fields}")
    metadata = lines(root / "meta/episodes.jsonl")
    if [e["episode_index"] for e in metadata] != list(range(info["total_episodes"])):
        raise ValueError("contiguous episode metadata required")
    state_names = info["features"]["observation.state"]["names"]
    action_names = info["features"]["action"]["names"]
    indices = [state_names.index(name.removesuffix(".command") + ".position" if name.endswith(".command") else name)
               for name in action_names]
    episodes, offset = [], 0
    for entry in metadata:
        ep = entry["episode_index"]
        table = pq.read_table(data_path(root, info, ep))
        n = len(table)
        if set(table.column_names) != expected_fields or n != entry["length"] or n < 2:
            raise ValueError(f"invalid source columns or length: {ep}")
        for key in table.column_names:
            array = values(table, key)
            if not np.isfinite(array).all():
                raise ValueError(f"nonfinite source values: {ep}/{key}")
            feature = info["features"][key]
            expected = (n, feature["shape"][0]) if key in {"observation.state", "action", "observation.engaged", "observation.arm_engaged"} else (n,)
            if array.shape != expected:
                raise ValueError(f"source feature shape mismatch: {ep}/{key}")
        if not np.array_equal(values(table, "index"), np.arange(offset, offset + n)):
            raise ValueError("source global indices do not match metadata")
        if not np.array_equal(values(table, "frame_index"), np.arange(n)) or not np.all(values(table, "episode_index") == ep):
            raise ValueError("source episode/frame indices invalid")
        if not np.allclose(values(table, "timestamp"), np.arange(n) / info["fps"], atol=1e-4, rtol=0):
            raise ValueError("source timestamps are not on the declared fps grid")
        if len(np.unique(values(table, "task_index"))) != 1:
            raise ValueError("changing task within episode unsupported")
        episodes.append({"id": str(ep), "qpos": values(table, "observation.state")[:, indices],
                         "actions": values(table, "action"), "gripper": np.zeros(n)})
        offset += n
    if offset != info["total_frames"]:
        raise ValueError("source total_frames mismatch")
    return info, metadata, episodes


def resample_table(table, obs, act, *, fps, offset):
    nearest = np.rint(obs).astype(int)
    n = len(obs)
    data = {}
    for field in table.schema:
        key = field.name
        original = values(table, key)
        if key == "observation.state":
            array = interpolate(original, obs)
        elif key == "action":
            array = interpolate(original, act)
        elif key == "timestamp":
            array = np.arange(n) / fps
        elif key == "frame_index":
            array = np.arange(n)
        elif key == "index":
            array = np.arange(offset, offset + n)
        else:
            array = original[nearest]
        data[key] = pa.array(array.tolist(), type=field.type)
    return pa.Table.from_arrays([data[f.name] for f in table.schema], schema=table.schema)


def numeric_stats(table):
    result = {}
    for key in table.column_names:
        array = values(table, key).astype(np.float64)
        if array.ndim == 1:
            array = array[:, None]
        result[key] = {"min": array.min(axis=0).tolist(), "max": array.max(axis=0).tolist(),
                       "mean": array.mean(axis=0).tolist(), "std": array.std(axis=0).tolist(),
                       "count": [len(array)]}
    return result


def aggregate_stats(episodes):
    result = {}
    for key in episodes[0]:
        stats = [entry[key] for entry in episodes]
        weights = np.asarray([s["count"][0] for s in stats], float)
        weights /= weights.sum()
        means = np.asarray([s["mean"] for s in stats])
        shape = (len(stats),) + (1,) * (means.ndim - 1)
        mean = (means * weights.reshape(shape)).sum(axis=0)
        variance = ((np.asarray([s["std"] for s in stats]) ** 2 + (means - mean) ** 2) * weights.reshape(shape)).sum(axis=0)
        result[key] = {"min": np.min([s["min"] for s in stats], axis=0).tolist(),
                       "max": np.max([s["max"] for s in stats], axis=0).tolist(),
                       "mean": mean.tolist(), "std": np.sqrt(variance).tolist(),
                       "count": [sum(s["count"][0] for s in stats)]}
    return result
