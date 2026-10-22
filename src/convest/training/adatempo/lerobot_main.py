"""AdaTempo's independent LeRobot v2.1 entry point; no synthetic data."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import shutil

import numpy as np
import pyarrow.parquet as pq

from .algorithm import adatempo
from .lerobot_io import read_source, data_path, video_path, resample_table, numeric_stats, aggregate_stats, dump
from .lerobot_video import resample_video


def process(source, destination, *, workers=4, cmax=3.0):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("source and destination overlap")
    if any(destination.iterdir()):
        raise FileExistsError("output staging directory must be empty")
    print("Reading LeRobot trajectories", flush=True)
    info, metadata, episodes = read_source(source)
    print(f"Aligning {len(episodes)} trajectories ({info['total_frames']} frames)", flush=True)
    results, traces = adatempo(episodes, cmax=cmax)
    print("Alignment complete; writing tables and resampling videos", flush=True)
    out_info, stats, out_episodes, offset = deepcopy(info), [], [], 0
    cameras = [key for key, feature in info["features"].items() if feature["dtype"] == "video"]
    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for entry, result, trace in zip(metadata, results, traces):
            ep = entry["episode_index"]
            obs, act = np.asarray(result["source_index"]), np.asarray(result["action_source_index"])
            table = resample_table(pq.read_table(data_path(source, info, ep)), obs, act, fps=info["fps"], offset=offset)
            path = data_path(destination, info, ep)
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, path)
            stats.append(numeric_stats(table))
            out_episodes.append({**entry, "length": len(table)})
            dump(destination / f"processing/episode_{ep:06d}.json", {
                "episode_index": ep, "source_length": entry["length"],
                "source_index": obs.tolist(), "action_source_index": act.tolist(),
                "phase": trace["phase"], "speed_profile": trace["speed_profile"]})
            for key in cameras:
                jobs.append((ep, key, pool.submit(resample_video, video_path(source, info, ep, key),
                            video_path(destination, info, ep, key), obs, entry["length"], info["fps"])))
            offset += len(table)
        for number, (ep, key, future) in enumerate(jobs, 1):
            stats[ep][key] = future.result()
            if number % max(len(cameras), 1) == 0:
                print(f"Videos complete: {number}/{len(jobs)} (episode {ep})", flush=True)
    out_info["total_frames"] = offset
    for key in cameras:
        shape = info["features"][key]["shape"]
        out_info["features"][key]["info"] = {
            "video.height": shape[0], "video.width": shape[1], "video.codec": "h264",
            "video.pix_fmt": "yuv420p", "video.fps": info["fps"], "video.channels": 3,
            "video.is_depth_map": False, "has_audio": False}
    dump(destination / "meta/info.json", out_info)
    shutil.copyfile(source / "meta/tasks.jsonl", destination / "meta/tasks.jsonl")
    (destination / "meta/episodes.jsonl").write_text("".join(json.dumps(e) + "\n" for e in out_episodes))
    (destination / "meta/episodes_stats.jsonl").write_text("".join(
        json.dumps({"episode_index": ep, "stats": entry}, allow_nan=False) + "\n" for ep, entry in enumerate(stats)))
    dump(destination / "meta/stats.json", aggregate_stats(stats))
    from .lerobot_checks import validate_output
    print("Validating every output episode and video", flush=True)
    validation = validate_output(source, destination)
    summary = {"method": "adatempo", "input_frames": info["total_frames"], "output_frames": offset,
               "episodes": len(metadata), "cmin": 1.0, "cmax": cmax, "fps": info["fps"],
               "validation": validation, "implementation": "joint-command AdaTempo prototype; no robot evaluation",
               "observations": "continuous state interpolated at source_index; recorded velocity interpolated without time scaling",
               "actions": "recorded commands interpolated at action_source_index (next virtual time)",
               "images_and_flags": "nearest source_index; source_timestamp_ns preserves that frame's exact integer",
               "gripper_descriptor": "disabled; continuous hand commands already included in native-dimensional motion",
               "video_encoding": "h264 yuv420p CRF 18; nearest input frame; uniform original fps"}
    dump(destination / "processing/summary.json", summary)
    print(json.dumps(summary), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cmax", type=float, default=3.0)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("workers must be positive")
    process(args.input, args.output, workers=args.workers, cmax=args.cmax)
