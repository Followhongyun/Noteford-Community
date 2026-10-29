from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
ACCOUNT_SUFFIX = re.compile(r'^(.+)_([0-9a-fA-F]{4})$')


def account_username(account_directory: str) -> str:
    match = ACCOUNT_SUFFIX.fullmatch(account_directory)
    return match.group(1) if match else account_directory


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def _canonical(path: Path) -> str:
    return str(path.expanduser().resolve())


def library_id_for(wechat_root: Path, account: str) -> str:
    material = (_canonical(wechat_root) + '\0' + str(account)).encode(
        'utf-8', 'surrogatepass'
    )
    return 'lib-' + hashlib.sha256(material).hexdigest()[:16]


def normalize_wechat_root(path: Path) -> tuple[Path, list[str] | None]:
    root = path.expanduser().resolve()
    if not root.is_dir():
        return root, None

    # The user may select one concrete account directory.
    if (
        (root / 'db_storage' / 'session' / 'session.db').is_file()
        and (root / 'db_storage' / 'contact' / 'contact.db').is_file()
    ):
        return root.parent, [root.name]

    # Or the parent directory that contains xwechat_files.
    nested = root / 'xwechat_files'
    if nested.is_dir():
        return nested.resolve(), None

    return root, None


def discover_account_directories(wechat_root: Path) -> tuple[Path, list[str]]:
    root, explicit = normalize_wechat_root(wechat_root)
    if explicit is not None:
        return root, explicit
    if not root.is_dir():
        return root, []
    found: list[str] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if not child.is_dir() or child.is_symlink():
            continue
        session = child / 'db_storage' / 'session' / 'session.db'
        contact = child / 'db_storage' / 'contact' / 'contact.db'
        if session.is_file() and contact.is_file():
            found.append(child.name)
    return root, found


@dataclass
class LibraryRecord:
    id: str
    wechat_root: str
    account: str
    display_name: str
    state: str
    created_at: str
    updated_at: str
    initialized_at: str | None = None
    last_error: str | None = None

    def public_dict(self, *, current: bool = False) -> dict[str, Any]:
        return {
            'id': self.id,
            'display_name': self.display_name,
            'state': self.state,
            'is_current': current,
            'initialized': bool(self.initialized_at),
            'last_error': self.last_error,
            'source_root': self.wechat_root,
        }


@dataclass
class LibraryContext:
    app_root: Path
    library: LibraryRecord
    overlap_local_ids: int = 200
    allow_memory_rescan: bool = True
    voice_decoder_path: str | None = None

    @property
    def root(self) -> Path:
        return self.app_root / 'libraries' / self.library.id

    @property
    def data_root(self) -> Path:
        return self.root / 'data'

    @property
    def secrets_root(self) -> Path:
        return self.root / 'secrets'

    @property
    def index_path(self) -> Path:
        return self.data_root / 'index' / 'wechat_index.db'

    @property
    def candidates_path(self) -> Path:
        return self.secrets_root / 'candidate_keys.json'

    @property
    def image_keys_path(self) -> Path:
        return self.secrets_root / 'image_keys.json'

    @property
    def wechat_root(self) -> Path:
        return Path(self.library.wechat_root)

    @property
    def account(self) -> str:
        return self.library.account

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root / 'index',
            self.data_root / 'state',
            self.data_root / 'encrypted',
            self.data_root / 'decrypted',
            self.data_root / 'media-cache',
            self.data_root / 'batches',
            self.data_root / 'jobs',
            self.data_root / 'runtime',
            self.secrets_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def config_dict(self) -> dict[str, Any]:
        self.ensure_dirs()
        return {
            'app_root': str(self.app_root),
            # Existing Core functions deliberately receive the per-library root
            # as project_root. This keeps every derived data/secrets path inside
            # one filesystem namespace without teaching every module about the
            # global registry.
            'project_root': str(self.root),
            'library_id': self.library.id,
            'library_added_at': self.library.created_at,
            'wechat_root': str(self.wechat_root),
            'account': self.account,
            'index_path': str(self.index_path),
            'candidates_path': str(self.candidates_path),
            'image_keys_path': str(self.image_keys_path),
            'voice_decoder_path': self.voice_decoder_path,
            'overlap_local_ids': int(self.overlap_local_ids),
            'allow_memory_rescan': bool(self.allow_memory_rescan),
        }


