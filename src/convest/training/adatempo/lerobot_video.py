"""Nearest-frame video sampling, with a new uniform output time base."""
from fractions import Fraction

import av
import numpy as np


def resample_video(source, destination, positions, source_length, fps):
    selected = np.rint(positions).astype(int)
    if len(selected) == 0 or selected[0] < 0 or selected[-1] >= source_length or np.any(np.diff(selected) < 0):
        raise ValueError("invalid video source positions")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cursor, count = 0, 0
    with av.open(str(source)) as reader, av.open(str(destination), "w") as writer:
        original = reader.streams.video[0]
        original.thread_count = 2
        stream = writer.add_stream("libx264", rate=Fraction(fps))
        stream.width, stream.height = original.width, original.height
        stream.pix_fmt, stream.thread_count = "yuv420p", 2
        stream.options = {"crf": "18", "preset": "veryfast"}
        for i, frame in enumerate(reader.decode(video=0)):
            count += 1
            if frame.pts is None or abs(float(frame.pts * frame.time_base) - i / fps) > 1e-4:
                raise ValueError(f"source video timestamp mismatch: {source}/{i}")
            while cursor < len(selected) and selected[cursor] == i:
                output = av.VideoFrame.from_ndarray(frame.to_ndarray(format="rgb24"), format="rgb24")
                output.pts, output.time_base = cursor, Fraction(1, fps)
                for packet in stream.encode(output): writer.mux(packet)
                cursor += 1
        for packet in stream.encode(): writer.mux(packet)
    if count != source_length or cursor != len(selected):
        raise ValueError(f"source video frame count mismatch: {source}: {count}/{source_length}")
    return inspect_video(destination, len(selected), fps, [stream.height, stream.width, 3])


def inspect_video(path, expected, fps, shape):
    """Decode every frame/PTS; estimate RGB normalization from evenly sampled pixels."""
    samples = set(np.linspace(0, expected - 1, min(expected, max(100, int(expected ** .75)))).astype(int))
    total, squares = np.zeros(3), np.zeros(3)
    low, high = np.full(3, np.inf), np.full(3, -np.inf)
    pixels, sampled, count = 0, 0, 0
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_count = 2
        if [stream.height, stream.width, 3] != shape or stream.average_rate != fps:
            raise ValueError(f"video shape/fps mismatch: {path}")
        for i, frame in enumerate(container.decode(video=0)):
            count += 1
            if frame.pts is None or abs(float(frame.pts * frame.time_base) - i / fps) > 1e-4:
                raise ValueError(f"output video timestamp mismatch: {path}/{i}")
            if i in samples:
                step = max(1, max(shape[:2]) // 150)
                array = frame.to_ndarray(format="rgb24")[::step, ::step].reshape(-1, 3).astype(np.float64) / 255
                total += array.sum(axis=0)
                squares += (array * array).sum(axis=0)
                low, high = np.minimum(low, array.min(axis=0)), np.maximum(high, array.max(axis=0))
                pixels += len(array)
                sampled += 1
    if count != expected or not pixels:
        raise ValueError(f"output video frame count mismatch: {path}: {count}/{expected}")
    mean = total / pixels
    return {"min": low[:, None, None].tolist(), "max": high[:, None, None].tolist(),
            "mean": mean[:, None, None].tolist(),
            "std": np.sqrt(np.maximum(squares / pixels - mean ** 2, 0))[:, None, None].tolist(),
            "count": [sampled]}
