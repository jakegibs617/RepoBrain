"""RepoBrain configuration, loaded from .repobrain/config.json when present."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

REPOBRAIN_DIR = ".repobrain"
DB_FILENAME = "repobrain.sqlite"
CONFIG_FILENAME = "config.json"

DEFAULT_MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB
DEFAULT_HISTORY_MAX_COMMITS = 500
DEFAULT_HISTORY_MAX_FILES_PER_COMMIT = 50


@dataclass
class RepoBrainConfig:
    db_path: str = f"{REPOBRAIN_DIR}/{DB_FILENAME}"
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE
    history_max_commits: int = DEFAULT_HISTORY_MAX_COMMITS
    history_max_files_per_commit: int = DEFAULT_HISTORY_MAX_FILES_PER_COMMIT

    @classmethod
    def load(cls, root: str | Path) -> "RepoBrainConfig":
        """Load config from <root>/.repobrain/config.json, falling back to defaults."""
        cfg_path = Path(root) / REPOBRAIN_DIR / CONFIG_FILENAME
        if not cfg_path.is_file():
            return cls()
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)

    def save(self, root: str | Path) -> Path:
        cfg_path = Path(root) / REPOBRAIN_DIR / CONFIG_FILENAME
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return cfg_path