class LibraryRegistry:
    def __init__(self, app_root: Path, root_config: dict[str, Any]):
        self.app_root = app_root.resolve()
        from .workspace_access import WorkspaceAccess
        self.workspace_access = WorkspaceAccess(self.app_root)
        self.root_config = root_config
        registry_value = root_config.get('library_registry_path')
        self.registry_path = (
            Path(registry_value).resolve()
            if registry_value
            else self.app_root / 'data' / 'library_registry.json'
        )
        self._payload = self._read()

    def _read(self) -> dict[str, Any]:
        self.workspace_access.check()
        if not self.registry_path.is_file():
            return {
                'schema': SCHEMA_VERSION,
                'current_library_id': None,
                'libraries': [],
                'ignored_library_ids': [],
                'updated_at': _utcnow(),
            }
        payload = json.loads(self.registry_path.read_text(encoding='utf-8'))
        if int(payload.get('schema') or 0) != SCHEMA_VERSION:
            raise ValueError('unsupported library registry schema')
        payload.setdefault('libraries', [])
        payload.setdefault('ignored_library_ids', [])
        payload.setdefault('current_library_id', None)
        return payload

    def _write(self) -> None:
        self.workspace_access.check()
        self._payload['schema'] = SCHEMA_VERSION
        self._payload['updated_at'] = _utcnow()
        _atomic_json(self.registry_path, self._payload)

    def _records(self) -> list[LibraryRecord]:
        self.workspace_access.check()
        out: list[LibraryRecord] = []
        for raw in self._payload.get('libraries') or []:
            out.append(
                LibraryRecord(
                    id=str(raw['id']),
                    wechat_root=str(raw['wechat_root']),
                    account=str(raw['account']),
                    display_name=str(raw.get('display_name') or '微信资料库'),
                    state=str(raw.get('state') or 'discovered'),
                    created_at=str(raw.get('created_at') or _utcnow()),
                    updated_at=str(raw.get('updated_at') or _utcnow()),
                    initialized_at=raw.get('initialized_at'),
                    last_error=raw.get('last_error'),
                )
            )
        return out

    def list(self) -> list[LibraryRecord]:
        return self._records()

    def get(self, library_id: str) -> LibraryRecord:
        for record in self._records():
            if record.id == library_id:
                return record
        raise KeyError('library_not_found')

    def current(self) -> LibraryRecord:
        current_id = self._payload.get('current_library_id')
        if current_id:
            try:
                return self.get(str(current_id))
            except KeyError:
                pass
        records = self._records()
        if not records:
            raise KeyError('no_library_registered')
        self._payload['current_library_id'] = records[0].id
        self._write()
        return records[0]

    def context(self, library_id: str | None = None) -> LibraryContext:
        record = self.get(library_id) if library_id else self.current()
        return LibraryContext(
            app_root=self.app_root,
            library=record,
            overlap_local_ids=int(self.root_config.get('overlap_local_ids', 200)),
            allow_memory_rescan=bool(
                self.root_config.get('allow_memory_rescan', True)
            ),
            voice_decoder_path=self.root_config.get('voice_decoder_path'),
        )

    def switch(self, library_id: str) -> LibraryRecord:
        record = self.get(library_id)
        self._payload['current_library_id'] = record.id
        self._write()
        return record

    def discover(self, wechat_root: Path) -> list[LibraryRecord]:
        from .workspace_access import require_source
        require_source(self.app_root, wechat_root)
        root, accounts = discover_account_directories(wechat_root)
        existing = {record.id: record for record in self._records()}
        order = [record.id for record in self._records()]
        now = _utcnow()
        changed = False

        ignored = {
            str(value)
            for value in (self._payload.get('ignored_library_ids') or [])
        }

        for account in accounts:
            library_id = library_id_for(root, account)
            if library_id in existing or library_id in ignored:
                continue
            record = LibraryRecord(
                id=library_id,
                wechat_root=str(root),
                account=account,
                display_name=f'本地微信 {len(existing) + 1}',
                state='discovered',
                created_at=now,
                updated_at=now,
            )
            existing[library_id] = record
            order.append(library_id)
            changed = True

        if changed:
            self._payload['libraries'] = [
                asdict(existing[library_id]) for library_id in order
            ]
            if not self._payload.get('current_library_id') and order:
                self._payload['current_library_id'] = order[0]
            self._write()
        return self._records()

    def register_account(self, wechat_root: Path, account: str) -> LibraryRecord:
        from .workspace_access import require_source
        require_source(self.app_root, wechat_root)
        root, accounts = discover_account_directories(wechat_root)
        if account not in accounts:
            raise ValueError('account_not_found')
        library_id = library_id_for(root, account)
        try:
            return self.get(library_id)
        except KeyError:
            pass

        now = _utcnow()
        record = LibraryRecord(
            id=library_id,
            wechat_root=str(root),
            account=account,
            display_name=account_username(account),
            state='discovered',
            created_at=now,
            updated_at=now,
        )
        self._payload['libraries'].append(asdict(record))
        ignored = [
            value for value in self._payload.get('ignored_library_ids') or []
            if str(value) != library_id
        ]
        self._payload['ignored_library_ids'] = ignored
        if not self._payload.get('current_library_id'):
            self._payload['current_library_id'] = library_id
        self._write()
        return record

    def register_archive_account(self, archive_root: Path, account: str) -> LibraryRecord:
        from .workspace_access import require_source
        require_source(self.app_root, archive_root)
        """Register one already-decrypted macOS account directory.

        Unlike ``register_account``, this does not require encrypted
        ``db_storage`` files. Windows registration does not call it.
        """

        root = archive_root.expanduser().resolve()
        account_dir = root / account
        if not account_dir.is_dir() or account_dir.is_symlink():
            raise ValueError('account_not_found')
        library_id = library_id_for(root, account)
        try:
            return self.get(library_id)
        except KeyError:
            pass

        now = _utcnow()
        record = LibraryRecord(
            id=library_id,
            wechat_root=str(root),
            account=account,
            display_name=account_username(account),
            state='discovered',
            created_at=now,
            updated_at=now,
        )
        self._payload['libraries'].append(asdict(record))
        ignored = [
            value for value in self._payload.get('ignored_library_ids') or []
            if str(value) != library_id
        ]
        self._payload['ignored_library_ids'] = ignored
        if not self._payload.get('current_library_id'):
            self._payload['current_library_id'] = library_id
        self._write()
        return record

    def update(
        self,
        library_id: str,
        *,
        display_name: str | None = None,
        state: str | None = None,
        initialized: bool | None = None,
        last_error: str | None = None,
    ) -> LibraryRecord:
        now = _utcnow()
        rows = self._payload.get('libraries') or []
        for row in rows:
            if str(row.get('id')) != library_id:
                continue
            if display_name is not None:
                row['display_name'] = str(display_name).strip() or row.get(
                    'display_name', '微信资料库'
                )
            if state is not None:
                row['state'] = state
            if initialized is True and not row.get('initialized_at'):
                row['initialized_at'] = now
            row['last_error'] = last_error
            row['updated_at'] = now
            self._write()
            return self.get(library_id)
        raise KeyError('library_not_found')

    def remove(
        self,
        library_id: str,
        *,
        delete_local_data: bool = True,
        ignore_source: bool = True,
    ) -> dict[str, Any]:
        record = self.get(library_id)
        context = self.context(library_id)
        removed_path = str(context.root)

        if delete_local_data and context.root.exists():
            shutil.rmtree(context.root)

        rows = [
            row
            for row in (self._payload.get('libraries') or [])
            if str(row.get('id')) != library_id
        ]
        self._payload['libraries'] = rows

        if ignore_source:
            ignored = {
                str(value)
                for value in (self._payload.get('ignored_library_ids') or [])
            }
            ignored.add(library_id)
            self._payload['ignored_library_ids'] = sorted(ignored)

        if self._payload.get('current_library_id') == library_id:
            self._payload['current_library_id'] = (
                str(rows[0]['id']) if rows else None
            )

        self._write()
        return {
            'id': library_id,
            'account': record.account,
            'deleted_local_data': bool(delete_local_data),
            'ignored_source': bool(ignore_source),
            'removed_path': removed_path,
        }

    def public_list(self) -> list[dict[str, Any]]:
        current_id = self._payload.get('current_library_id')
        return [
            record.public_dict(current=(record.id == current_id))
            for record in self._records()
        ]

    def bootstrap_from_root_config(self) -> list[LibraryRecord]:
        roots = self.root_config.get('wechat_roots')
        if not roots:
            one = self.root_config.get('wechat_root')
            roots = [one] if one else []
        for value in roots:
            if value:
                self.discover(Path(str(value)))

        legacy_account = self.root_config.get('account')
        legacy_root = self.root_config.get('wechat_root')
        if legacy_account and legacy_root:
            legacy_id = library_id_for(Path(str(legacy_root)), str(legacy_account))
            try:
                self.switch(legacy_id)
            except KeyError:
                self.discover(Path(str(legacy_root)))
                try:
                    self.switch(legacy_id)
                except KeyError:
                    pass
        return self._records()


