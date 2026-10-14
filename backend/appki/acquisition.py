"""Private App Store acquisition CLI. Never import this worker into the web app.

The installer supplies a pinned ipatool adapter: file keyring, strict per-profile
XDG paths, protected passphrase input, identity binding and sanitized output.
Only an operator invokes login; downloads never acquire or purchase a license.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import termios
import time
from typing import Any
import uuid


DEFAULT_ROOT = Path('/var/lib/appki-acquirer')
DEFAULT_TOOL = Path('/usr/local/libexec/appki-ipatool')
DEFAULT_REGISTRY = Path('/var/lib/appki/artifacts/registry.json')
DEFAULT_ARTIFACT_ROOT = Path('/var/lib/appki/artifacts')
IPATOOL_COMMIT = '747d66fc0acd896759f43a206a7ddfa2ab49e584'
IPATOOL_ADAPTER_VERSION = '2.6.0-appki.2'
_IDENTIFIER = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
_SLUG = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z')
_BUNDLE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_JOB_ID = re.compile(r'[0-9a-f]{32}\Z')
_ERROR_CODES = frozenset({
    'requires_auth', 'requires_2fa', 'account_identity_mismatch', 'license_required',
    'network_timeout', 'network_failed', 'dns_failed', 'tls_failed', 'rate_limited',
    'upstream_http_error', 'upstream_response_invalid', 'credentials_rejected',
    'account_disabled', 'machine_identity_failed', 'runtime_setup_failed',
    'private_storage_failed', 'authentication_failed', 'authentication_rejected', 'acquisition_failed',
    'auth_success_unconfirmed', 'auth_cache_unconfirmed', 'tool_timeout',
    'tool_unavailable', 'tool_crashed', 'invalid_tool_output',
})
_ERROR_STAGES = frozenset({'local_storage', 'machine', 'bag', 'assets', 'runtime',
    'signer_setup', 'authentication', 'verification', 'signer_close', 'account_binding',
    'download', 'runner', 'unknown'})


class AcquisitionError(Exception):
    """A public error code, containing no upstream output or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ''
    stderr: str = ''


class IpatoolRunner:
    def run(self, argv: list[str], *, env: dict[str, str], interactive: bool = False,
            lock_fd: int | None = None) -> CommandResult:
        terminal_settings = termios.tcgetattr(0) if interactive and os.isatty(0) else None
        try:
            result = subprocess.run(
                argv, env=env, shell=False, stdin=None if interactive else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=None if interactive else subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace', timeout=900 if interactive else 3600,
                pass_fds=() if lock_fd is None else (lock_fd,),
            )
        except subprocess.TimeoutExpired as exc:
            raise AcquisitionError('tool_timeout') from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise AcquisitionError('tool_unavailable') from exc
        finally:
            # A timeout kills the child before Go's deferred terminal restore.
            # Preserve the operator's terminal on timeout and Ctrl-C as well.
            if terminal_settings is not None:
                try:
                    termios.tcsetattr(0, termios.TCSANOW, terminal_settings)
                except (OSError, termios.error):
                    pass
        if len(result.stdout) > 1024 * 1024 or len(result.stderr or '') > 1024 * 1024:
            raise AcquisitionError('invalid_tool_output')
        return CommandResult(result.returncode, result.stdout, result.stderr or '')


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AcquisitionError('invalid_identifier')
    return value


