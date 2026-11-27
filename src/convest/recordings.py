"""Numeric recording identities accepted by existing range-based batch naming."""
import re

EPISODE = re.compile(r'(?:episode|ep[_-]?)([0-9]+)\Z')


def number(name):
    match=EPISODE.fullmatch(name)
    if not match:raise ValueError('录制目录须为episode数字或ep_数字，保持现有批次编号规则')
    return int(match.group(1))
