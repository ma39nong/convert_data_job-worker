"""Process-local proof for validated input, with content and schema identity.

Receipts are never loaded from queue JSON or accepted through the command line.
Standalone jobs and restarted attempts still perform the full readiness check.
"""
from dataclasses import dataclass
from pathlib import Path
from copy import deepcopy

from .config import digest


def validation_context(config):
    contract = Path(config['contract'])
    schemas = Path(config['schema_dir'])
    return digest({'contract': contract.read_text(),
                   'schemas': {p.name: p.read_text() for p in sorted(schemas.glob('*.msg'))}})


def file_snapshot(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Unsafe validated working copy')
    result = []
    for p in [root, *sorted(root.rglob('*'))]:
        if p.is_symlink() or not (p.is_file() or p.is_dir()):
            raise ValueError('Unsafe entry in validated working copy')
        st = p.stat()
        result.append((p.relative_to(root).as_posix(), st.st_size, st.st_mtime_ns,
                       st.st_ctime_ns, st.st_ino, st.st_dev))
    return result


@dataclass(frozen=True)
class ValidatedInput:
    root: Path
    context: str
    files: list
    readiness: dict
    profile: dict
    kind: str = 'working_copy'

    def reuse(self, path, config):
        if Path(path).resolve() != self.root or validation_context(config) != self.context:
            raise ValueError('Readiness receipt path/schema mismatch')
        self.assert_unchanged()
        return deepcopy(self.readiness), deepcopy(self.profile)

    def assert_unchanged(self):
        if file_snapshot(self.root) != self.files:
            raise ValueError('Validated '+('source' if self.kind=='source' else 'working copy')+' changed')
