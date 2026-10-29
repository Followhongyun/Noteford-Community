"""Mac account discovery and request binding. Original WeChat storage is read-only."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
from pathlib import Path
import threading

from .libraries import LibraryRegistry, account_username, library_id_for

request_library = ContextVar('mac_request_library', default=None)


@contextmanager
def library_scope(registry, library_id):
    if library_id:
        registry.get(library_id)  # Reject an unknown id; never fall back to another account.
    token = request_library.set(library_id or None)
    try:
        yield
    finally:
        request_library.reset(token)


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._account_lock:
            return method(self, *args, **kwargs)
    return call


class MacLibraryRegistry(LibraryRegistry):
    """Serialize account selection and background-sync registry writes in this service."""
    def __init__(self, *args, **kwargs):
        self._account_lock = threading.RLock()
        super().__init__(*args, **kwargs)

    def bootstrap_from_root_config(self):
        # Legacy single-account configuration must not reset a saved selection
        # on every service/CLI startup, or write over a running service's registry.
        if self._payload.get('current_library_id'):
            try:
                self.get(self._payload['current_library_id'])
                return self.list()
            except KeyError:
                pass
        return super().bootstrap_from_root_config()


for _name in ('list', 'get', 'current', 'switch', 'register_archive_account', 'update', 'remove'):
    setattr(MacLibraryRegistry, _name, _locked(getattr(LibraryRegistry, _name)))


class MacAccounts:
    def __init__(self, api, *, roots=None, login=None, state_dir=None):
        from .login_status import mac_login_status
        from .mac_capture import default_db_roots
        from .mac_resign import default_state_dir
        self.api = api
        self.roots = roots or default_db_roots
        self.login = login or mac_login_status
        self.state_dir = state_dir or default_state_dir()
        self.preference_path = Path(api.root_config['project_root']) / 'data' / 'mac-accounts.json'
        self.lock = threading.RLock()
        self._last_active = None
        self._active_observations = 0
        self._cancel = threading.Event()

    def following(self):
        if not self.preference_path.exists():
            return True
        return json.loads(self.preference_path.read_text(encoding='utf-8')).get('follow_wechat', True) is True

    def configure(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('follow_wechat must be boolean')
        with self.lock:
            self.preference_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.preference_path.with_suffix('.tmp')
            temp.write_text(json.dumps({'version': 1, 'follow_wechat': enabled}), encoding='utf-8')
            temp.replace(self.preference_path)
        return self.snapshot()

    def sources(self):
        from .mac_capture import _account_dirs
        result = {}
        for root in self.roots():
            root = Path(root).resolve()
            for child in sorted(_account_dirs(root), key=lambda child: child.name):
                result[library_id_for(root, child.name)] = (root, child.name)
        return result

    def snapshot(self):
        from .mac_auto import shared_auto_job
        from .mac_live_sync import managed_archive
        sources = self.sources()
        active = []
        observations = {}
        for root, _ in sources.values():
            if root in observations:
                continue
            observations[root] = self.login(root)
            if observations[root].get('state') == 'active':
                active.append((root, observations[root].get('account')))
        # Do not guess from folder timestamps or choose between simultaneous sessions.
        current = active[0] if len(active) == 1 else None
        ambiguous = any(v.get('reason') == 'multiple_account_sessions' for v in observations.values())
        if ambiguous:
            current = None
        with self.lock:
            if current == self._last_active:
                self._active_observations += 1
            else:
                self._last_active, self._active_observations = current, 1
            stable = current is not None and self._active_observations >= 2
            follows = self.following()
        archive = managed_archive().resolve()
        records = self.api.registry.list()
        rows = []
        active_library = None
        for source_id, (root, account) in sources.items():
            record = next((r for r in records if r.account == account and Path(r.wechat_root).resolve() == archive), None)
            if record is None:
                continue  # Never expose unadded accounts discovered on disk.
            logged_in = (root, account) == current
            if logged_in and record:
                active_library = record.id
            rows.append({'id': source_id, 'account': account_username(account),
                         'display_name': record.display_name if record else account_username(account),
                         'library_id': record.id if record else None,
                         'logged_in': logged_in, 'connected': bool(record)})
        capture = shared_auto_job().snapshot()
        return {'follow_wechat': follows, 'accounts': rows,
                'active_library_id': active_library,
                'suggested_library_id': active_library if follows and stable and not capture.get('running') else None,
                'login_state': 'active' if current else 'unknown' if ambiguous or any(v.get('state') != 'not_running' for v in observations.values()) else 'not_running',
                'capture': capture}

    def _finish_connection(self, account):
        from datetime import datetime, timezone
        from .libraries import LibraryContext, LibraryRecord
        from .mac_live_sync import managed_archive
        from .service import LocalApi
        archive = managed_archive()
        library_id = library_id_for(archive, account)
        try:
            record = self.api.registry.get(library_id)
            core = self.api._core(library_id)
            existed = True
        except KeyError:
            now = datetime.now(timezone.utc).isoformat()
            record = LibraryRecord(library_id, str(archive), account, account_username(account), 'discovered', now, now)
            core = LocalApi(LibraryContext(self.api.registry.app_root, record).config_dict())
            existed = False
        result = core.refresh()
        if not result.get('ok') or not result.get('complete'):
            raise ValueError('account_initialization_failed')
        status = core.status()
        # Publish only after login, verified decryption, and the first usable index.
        with self.lock:
            if self._cancel.is_set():
                raise ValueError('cancelled')
            record = self.api.registry.register_archive_account(archive, account)
            self.api.registry.update(record.id, display_name=status.get('self_display_name') or record.display_name,
                                     state='ready', initialized=True, last_error=None)
            with self.api._lock:
                self.api._cores[record.id] = core
        self.api._automatic_wake.set()
        return record.id, existed

    def connect(self, library_id=None, *, single=False):
        from .mac_entry import build_auto_config, auto_capture_run_fn
        from .mac_auto import shared_auto_job
        from .mac_capture import select_active_root
        job = shared_auto_job()
        with self.lock:
            if job.snapshot().get('running'):
                raise ValueError('正在连接账号，请等待完成')
            account = None
            if single and self.api.registry.list():
                current = self.api.registry.current()
                if library_id and library_id != current.id:
                    raise ValueError('当前版本仅连接当前微信账号')
                library_id = current.id
            root = select_active_root(list(self.roots()))
            if library_id is not None:
                record = self.api.registry.get(library_id)
                account = record.account
                root = next((r for r, a in self.sources().values() if a == account), root)
            if root is None:
                raise ValueError('未找到微信数据位置，请确认已安装并打开过微信')
            config = build_auto_config(app=Path('/Applications/WeChat.app'), state_dir=self.state_dir,
                                       db_root=root, account_prefix=account)
            config.require_live_login = True
            self._cancel = threading.Event()
            config.is_cancelled = self._cancel.is_set
            run_capture = auto_capture_run_fn(config)
            def run(report, force):
                if single:
                    def begin(account):
                        from .mac_live_sync import managed_archive
                        from .mac_capture import CaptureError
                        from .workspace_access import require_source
                        if not account or Path(account).name != account or account in {'.', '..'}:
                            raise CaptureError('invalid_account')
                        archive = managed_archive()
                        account_dir = archive / account
                        require_source(self.api.registry.app_root, account_dir)
                        if account_dir.is_symlink():
                            raise CaptureError('invalid_account_directory')
                        # The first verified login arrives before any database is
                        # copied. Registration needs this owned directory to exist.
                        account_dir.mkdir(parents=True, exist_ok=True)
                        record = self.api.registry.register_archive_account(archive, account)
                        self.api.registry.switch(record.id)
                        self.api.registry.update(record.id, state='syncing', last_error=None)
                        return record.id

                    def progress(row):
                        from .decrypted_archive import refresh_decrypted_library
                        from .mac_live_sync import managed_archive
                        name = Path(row['relative_path']).name
                        if name not in ('session.db', 'contact.db') and not name.startswith(('message_', 'biz_message_')):
                            return
                        core = self.api._core(report.library_id)
                        try:
                            with core.refresh_lock:
                                refresh_decrypted_library(wechat_root=managed_archive(), account=core.account,
                                    project_root=core.project_root, candidates_path=core.project_root / 'secrets/candidate_keys.json',
                                    index_path=core.repository.index_path,
                                    message_database_names={name}, write_archive_status=False)
                        except FileNotFoundError:
                            return  # Wait for both catalog databases to arrive.
                        except Exception:
                            if 'initial_index_retry' not in report.notes:
                                report.notes.append('initial_index_retry')
                            return  # Continue decrypting other databases; final refresh retries.
                        status = core.status()
                        self.api.registry.update(report.library_id, display_name=status.get('self_display_name') or core.account,
                                                 initialized=True)
                        report.readable = True

                    config.on_account_captured = begin
                    config.on_database_synced = progress
                run_capture(report, force)
                if self._cancel.is_set():
                    report.ok, report.error, report.phase = False, 'cancelled', 'failed'
                    return
                if report.ok and not report.connected_account:
                    report.ok, report.error, report.phase = False, 'account_not_confirmed', 'failed'
                if report.ok and report.connected_account:
                    try:
                        report.phase = 'sync'
                        report.scan_status = 'verifying'
                        report.library_id, report.already_added = self._finish_connection(report.connected_account)
                        report.phase = 'done'
                        report.scan_status = 'done'
                    except Exception:
                        error = 'cancelled' if self._cancel.is_set() else 'account_initialization_failed'
                        report.ok, report.error, report.phase = False, error, 'failed'
                        report.scan_status = 'failed'
            # This explicit Add/Reconnect action must show a login even if old keys exist.
            return job.start(run, force=True)

    def cancel(self):
        with self.lock:
            self._cancel.set()
        return {'ok': True}


def account_source(record):
    """Resolve a managed archive to the original read-only login evidence."""
    from .mac_capture import default_db_roots
    for root in default_db_roots():
        if (root / record.account / 'db_storage').is_dir():
            return str(root)
    return record.wechat_root
