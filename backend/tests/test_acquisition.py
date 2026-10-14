import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import pty
import select
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile

import pytest

from appki.acquisition import AcquisitionError, AcquisitionWorker, CommandResult, main
from appki.catalog import ArtifactRegistry


IDENTITY = hashlib.sha256(b'owner@example.invalid').hexdigest()
OTHER_IDENTITY = hashlib.sha256(b'other@example.invalid').hexdigest()


def ipa_bytes(bundle='com.example.test'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('Payload/Example.app/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': bundle, 'CFBundleShortVersionString': '1.2.3',
            'CFBundleVersion': '123', 'MinimumOSVersion': '15.0',
        }))
        archive.writestr('Payload/Example.app/Example', b'unchanged fixture executable')
    return stream.getvalue()


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.override = None
        self.identity = IDENTITY

    def run(self, argv, *, env, interactive=False, lock_fd=None):
        self.calls.append((argv, env, interactive))
        if lock_fd is not None:
            assert stat.S_ISREG(os.fstat(lock_fd).st_mode)
        if self.override:
            result = self.override(argv, env, interactive)
            if result is not None:
                return result
        if 'auth' in argv:
            return CommandResult(0, json.dumps({'success': True, 'account_identity_sha256': self.identity}))
        output = Path(argv[argv.index('--output') + 1])
        output.write_bytes(ipa_bytes())
        return CommandResult(0, json.dumps({'success': True, 'purchased': False, 'output': str(output)}))


@pytest.fixture
def setup(tmp_path):
    registry = ArtifactRegistry(tmp_path / 'shared/registry.json', tmp_path / 'shared')
    registry.set_product('example', {'title': 'Example', 'app_store_id': 123,
        'expected_bundle_id': 'com.example.test', 'price_rub': '490.00'})
    runner = FakeRunner()
    worker = AcquisitionWorker(tmp_path / 'private', registry, ipatool_path=tmp_path / 'tool', runner=runner)
    worker.add_profile('owner', label='Owner')
    return worker, registry, runner


def queued(worker):
    return worker.enqueue(app_slug='example', account_id='owner')