def load_root_config(path: Path) -> dict[str, Any]:
    path = path.resolve()
    payload = json.loads(path.read_text(encoding='utf-8'))
    def relative_to_config(value):
        value = Path(value).expanduser()
        return (value if value.is_absolute() else path.parent / value).resolve()
    app_root = relative_to_config(payload.get('project_root') or path.parent)
    from .workspace_access import require_active
    require_active(app_root)
    roots = payload.get('wechat_roots')
    if not roots:
        one = payload.get('wechat_root')
        roots = [one] if one else []
    return {
        **payload,
        'project_root': str(app_root),
        **({'wechat_root': str(relative_to_config(payload['wechat_root']))} if payload.get('wechat_root') else {}),
        'voice_decoder_path': str(relative_to_config(payload['voice_decoder_path'])) if payload.get('voice_decoder_path') else None,
        'wechat_roots': [
            str(relative_to_config(str(value)))
            for value in roots
            if value
        ],
        'library_registry_path': str(
            relative_to_config(payload.get('library_registry_path')
                or (app_root / 'data' / 'library_registry.json'))
        ),
    }


def registry_from_config(path: Path) -> LibraryRegistry:
    root_config = load_root_config(path)
    import sys
    registry_type = LibraryRegistry
    if sys.platform == 'darwin':
        from .mac_accounts import MacLibraryRegistry
        registry_type = MacLibraryRegistry
    registry = registry_type(Path(root_config['project_root']), root_config)
    registry.bootstrap_from_root_config()
    return registry


