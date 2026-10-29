"""Check the actual encoded speedup video, including its random-access interval."""
import hashlib
from fractions import Fraction

import av
import numpy as np

from convest.training.adatempo.lerobot_video import resample_video


def test_speedup_encodes_gop_30_without_changing_fps_or_source(tmp_path):
    source = tmp_path / 'normal.mp4'
    destination = tmp_path / 'speedup.mp4'
    source_length, fps = 240, 30
    # A static shot avoids scene-cut keyframes masking an incorrectly long GOP.
    with av.open(str(source), 'w') as writer:
        stream = writer.add_stream('libx264', rate=fps)
        stream.width, stream.height, stream.pix_fmt = 64, 48, 'yuv420p'
        for index in range(source_length):
            frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), 100, dtype=np.uint8), format='rgb24')
            frame.pts, frame.time_base = index, Fraction(1, fps)
            for packet in stream.encode(frame):
                writer.mux(packet)
        for packet in stream.encode():
            writer.mux(packet)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    positions = np.arange(0, source_length, 2, dtype=float)
    resample_video(source, destination, positions, source_length, fps)
    with av.open(str(destination)) as reader:
        stream = reader.streams.video[0]
        assert stream.average_rate == fps
        assert (stream.width, stream.height) == (64, 48)
        frames = list(reader.decode(video=0))
    assert len(frames) == len(positions)
    assert all(abs(float(frame.pts * frame.time_base) - index / fps) < 1e-6
               for index, frame in enumerate(frames))
    keys = [index for index, frame in enumerate(frames) if frame.key_frame]
    assert keys[0] == 0
    # Include the trailing GOP so even a video with only its first I-frame fails.
    assert max(np.diff(keys + [len(frames)])) <= 30, keys
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