def test_private_profile_and_public_status_do_not_disclose_secrets(setup):
    worker, _, runner = setup
    passphrase = worker.root / 'profiles/owner/passphrase'
    assert stat.S_IMODE(passphrase.stat().st_mode) == 0o600
    assert stat.S_IMODE(worker.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(worker.database_path.stat().st_mode) == 0o600
    assert len(passphrase.read_text().strip()) >= 32
    public = json.dumps([worker.status(), worker.profiles(), worker.jobs()])
    assert passphrase.read_text().strip() not in public
    assert IDENTITY not in public
    assert runner.calls == []


@pytest.mark.parametrize('identifier', ['../owner', 'a/b', '-x', 'x;touch nope', 'x\nsecret'])
def test_invalid_profile_path_never_runs_tool(setup, identifier):
    worker, _, runner = setup
    with pytest.raises(AcquisitionError, match='invalid_identifier'):
        worker.add_profile(identifier, label='Owner')
    assert runner.calls == []


def test_profile_recreation_never_rotates_existing_passphrase(setup):
    worker, _, _ = setup
    before = (worker.root / 'profiles/owner/passphrase').read_bytes()
    with pytest.raises(AcquisitionError, match='profile_exists'):
        worker.add_profile('owner', label='Other')
    assert (worker.root / 'profiles/owner/passphrase').read_bytes() == before


def test_two_accounts_have_separate_state_cache_and_passphrase(setup):
    worker, _, runner = setup
    worker.add_profile('second', label='Second')
    worker.login('owner')
    worker.login('second')
    first, second = runner.calls[0], runner.calls[2]
    assert first[1]['XDG_STATE_HOME'] != second[1]['XDG_STATE_HOME']
    assert first[1]['XDG_CACHE_HOME'] != second[1]['XDG_CACHE_HOME']
    assert first[0][first[0].index('--keychain-passphrase-file') + 1] != second[0][second[0].index('--keychain-passphrase-file') + 1]
    assert first[1].get('HOME') == os.environ.get('HOME')
    assert '--password' not in first[0] and '--auth-code' not in first[0]
    assert '--keychain-passphrase' not in first[0]
    assert not any('PROXY' in name for name in first[1])


def test_auth_exit_zero_without_success_identity_is_not_login(setup):
    worker, _, runner = setup
    runner.override = lambda *args: CommandResult(0, json.dumps({'message': '2FA code is required'}))
    profile = worker.login('owner')
    assert profile['status'] == 'requires_auth'
    assert profile['identity_bound'] is False


def test_login_normalized_2fa_state_and_raw_errors_not_persisted(setup):
    worker, _, runner = setup
    runner.override = lambda *args: CommandResult(1, json.dumps({'error': 'requires_2fa'}), 'password=TOP-SECRET token=SECRET')
    assert worker.login('owner')['status'] == 'requires_2fa'
    assert b'TOP-SECRET' not in worker.database_path.read_bytes()
    assert b'SECRET' not in worker.database_path.read_bytes()


def test_account_switch_is_rejected_and_binding_never_changes(setup):
    worker, _, runner = setup
    assert worker.login('owner')['identity_bound'] is True
    runner.identity = OTHER_IDENTITY
    with pytest.raises(AcquisitionError, match='account_identity_mismatch'):
        worker.login('owner')
    with sqlite3.connect(worker.database_path) as db:
        assert db.execute('SELECT identity_sha256 FROM profiles WHERE id=?', ('owner',)).fetchone()[0] == IDENTITY
    assert runner.calls[-2][1]['APPKI_EXPECTED_ACCOUNT_SHA256'] == IDENTITY
    assert worker.profile('owner')['status'] == 'requires_auth'


def test_login_and_cached_identity_must_match(setup):
    worker, _, runner = setup
    runner.override = lambda argv, *args: CommandResult(0, json.dumps({
        'success': True, 'account_identity_sha256': OTHER_IDENTITY,
    })) if argv[-1] == 'info' else None
    with pytest.raises(AcquisitionError, match='account_identity_mismatch'):
        worker.login('owner')
    assert worker.profile('owner')['identity_bound'] is False


def test_job_before_manual_login_stops_without_apple_calls(setup):
    worker, registry, runner = setup
    job = queued(worker)
    result = worker.run_job(job['id'])
    assert result['status'] == 'requires_auth'
    assert registry.active('example') is None
    assert runner.calls == []


def test_download_import_provenance_and_exact_rerun_no_duplicate_or_purchase(setup):
    worker, registry, runner = setup
    worker.login('owner')
    job = queued(worker)
    result = worker.run_job(job['id'])
    assert result['status'] == 'ready'
    artifact = registry.get(result['artifact_id'])
    assert artifact['source_account_id'] == 'owner'
    assert artifact['activation_profile_id'] == 'owner'
    assert artifact['bundle_id'] == 'com.example.test'
    assert Path(artifact['path']).read_bytes() == ipa_bytes()
    assert registry.active('example') is None
    before_calls = len(runner.calls)
    assert worker.run_job(job['id']) == result
    assert len(runner.calls) == before_calls
    assert len(registry.read()['artifacts']) == 1
    download = runner.calls[-1]
    assert '--purchase' not in download[0]
    assert download[2] is False and '--non-interactive' in download[0]
    assert download[1]['APPKI_EXPECTED_ACCOUNT_SHA256'] == IDENTITY
    assert (worker.root / 'profiles/owner/passphrase').read_text().strip() not in json.dumps(download)


def test_job_downloaded_checkpoint_reimports_without_process(setup):
    worker, registry, runner = setup
    worker.login('owner')
    job = queued(worker)
    directory = worker.root / 'jobs' / job['id']
    directory.mkdir(mode=0o700)
    output = directory / 'download.ipa'
    output.write_bytes(ipa_bytes())
    output.chmod(0o600)
    record = registry.import_ipa(output, app_slug='example', title='Example', expected_bundle_id='com.example.test',
        source_account_id='owner', activation_profile_id='owner')
    worker._job_status(job['id'], 'downloaded')
    before = len(runner.calls)
    assert worker.run_job(job['id'])['artifact_id'] == record['id']
    assert len(runner.calls) == before
    assert len(registry.read()['artifacts']) == 1


def test_failed_partial_retry_cleans_only_own_output(setup):
    worker, _, runner = setup
    worker.login('owner')
    job = queued(worker)
    directory = worker.root / 'jobs' / job['id']
    directory.mkdir(mode=0o700)
    output = directory / 'download.ipa'
    output.write_bytes(b'incomplete')
    output.with_name('download.ipa.tmp').write_bytes(b'incomplete temporary')
    untouched = directory / 'operator-note'
    untouched.write_text('keep')
    worker._job_status(job['id'], 'failed')
    def ensure_clean(argv, *args):
        if 'download' in argv:
            assert not output.exists()
            assert not output.with_name('download.ipa.tmp').exists()
        return None
    runner.override = ensure_clean
    assert worker.run_job(job['id'])['status'] == 'ready'
    assert untouched.read_text() == 'keep'


def test_wrong_bundle_is_not_imported_or_published(setup):
    worker, registry, runner = setup
    worker.login('owner')
    job = queued(worker)
    def wrong_bundle(argv, *args):
        output = Path(argv[argv.index('--output') + 1])
        output.write_bytes(ipa_bytes('com.example.other'))
        return CommandResult(0, json.dumps({'success': True, 'purchased': False, 'output': str(output)}))
    runner.override = wrong_bundle
    assert worker.run_job(job['id'])['error_code'] == 'artifact_validation_failed'
    assert registry.read()['artifacts'] == {}
    assert registry.active('example') is None


@pytest.mark.parametrize('payload', [{'app_slug': 'bad_slug'}, {'app_store_id': '1;touch /tmp/pwn'},
    {'app_store_id': -3}, {'expected_bundle_id': 'com..example'}, {'title': 'secret\ntext'}])
def test_invalid_job_payload_has_no_side_effects(setup, payload):
    worker, _, runner = setup
    arguments = dict(app_slug='example', account_id='owner', app_store_id=123,
                     expected_bundle_id='com.example.test', title='Example')
    arguments.update(payload)
    with pytest.raises((AcquisitionError, ValueError)):
        worker.enqueue(**arguments)
    assert worker.jobs() == []
    assert runner.calls == []


def test_unconfigured_planned_app_is_not_guessed(setup):
    worker, _, runner = setup
    with pytest.raises(AcquisitionError, match='product_metadata_required'):
        worker.enqueue(app_slug='v2raytun', account_id='owner')
    assert runner.calls == []


def test_per_account_lock_blocks_same_profile_and_allows_other_profile(setup):
    worker, _, runner = setup
    worker.add_profile('other', label='Other')
    worker.login('owner')
    worker.login('other')
    same = queued(worker)
    other = worker.enqueue(app_slug='example', account_id='other')
    with worker._lock('owner'):
        with pytest.raises(AcquisitionError, match='account_busy'):
            worker.run_job(same['id'])
        assert worker.run_job(other['id'])['status'] == 'ready'
    assert worker.job(same['id'])['status'] == 'queued'


def test_orphan_child_retains_account_lock_until_exit(setup):
    worker, _, _ = setup
    child = None
    try:
        with worker._lock('owner') as descriptor:
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                pass_fds=(descriptor,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        # Simulate the parent closing its FD after a crash: the child still has it.
        with pytest.raises(AcquisitionError, match='account_busy'):
            with worker._lock('owner'):
                pass
        child.terminate()
        child.wait(timeout=5)
        with worker._lock('owner'):
            pass
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


def test_private_state_symlink_and_world_readable_passphrase_rejected(setup, tmp_path):
    worker, _, runner = setup
    phrase = worker.root / 'profiles/owner/passphrase'
    phrase.chmod(0o644)
    with pytest.raises(AcquisitionError, match='unsafe_private_permissions'):
        worker.login('owner')
    phrase.chmod(0o600)
    state = worker.root / 'profiles/owner/state/ipatool'
    state.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AcquisitionError, match='unsafe_private_path'):
        worker.login('owner')
    assert runner.calls == []


def test_job_2fa_is_durable_and_no_raw_log_is_retained(setup):
    worker, registry, runner = setup
    worker.login('owner')
    job = queued(worker)
    runner.override = lambda *args: CommandResult(1, json.dumps({'error': 'requires_2fa'}), 'SECRET-APPLE-PASSWORD')
    assert worker.run_job(job['id'])['status'] == 'requires_2fa'
    reopened = AcquisitionWorker(worker.root, registry, runner=runner)
    assert reopened.job(job['id'])['status'] == 'requires_2fa'
    assert b'SECRET-APPLE-PASSWORD' not in worker.database_path.read_bytes()
    assert registry.active('example') is None
    before_calls = len(runner.calls)
    assert reopened.run_job(job['id'])['status'] == 'requires_2fa'
    assert len(runner.calls) == before_calls


def test_interactive_runner_preserves_tty_and_captures_only_safe_json(tmp_path):
    """Offline stand-in for the upstream stderr prompt + masked TTY reader."""
    helper = tmp_path / 'prompt.py'
    helper.write_text('''import json, os, sys, termios
assert os.isatty(0)
before = termios.tcgetattr(0)
masked = termios.tcgetattr(0)
masked[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, masked)
os.write(2, b"enter private password: ")
value = sys.stdin.readline().strip()
termios.tcsetattr(0, termios.TCSANOW, before)
print(json.dumps({"success": value == "offline-fixture-secret"}))
''')
    driver = tmp_path / 'driver.py'
    driver.write_text('''import json, os, sys
from appki.acquisition import IpatoolRunner
result = IpatoolRunner().run([sys.executable, sys.argv[1]],
    env={"PATH": "/usr/bin:/bin"}, interactive=True)
print(json.dumps({"returncode": result.returncode, "stdout": result.stdout}))
''')
    master, slave = pty.openpty()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child = subprocess.Popen([sys.executable, str(driver), str(helper)], stdin=slave,
        stderr=slave, stdout=subprocess.PIPE, env=env)
    os.close(slave)
    observed = b''
    try:
        deadline = time.monotonic() + 10
        while b'enter private password: ' not in observed:
            assert time.monotonic() < deadline
            if select.select([master], [], [], 0.1)[0]:
                observed += os.read(master, 4096)
        os.write(master, b'offline-fixture-secret\n')
        output, _ = child.communicate(timeout=10)
        assert child.returncode == 0
        result = json.loads(output)
        assert result['returncode'] == 0
        assert json.loads(result['stdout']) == {'success': True}
        assert b'enter private password' not in output
        assert b'offline-fixture-secret' not in output + observed
    finally:
        os.close(master)
        if child.poll() is None:
            child.kill()
            child.wait()


def test_interactive_timeout_restores_operator_terminal(tmp_path):
    helper = tmp_path / 'timeout.py'
    helper.write_text('''import os, termios, time
settings = termios.tcgetattr(0)
settings[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, settings)
os.write(2, b"waiting for private input: ")
time.sleep(30)
''')
    driver = tmp_path / 'driver.py'
    driver.write_text('''import json, subprocess, sys, termios
from appki.acquisition import AcquisitionError, IpatoolRunner
original = termios.tcgetattr(0)
real_run = subprocess.run
def fast_run(*args, **kwargs):
    kwargs["timeout"] = 0.5
    return real_run(*args, **kwargs)
subprocess.run = fast_run
try:
    IpatoolRunner().run([sys.executable, sys.argv[1]], env={"PATH": "/usr/bin:/bin"}, interactive=True)
    raise AssertionError("timeout expected")
except AcquisitionError as exc:
    assert exc.code == "tool_timeout"
print(json.dumps({"restored": termios.tcgetattr(0) == original}))
''')
    master, slave = pty.openpty()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child = subprocess.Popen([sys.executable, str(driver), str(helper)], stdin=slave,
        stderr=slave, stdout=subprocess.PIPE, env=env)
    os.close(slave)
    try:
        output, _ = child.communicate(timeout=10)
        assert child.returncode == 0
        assert json.loads(output) == {'restored': True}
    finally:
        os.close(master)
        if child.poll() is None:
            child.kill()
            child.wait()


def test_cli_configure_import_publish_is_explicit(setup, tmp_path, capsys):
    worker, registry, runner = setup
    source = tmp_path / 'source.ipa'
    source.write_bytes(ipa_bytes())
    base = ['--root', str(worker.root), '--registry', str(registry.registry_path),
            '--artifact-root', str(registry.artifact_root)]
    assert main(base + ['apps', 'configure', 'example', '--title', 'Example',
        '--app-store-id', '123', '--bundle-id', 'com.example.test', '--price-rub', '490']) == 0
    assert json.loads(capsys.readouterr().out)['success'] is True
    assert main(base + ['import', str(source), '--app-slug', 'example', '--bundle-id', 'com.example.test',
        '--title', 'Example', '--source-account', 'owner', '--activation-profile', 'owner']) == 0
    record = json.loads(capsys.readouterr().out)
    assert registry.active('example') is None
    assert main(base + ['publish', 'example', record['id']]) == 0
    assert json.loads(capsys.readouterr().out) == {'success': True, 'app_slug': 'example', 'artifact_id': record['id']}
    assert registry.active('example')['id'] == record['id']
    assert runner.calls == []


def test_cli_failed_run_and_unproved_login_exit_nonzero(setup, monkeypatch, capsys):
    worker, registry, runner = setup
    base = ['--root', str(worker.root), '--registry', str(registry.registry_path),
            '--artifact-root', str(registry.artifact_root)]
    job = queued(worker)
    assert main(base + ['jobs', 'run', job['id']]) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'requires_auth'
    monkeypatch.setattr('appki.acquisition.IpatoolRunner', lambda: runner)
    runner.override = lambda *args: CommandResult(0, '{}')
    assert main(base + ['profiles', 'login', 'owner']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'requires_auth'
