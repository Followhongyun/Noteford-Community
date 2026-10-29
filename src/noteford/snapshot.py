from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class FileState:
    name: str
    size: int
    mtime_ns: int


def _state(path: Path) -> FileState:
    st = path.stat()
    return FileState(path.name, st.st_size, st.st_mtime_ns)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_database_snapshot(
    database: Path,
    destination_root: Path,
    retries: int = 5,
    settle_seconds: float = 0.20,
) -> Path:
    if not database.is_file():
        raise FileNotFoundError(database)

    siblings = [database]
    for suffix in ("-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        if candidate.exists():
            siblings.append(candidate)

    destination_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final_dir = destination_root / f"{database.stem}-{stamp}"

    for attempt in range(1, retries + 1):
        before = {path.name: _state(path) for path in siblings if path.exists()}
        tmp = destination_root / f".{final_dir.name}.attempt-{attempt}.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)

        copied = []
        try:
            for source in siblings:
                if not source.exists():
                    continue
                target = tmp / source.name
                shutil.copy2(source, target)
                copied.append(target)

            after = {path.name: _state(path) for path in siblings if path.exists()}
            stable = before == after
            if stable:
                manifest = {
                    "schema": 1,
                    "source_database": str(database),
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "source_state": {name: asdict(state) for name, state in before.items()},
                    "files": [
                        {
                            "name": path.name,
                            "size": path.stat().st_size,
                            "sha256": _sha256(path),
                        }
                        for path in copied
                    ],
                }
                (tmp / "snapshot.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, final_dir)
                return final_dir
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

        time.sleep(settle_seconds)

    raise RuntimeError(
        f"database kept changing during {retries} snapshot attempts: {database}"
    )
