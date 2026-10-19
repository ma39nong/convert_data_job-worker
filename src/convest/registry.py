"""Only the implemented multimodal LeRobot target is registered."""
from dataclasses import dataclass
from importlib import import_module

SOURCES = {'robot_rosbag2': 'convest.sources.robot_rosbag2'}

@dataclass(frozen=True)
class Target:
    recipe: str
    writer: str
    verifier: str
    file_patterns: tuple[str, ...]
    version: str

TARGETS = {'lerobot_v21_multimodal': Target(
    'convest.recipes.multimodal', 'convest.formats.multimodal', 'convest.verify',
    ('data/chunk-*/episode_*.parquet', 'videos/chunk-*/*/episode_*.mp4'), 'multimodal-2-depth-overlap')}

def get_source(name):
    if name not in SOURCES:
        raise ValueError(f'Unknown source: {name}')
    return import_module(SOURCES[name])

def get_target(name):
    if name not in TARGETS:
        raise ValueError(f'Unknown target: {name}')
    return TARGETS[name]

def get_writer(name):
    return import_module(get_target(name).writer)