def resolve_library_config(
    path: Path,
    library_id: str | None = None,
) -> dict[str, Any]:
    registry = registry_from_config(path)
    return registry.context(library_id).config_dict()



def _rewrite_value_paths(value: Any, replacements: list[tuple[str, str]]) -> tuple[Any, int]:
    changed = 0
    if isinstance(value, str):
        for old, new in replacements:
            if value == old or value.startswith(old + '\\'):
                return new + value[len(old):], 1
        return value, 0
    if isinstance(value, list):
        out = []
        for item in value:
            next_item, count = _rewrite_value_paths(item, replacements)
            out.append(next_item)
            changed += count
        return out, changed
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            next_item, count = _rewrite_value_paths(item, replacements)
            out[key] = next_item
            changed += count
        return out, changed
    return value, 0


def repair_migrated_library_paths(
    *,
    app_root: Path,
    context: LibraryContext,
) -> dict[str, Any]:
    replacements = [
        (str((app_root / 'data').resolve()), str(context.data_root.resolve())),
        (str((app_root / 'secrets').resolve()), str(context.secrets_root.resolve())),
    ]
    files_changed = 0
    values_changed = 0
    for path in context.root.rglob('*.json'):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            continue
        repaired, count = _rewrite_value_paths(payload, replacements)
        if not count:
            continue
        _atomic_json(path, repaired)
        files_changed += 1
        values_changed += count
    return {
        'files_changed': files_changed,
        'values_changed': values_changed,
    }