def _text(value: str, maximum: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise AcquisitionError('invalid_metadata')
    return value.strip()


def _app_slug(value: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise AcquisitionError('invalid_app_slug')
    return value


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise AcquisitionError('unsafe_private_path')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise AcquisitionError('unsafe_private_permissions')


def _private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise AcquisitionError('unsafe_private_permissions')


def _json_messages(result: CommandResult) -> list[dict[str, Any]]:
    messages = []
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(message, dict):
            messages.append(message)
    return messages


def _safe_diagnostic(code: Any, stage: Any, http_status: Any = None) -> dict[str, Any] | None:
    if not isinstance(code, str) or code not in _ERROR_CODES:
        return None
    diagnostic = {'code': code, 'stage': stage if isinstance(stage, str) and stage in _ERROR_STAGES else 'unknown'}
    if type(http_status) is int and 100 <= http_status <= 599:
        diagnostic['http_status'] = http_status
    return diagnostic


def _diagnostic(result: CommandResult, *, fallback='acquisition_failed') -> dict[str, Any]:
    """Accept only adapter enums/numeric status; discard all arbitrary text."""
    for message in reversed(_json_messages(result)):
        diagnostic = _safe_diagnostic(message.get('error'), message.get('stage'), message.get('http_status'))
        if diagnostic is not None:
            return diagnostic
    if result.returncode < 0 or result.returncode == 2:
        return {'code': 'tool_crashed', 'stage': 'runner'}
    return _safe_diagnostic(fallback, 'unknown') or {'code': 'acquisition_failed', 'stage': 'unknown'}


def _outcome(result: CommandResult) -> str:
    code = _diagnostic(result)['code']
    if code == 'requires_2fa':
        return 'requires_2fa'
    if code in ('requires_auth', 'account_identity_mismatch', 'credentials_rejected', 'account_disabled', 'authentication_rejected'):
        return 'requires_auth'
    return 'failed'


class AcquisitionWorker:
    def __init__(self, root: Path | str, registry: Any, *, ipatool_path: Path | str = DEFAULT_TOOL, runner: Any = None):
        self.root = Path(root).absolute()
        self.registry = registry
        self.tool = Path(ipatool_path).absolute()
        self.runner = runner or IpatoolRunner()
        _private_directory(self.root)
        for name in ('profiles', 'jobs', 'locks'):
            _private_directory(self.root / name)
        self.database_path = self.root / 'acquisition.sqlite3'
        if self.database_path.exists() or self.database_path.is_symlink():
            _private_file(self.database_path)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, status TEXT NOT NULL,
                    identity_sha256 TEXT, created_at INTEGER NOT NULL,
                    last_error_code TEXT, last_error_stage TEXT, last_http_status INTEGER
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES profiles(id),
                    app_slug TEXT NOT NULL, app_store_id TEXT NOT NULL,
                    expected_bundle_id TEXT NOT NULL, title TEXT NOT NULL,
                    activation_profile_id TEXT NOT NULL, status TEXT NOT NULL,
                    artifact_id TEXT, error_code TEXT, created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(profiles)')}
            for column, kind in (('last_error_code', 'TEXT'), ('last_error_stage', 'TEXT'), ('last_http_status', 'INTEGER')):
                if column not in columns:
                    db.execute(f'ALTER TABLE profiles ADD COLUMN {column} {kind}')
        os.chmod(self.database_path, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('PRAGMA synchronous = FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _lock(self, account_id: str):
        path = self.root / 'locks' / f'{_identifier(account_id)}.lock'
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        try:
            _private_file(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AcquisitionError('account_busy') from exc
            # The subprocess inherits this file description, so a killed Python
            # parent cannot release the lock while an orphan download is alive.
            yield fd
        finally:
            os.close(fd)

    def add_profile(self, account_id: str, *, label: str) -> dict[str, Any]:
        account_id, label = _identifier(account_id), _text(label, 80)
        with self._lock(account_id):
            with self._db() as db:
                if db.execute('SELECT 1 FROM profiles WHERE id=?', (account_id,)).fetchone():
                    raise AcquisitionError('profile_exists')
                path = self.root / 'profiles' / account_id
                _private_directory(path)
                for name in ('state', 'cache'):
                    _private_directory(path / name)
                passphrase = path / 'passphrase'
                if passphrase.exists() or passphrase.is_symlink():
                    raise AcquisitionError('profile_state_exists')
                fd = os.open(passphrase, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    stream.write(secrets.token_urlsafe(48) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                db.execute('INSERT INTO profiles (id,label,status,identity_sha256,created_at) VALUES (?,?,?,?,?)',
                           (account_id, label, 'requires_auth', None, int(time.time())))
        return self.profile(account_id)

    def profile(self, account_id: str) -> dict[str, Any]:
        account_id = _identifier(account_id)
        with self._db() as db:
            row = db.execute('SELECT * FROM profiles WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise AcquisitionError('profile_not_found')
        result = {'id': row['id'], 'label': row['label'], 'status': row['status'],
                  'identity_bound': row['identity_sha256'] is not None, 'created_at': row['created_at'], 'last_error': None}
        if row['last_error_code'] in _ERROR_CODES:
            result['last_error'] = {'code': row['last_error_code'],
                'stage': row['last_error_stage'] if row['last_error_stage'] in _ERROR_STAGES else 'unknown'}
            if type(row['last_http_status']) is int and 100 <= row['last_http_status'] <= 599:
                result['last_error']['http_status'] = row['last_http_status']
        return result

    def profiles(self) -> list[dict[str, Any]]:
        with self._db() as db:
            identifiers = [row[0] for row in db.execute('SELECT id FROM profiles ORDER BY created_at,id')]
        return [self.profile(identifier) for identifier in identifiers]

    def _command(self, account_id: str, arguments: list[str], *, interactive: bool = False,
                 lock_fd: int | None = None) -> CommandResult:
        with self._db() as db:
            profile = db.execute('SELECT * FROM profiles WHERE id=?', (_identifier(account_id),)).fetchone()
        if profile is None:
            raise AcquisitionError('profile_not_found')
        path = self.root / 'profiles' / account_id
        _private_directory(path)
        _private_file(path / 'passphrase')
        for name in ('state', 'cache'):
            _private_directory(path / name)
        _private_directory(path / 'state' / 'ipatool')
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
               'XDG_STATE_HOME': str(path / 'state'), 'XDG_CACHE_HOME': str(path / 'cache')}
        # Preserve the real Unix user's home; never substitute a profile as HOME.
        if 'HOME' in os.environ:
            env['HOME'] = os.environ['HOME']
        # No Apple secret, proxy setting or arbitrary caller environment.
        if profile['identity_sha256']:
            env['APPKI_EXPECTED_ACCOUNT_SHA256'] = profile['identity_sha256']
        argv = [str(self.tool), '--format', 'json', '--keychain-passphrase-file', str(path / 'passphrase')]
        if not interactive:
            argv.append('--non-interactive')
        return self.runner.run(argv + arguments, env=env, interactive=interactive, lock_fd=lock_fd)

    def _profile_status(self, account_id: str, status: str, *, diagnostic: dict[str, Any] | None = None) -> None:
        diagnostic = (_safe_diagnostic(diagnostic.get('code'), diagnostic.get('stage'), diagnostic.get('http_status')) or {}) if diagnostic is not None else {}
        with self._db() as db:
            db.execute('UPDATE profiles SET status=?,last_error_code=?,last_error_stage=?,last_http_status=? WHERE id=?',
                       (status, diagnostic.get('code'), diagnostic.get('stage'), diagnostic.get('http_status'), account_id))

    def login(self, account_id: str) -> dict[str, Any]:
        account_id = _identifier(account_id)
        with self._lock(account_id) as lock_fd:
            self.profile(account_id)
            try:
                result = self._command(account_id, ['auth', 'login'], interactive=True, lock_fd=lock_fd)
            except AcquisitionError as exc:
                if exc.code not in ('tool_timeout', 'tool_unavailable', 'invalid_tool_output'):
                    raise
                self._profile_status(account_id, 'requires_auth', diagnostic={'code': exc.code, 'stage': 'runner'})
                return self.profile(account_id)
            identities = [message.get('account_identity_sha256') for message in _json_messages(result)
                          if message.get('success') is True]
            if result.returncode != 0 or not identities or not isinstance(identities[-1], str) or not _DIGEST.fullmatch(identities[-1]):
                self._profile_status(account_id, _outcome(result) if _outcome(result) != 'failed' else 'requires_auth',
                    diagnostic=_diagnostic(result, fallback='auth_success_unconfirmed' if result.returncode == 0 else 'authentication_failed'))
                return self.profile(account_id)
            identity = identities[-1]
            cached = self._command(account_id, ['auth', 'info'], lock_fd=lock_fd)
            cached_identities = [message.get('account_identity_sha256') for message in _json_messages(cached)
                                 if message.get('success') is True]
            if cached.returncode != 0 or not cached_identities or cached_identities[-1] != identity:
                self._profile_status(account_id, 'requires_auth', diagnostic={'code': 'auth_cache_unconfirmed', 'stage': 'local_storage'})
                raise AcquisitionError('account_identity_mismatch')
            with self._db() as db:
                old_identity = db.execute('SELECT identity_sha256 FROM profiles WHERE id=?', (account_id,)).fetchone()[0]
                if old_identity and old_identity != identity:
                    db.execute('UPDATE profiles SET status=?,last_error_code=?,last_error_stage=?,last_http_status=NULL WHERE id=?',
                               ('requires_auth', 'account_identity_mismatch', 'account_binding', account_id))
                else:
                    db.execute('UPDATE profiles SET identity_sha256=?,status=?,last_error_code=NULL,last_error_stage=NULL,last_http_status=NULL WHERE id=?',
                               (identity, 'ready', account_id))
            if old_identity and old_identity != identity:
                raise AcquisitionError('account_identity_mismatch')
        return self.profile(account_id)

    def enqueue(self, *, app_slug: str, account_id: str, app_store_id: str | int | None = None,
                expected_bundle_id: str | None = None, title: str | None = None,
                activation_profile_id: str | None = None) -> dict[str, Any]:
        app_slug, account_id = _app_slug(app_slug), _identifier(account_id)
        self.profile(account_id)
        activation_profile_id = _identifier(activation_profile_id or account_id)
        definition = self.registry.get_product_definition(app_slug) or {}
        app_store_id = app_store_id if app_store_id is not None else definition.get('app_store_id')
        expected_bundle_id = expected_bundle_id if expected_bundle_id is not None else definition.get('expected_bundle_id')
        title = title if title is not None else definition.get('title')
        if app_store_id is None or expected_bundle_id is None or title is None:
            raise AcquisitionError('product_metadata_required')
        title = _text(title)
        identifier = str(app_store_id)
        if not re.fullmatch(r'[1-9][0-9]{0,18}', identifier) or int(identifier) > 9223372036854775807:
            raise AcquisitionError('invalid_app_store_id')
        if not isinstance(expected_bundle_id, str) or not _BUNDLE.fullmatch(expected_bundle_id) or '.' not in expected_bundle_id:
            raise AcquisitionError('invalid_bundle_id')
        now, job_id = int(time.time()), uuid.uuid4().hex
        with self._db() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, account_id, app_slug, identifier, expected_bundle_id, title,
                        activation_profile_id, 'queued', None, None, now, now))
        return self.job(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise AcquisitionError('invalid_job_id')
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise AcquisitionError('job_not_found')
        return dict(row)

    def jobs(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created_at,id')]

    def _job_status(self, job_id: str, status: str, *, error_code: str | None = None, artifact_id: str | None = None):
        with self._db() as db:
            db.execute('UPDATE jobs SET status=?,error_code=?,artifact_id=?,updated_at=? WHERE id=?',
                       (status, error_code, artifact_id, int(time.time()), job_id))

    def run_job(self, job_id: str) -> dict[str, Any]:
        job = self.job(job_id)
        with self._lock(job['account_id']) as lock_fd:
            # Refetch after the interprocess lock. A crashed running job can safely
            # resume; registry import is deterministic for identical provenance.
            job = self.job(job_id)
            if job['status'] == 'ready':
                return job
            profile = self.profile(job['account_id'])
            blocked = 'requires_auth' if not profile['identity_bound'] else profile['status']
            if blocked in ('requires_auth', 'requires_2fa') and job['status'] != 'downloaded':
                self._job_status(job_id, blocked, error_code=blocked)
                return self.job(job_id)
            directory = self.root / 'jobs' / job_id
            _private_directory(directory)
            output = directory / 'download.ipa'
            if output.is_symlink():
                raise AcquisitionError('unsafe_private_path')
            try:
                if job['status'] != 'downloaded':
                    # Never let upstream's non-interactive "output exists" path
                    # reuse an incomplete attempt. Touch only this job's files.
                    for partial in (output, output.with_name('download.ipa.tmp')):
                        if partial.exists() or partial.is_symlink():
                            info = partial.lstat()
                            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                                raise AcquisitionError('unsafe_private_path')
                            partial.unlink()
                    self._job_status(job_id, 'running')
                    result = self._command(job['account_id'], [
                        'download', '--app-id', job['app_store_id'], '--platform', 'iphone', '--output', str(output),
                    ], lock_fd=lock_fd)
                    success = any(message.get('success') is True and message.get('purchased') is False
                                  and message.get('output') == str(output) for message in _json_messages(result))
                    if result.returncode != 0 or not success or not output.is_file():
                        status = _outcome(result)
                        self._job_status(job_id, status, error_code=_diagnostic(result)['code'])
                        if status in ('requires_auth', 'requires_2fa'):
                            self._profile_status(job['account_id'], status, diagnostic=_diagnostic(result))
                        return self.job(job_id)
                    os.chmod(output, 0o600)
                    self._job_status(job_id, 'downloaded')
                _private_file(output)
                record = self.registry.import_ipa(
                    output, app_slug=job['app_slug'], title=job['title'],
                    expected_bundle_id=job['expected_bundle_id'], source_account_id=job['account_id'],
                    activation_profile_id=job['activation_profile_id'],
                )
                self._job_status(job_id, 'ready', artifact_id=record['id'])
                self._profile_status(job['account_id'], 'ready')
            except AcquisitionError as exc:
                self._job_status(job_id, 'failed', error_code=exc.code)
            except (ValueError, OSError, KeyError) as exc:
                # IPA/import validation messages are not retained verbatim either.
                self._job_status(job_id, 'failed', error_code='artifact_validation_failed')
        return self.job(job_id)

    def status(self) -> dict[str, Any]:
        with self._db() as db:
            counts = {row[0]: row[1] for row in db.execute('SELECT status,count(*) FROM jobs GROUP BY status')}
        return {'root': str(self.root), 'tool_installed': self.tool.is_file() and os.access(self.tool, os.X_OK),
                'adapter_version': IPATOOL_ADAPTER_VERSION, 'upstream_commit': IPATOOL_COMMIT,
                'profiles': len(self.profiles()), 'jobs': counts}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Private IPA loader; login is manual, publishing is explicit.')
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--ipatool', type=Path, default=DEFAULT_TOOL)
    parser.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument('--artifact-root', type=Path, default=DEFAULT_ARTIFACT_ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    apps = commands.add_parser('apps').add_subparsers(dest='action', required=True)
    apps.add_parser('list')
    configure = apps.add_parser('configure')
    configure.add_argument('app_slug')
    configure.add_argument('--title')
    configure.add_argument('--app-store-id', type=int)
    configure.add_argument('--bundle-id')
    configure.add_argument('--price-rub')
    configure.add_argument('--definition', type=Path, help='Optional strict product JSON definition file.')
    profiles = commands.add_parser('profiles').add_subparsers(dest='action', required=True)
    profiles.add_parser('list')
    profile_add = profiles.add_parser('add')
    profile_add.add_argument('account_id')
    profile_add.add_argument('--label', required=True)
    profile_login = profiles.add_parser('login')
    profile_login.add_argument('account_id')
    jobs = commands.add_parser('jobs').add_subparsers(dest='action', required=True)
    jobs.add_parser('list')
    enqueue = jobs.add_parser('enqueue')
    enqueue.add_argument('--app-slug', required=True)
    enqueue.add_argument('--app-store-id')
    enqueue.add_argument('--bundle-id')
    enqueue.add_argument('--title')
    enqueue.add_argument('--account', required=True)
    enqueue.add_argument('--activation-profile')
    run = jobs.add_parser('run')
    run.add_argument('job_id')
    importer = commands.add_parser('import')
    importer.add_argument('source', type=Path)
    importer.add_argument('--app-slug', required=True)
    importer.add_argument('--bundle-id', required=True)
    importer.add_argument('--title', required=True)
    importer.add_argument('--source-account')
    importer.add_argument('--activation-profile')
    publish = commands.add_parser('publish')
    publish.add_argument('app_slug')
    publish.add_argument('artifact_id')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        from .catalog import ArtifactRegistry
        registry = ArtifactRegistry(args.registry, args.artifact_root)
        worker = AcquisitionWorker(args.root, registry, ipatool_path=args.ipatool)
        if args.command == 'status':
            result = worker.status()
        elif args.command == 'apps':
            if args.action == 'list':
                result = registry.product_definitions()
            else:
                definition = {}
                if args.definition is not None:
                    if args.definition.stat().st_size > 65536:
                        raise AcquisitionError('invalid_metadata')
                    definition = json.loads(args.definition.read_text(encoding='utf-8'))
                    if not isinstance(definition, dict):
                        raise AcquisitionError('invalid_metadata')
                for name, value in (('title', args.title), ('app_store_id', args.app_store_id),
                                    ('expected_bundle_id', args.bundle_id), ('price_rub', args.price_rub)):
                    if value is not None:
                        definition[name] = value
                registry.set_product(_app_slug(args.app_slug), definition)
                result = {'success': True, 'app_slug': args.app_slug,
                          'definition': registry.get_product_definition(args.app_slug)}
        elif args.command == 'profiles':
            if args.action == 'add':
                result = worker.add_profile(args.account_id, label=args.label)
            elif args.action == 'login':
                result = worker.login(args.account_id)
            else:
                result = worker.profiles()
        elif args.command == 'jobs':
            if args.action == 'enqueue':
                result = worker.enqueue(app_slug=args.app_slug, app_store_id=args.app_store_id,
                    expected_bundle_id=args.bundle_id, title=args.title, account_id=args.account,
                    activation_profile_id=args.activation_profile)
            elif args.action == 'run':
                result = worker.run_job(args.job_id)
            else:
                result = worker.jobs()
        elif args.command == 'import':
            result = registry.import_ipa(args.source, app_slug=_app_slug(args.app_slug), title=_text(args.title),
                expected_bundle_id=args.bundle_id, source_account_id=args.source_account,
                activation_profile_id=args.activation_profile)
        else:
            registry.publish(_app_slug(args.app_slug), args.artifact_id)
            result = {'success': True, 'app_slug': args.app_slug, 'artifact_id': args.artifact_id}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if ((args.command == 'profiles' and args.action == 'login')
                or (args.command == 'jobs' and args.action == 'run')):
            return 0 if result.get('status') == 'ready' else 1
        return 0
    except AcquisitionError as exc:
        print(json.dumps({'success': False, 'error': exc.code}))
    except (ValueError, OSError, sqlite3.Error):
        print(json.dumps({'success': False, 'error': 'loader_operation_failed'}))
    except KeyboardInterrupt:
        print(json.dumps({'success': False, 'error': 'interrupted'}))
        return 130
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