def _move_if_exists(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    if destination.exists():
        if (
            source.is_dir()
            and destination.is_dir()
            and not any(destination.iterdir())
        ):
            destination.rmdir()
        else:
            raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return True


def migrate_legacy_single_library(
    *,
    app_root: Path,
    root_config: dict[str, Any],
    registry: LibraryRegistry,
) -> dict[str, Any]:
    """Move the old single-account workspace into the current LibraryContext.

    This is intentionally explicit and one-shot. It never overwrites a library
    destination and writes the registry state only after every successful move.
    """

    account = root_config.get('account')
    wechat_root = root_config.get('wechat_root')
    if not account or not wechat_root:
        return {'migrated': False, 'reason': 'no_legacy_single_account_config'}

    library_id = library_id_for(Path(str(wechat_root)), str(account))
    record = registry.get(library_id)
    context = registry.context(library_id)
    context.ensure_dirs()

    marker = context.root / '.legacy-migrated-v1'
    if marker.is_file():
        repair = repair_migrated_library_paths(
            app_root=app_root,
            context=context,
        )
        return {
            'migrated': False,
            'reason': 'already_migrated',
            'library_id': library_id,
            'repair': repair,
        }

    moved: list[tuple[str, str]] = []

    def move(source: Path, destination: Path) -> None:
        if _move_if_exists(source, destination):
            moved.append((str(source), str(destination)))

    # SQLite index and its sidecars.
    legacy_index = app_root / 'data' / 'index' / 'wechat_index.db'
    for suffix in ('', '-wal', '-shm'):
        move(
            Path(str(legacy_index) + suffix),
            Path(str(context.index_path) + suffix),
        )

    # Account-scoped snapshot/state directories.
    for layer in ('encrypted', 'decrypted', 'state'):
        move(
            app_root / 'data' / layer / str(account),
            context.data_root / layer / str(account),
        )

    # Single-account artifacts produced before Library Registry existed.
    for layer in ('media-cache', 'batches', 'jobs'):
        move(
            app_root / 'data' / layer,
            context.data_root / layer,
        )

    legacy_daily = app_root / 'data' / 'state' / 'daily_job.json'
    move(legacy_daily, context.data_root / 'state' / 'daily_job.json')

    legacy_selective = app_root / 'data' / 'runtime' / 'selective'
    move(
        legacy_selective,
        context.data_root / 'runtime' / 'selective',
    )

    for name in ('candidate_keys.json', 'image_keys.json'):
        import sys
        if sys.platform == 'darwin':
            from .mac_key_storage import migrate_cache_path
            migrate_cache_path(
                app_root / 'secrets' / name,
                context.secrets_root / name,
                'image' if name == 'image_keys.json' else 'candidates',
            )
        else:
            move(app_root / 'secrets' / name, context.secrets_root / name)

    repair = repair_migrated_library_paths(
        app_root=app_root,
        context=context,
    )

    marker.write_text(
        json.dumps(
            {
                'schema': 1,
                'migrated_at': _utcnow(),
                'legacy_account': str(account),
                'moved_count': len(moved),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    display_name = record.display_name
    if context.index_path.is_file():
        try:
            con = sqlite3.connect(
                f'file:{context.index_path.resolve().as_posix()}?mode=ro',
                uri=True,
            )
            row = con.execute(
                'SELECT self_display_name FROM account_profile WHERE account=?',
                (str(account),),
            ).fetchone()
            con.close()
            if row and row[0]:
                display_name = str(row[0])
        except sqlite3.Error:
            pass

    registry.switch(library_id)
    registry.update(
        library_id,
        display_name=display_name,
        state='ready',
        initialized=True,
        last_error=None,
    )
    return {
        'migrated': True,
        'library_id': library_id,
        'moved': moved,
        'library_root': str(context.root),
        'repair': repair,
    }
