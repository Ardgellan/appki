from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
from types import SimpleNamespace
import zipfile

import pytest

from appki.catalog import ArtifactRegistry, CatalogError, legacy_artifact, load_products, resolve_artifact
from appki.ota import IPAFile


def make_ipa(path, *, bundle='su.ffg.happ', version='1.0', build='1', minimum='15.0'):
    info = {'CFBundleIdentifier': bundle, 'CFBundleShortVersionString': version,
            'CFBundleVersion': build, 'MinimumOSVersion': minimum}
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Payload/Fixture.app/Info.plist', plistlib.dumps(info))
        archive.writestr('Payload/Fixture.app/Fixture', ('synthetic-' + version + build).encode())
    return path


@pytest.fixture
def registry(tmp_path):
    return ArtifactRegistry(tmp_path / 'private' / 'registry.json', tmp_path / 'private' / 'ipa')


def import_happ(registry, path, **extra):
    return registry.import_ipa(path, app_slug='happ', title='Happ', expected_bundle_id='su.ffg.happ', **extra)


def write_registry(registry, value):
    registry.registry_path.write_text(json.dumps(value), encoding='utf-8')


def settings_for(path, **extra):
    return SimpleNamespace(happ_ipa_path=path, happ_ipa_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                           happ_price_rub='1.00', catalog_definition_path=None, **extra)


def test_empty_registry_read_never_creates_runtime_files(registry, tmp_path):
    assert registry.read() == {'schema_version': 1, 'artifacts': {}, 'active': {}, 'products': {}}
    assert registry.active('happ') is None
    assert registry.get('ipa-' + '0' * 64) is None
    assert not (tmp_path / 'private').exists()


def test_import_has_checked_bytes_permissions_and_idempotent_provenance(registry, tmp_path):
    source = make_ipa(tmp_path / 'source.ipa')
    record = import_happ(registry, source, source_account_id='account-a', activation_profile_id='profile-a')
    destination = Path(record['path'])
    assert destination.read_bytes() == source.read_bytes()
    assert record['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert destination == registry.artifact_root / 'happ' / (record['sha256'] + '.ipa')
    assert stat.S_IMODE(destination.stat().st_mode) == 0o440
    assert stat.S_IMODE(destination.parent.stat().st_mode) & 0o777 == 0o750
    assert stat.S_IMODE(registry.registry_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(registry.lock_path.stat().st_mode) == 0o640
    assert record['source_account_id'] == 'account-a'
    assert record['activation_profile_id'] == 'profile-a'
    assert resolve_artifact(record).available
    assert registry.active('happ') is None
    assert import_happ(registry, source, source_account_id='account-a', activation_profile_id='profile-a') == record
    second = import_happ(registry, source, source_account_id='account-b', activation_profile_id='profile-b')
    assert second['id'] != record['id']
    assert second['path'] == record['path']
    assert len(registry.read()['artifacts']) == 2


def test_new_artifact_directories_preserve_setgid_where_supported(registry, tmp_path):
    probe = tmp_path / 'setgid-probe'
    probe.mkdir()
    probe.chmod(0o2750)
    if not probe.stat().st_mode & stat.S_ISGID:
        pytest.skip('This filesystem strips setgid directory bits even after chmod.')
    record = import_happ(registry, make_ipa(tmp_path / 'source.ipa'))
    assert stat.S_IMODE(Path(record['path']).parent.stat().st_mode) == 0o2750
    assert stat.S_IMODE(registry.artifact_root.stat().st_mode) == 0o2750


def test_import_rejects_wrong_bundle_and_malformed_ipa_without_publication(registry, tmp_path):
    wrong = make_ipa(tmp_path / 'wrong.ipa', bundle='com.example.wrong')
    with pytest.raises(CatalogError):
        import_happ(registry, wrong)
    invalid = tmp_path / 'invalid.ipa'
    invalid.write_bytes(b'not-an-ipa')
    with pytest.raises(CatalogError):
        import_happ(registry, invalid)
    assert registry.read()['artifacts'] == {}
    assert not list(registry.artifact_root.glob('*/*.ipa'))
    assert not list(registry.artifact_root.glob('*/.import-*'))


@pytest.mark.parametrize('info', [
    ['not-a-dictionary'],
    {'CFBundleIdentifier': 'su.ffg.happ'},
    {'CFBundleIdentifier': 'su.ffg.happ', 'CFBundleShortVersionString': 'bad version',
     'CFBundleVersion': '1'},
    {'CFBundleIdentifier': 'su.ffg.happ', 'CFBundleShortVersionString': '1.0',
     'CFBundleVersion': '1', 'MinimumOSVersion': '../15'},
])
def test_invalid_info_fields_never_enter_the_registry(registry, tmp_path, info):
    source = tmp_path / 'source.ipa'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('Payload/Fixture.app/Info.plist', plistlib.dumps(info))
    with pytest.raises(CatalogError):
        import_happ(registry, source)
    assert registry.read()['artifacts'] == {}
    assert not list(registry.artifact_root.glob('*/*.ipa'))


def test_wrong_existing_hash_is_never_overwritten(registry, tmp_path):
    source = make_ipa(tmp_path / 'source.ipa')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    directory = registry.artifact_root / 'happ'
    directory.mkdir(parents=True)
    destination = directory / (digest + '.ipa')
    destination.write_bytes(b'corrupt-existing-file')
    destination.chmod(0o440)
    with pytest.raises(CatalogError):
        import_happ(registry, source)
    assert destination.read_bytes() == b'corrupt-existing-file'
    assert registry.read()['artifacts'] == {}


def test_publication_retains_every_old_version_and_exact_app(registry, tmp_path):
    first = import_happ(registry, make_ipa(tmp_path / 'first.ipa', version='1.0', build='11'),
                        source_account_id='account-a', activation_profile_id='profile-a')
    second = import_happ(registry, make_ipa(tmp_path / 'second.ipa', version='2.0', build='22'),
                         source_account_id='account-b', activation_profile_id='profile-b')
    registry.publish('happ', first['id'])
    assert registry.active('happ') == first
    registry.publish('happ', second['id'])
    assert registry.active('happ') == second
    assert registry.get(first['id']) == first
    assert resolve_artifact(first).version == '1.0'
    assert resolve_artifact(second).version == '2.0'
    assert registry.active('v2raytun') is None
    with pytest.raises(CatalogError):
        registry.publish('v2raytun', first['id'])
    with pytest.raises(CatalogError):
        registry.publish('happ', 'ipa-' + '0' * 64)


def test_corrupt_bytes_or_forged_metadata_cannot_be_published(registry, tmp_path):
    record = import_happ(registry, make_ipa(tmp_path / 'source.ipa'))
    path = Path(record['path'])
    original = path.read_bytes()
    path.chmod(0o640)
    path.write_bytes(b'corrupt')
    path.chmod(0o440)
    assert not resolve_artifact(record).available
    with pytest.raises(CatalogError):
        registry.publish('happ', record['id'])
    assert registry.read()['active'] == {}
    path.chmod(0o640)
    path.write_bytes(original)
    path.chmod(0o440)
    value = registry.read()
    value['artifacts'][record['id']]['version'] = '99.0'
    write_registry(registry, value)
    with pytest.raises(CatalogError):
        registry.publish('happ', record['id'])
    value['artifacts'][record['id']]['sha256'] = '0' * 64
    write_registry(registry, value)
    with pytest.raises(CatalogError):
        registry.get(record['id'])


def test_product_definition_is_a_strict_secret_free_whitelist(registry):
    registry.set_product('happ', {'price_rub': '12.5', 'app_store_id': 12345})
    definition = registry.get_product_definition('happ')
    assert definition['price_rub'] == '12.50'
    assert definition['expected_bundle_id'] == 'su.ffg.happ'
    assert definition['app_store_id'] == 12345
    assert len(registry.product_definitions()) == 7
    for key in ('password', 'apple_id', 'credentials', 'source_account_id', 'ipa'):
        with pytest.raises(CatalogError) as error:
            registry.set_product('happ', {key: 'never-persist-this-secret'})
        assert 'never-persist-this-secret' not in str(error.value)
    for definition in ({'app_store_id': True}, {'app_store_id': -1},
                       {'expected_bundle_id': '../escape'}, {'features': [{'password': 'secret'}]},
                       {'coming_soon': 'false'}, {'price_rub': 'NaN'}, {'price_rub': '0'}):
        with pytest.raises(CatalogError):
            registry.set_product('happ', definition)
    text = registry.registry_path.read_text()
    assert 'never-persist-this-secret' not in text
    assert 'credentials' not in text


def test_record_cannot_contain_credentials_or_profile_values_that_are_secrets(registry, tmp_path):
    source = make_ipa(tmp_path / 'source.ipa')
    for profile in ('user@example.com', '../account', 'space separated password'):
        with pytest.raises(CatalogError):
            import_happ(registry, source, source_account_id=profile)
    record = import_happ(registry, source)
    value = registry.read()
    value['artifacts'][record['id']]['password'] = 'private-sentinel'
    write_registry(registry, value)
    with pytest.raises(CatalogError) as error:
        registry.get(record['id'])
    assert 'private-sentinel' not in str(error.value)


@pytest.mark.parametrize('slug', ['../outside', 'happ/child', 'happ..', '@happ', 'UPPER', ''])
def test_slug_path_escape_is_rejected(registry, tmp_path, slug):
    source = make_ipa(tmp_path / 'source.ipa')
    with pytest.raises(CatalogError):
        registry.import_ipa(source, app_slug=slug, title='Happ', expected_bundle_id='su.ffg.happ')
    assert not registry.registry_path.exists()


def test_symlink_source_destination_or_registry_is_rejected(registry, tmp_path):
    source = make_ipa(tmp_path / 'source.ipa')
    link = tmp_path / 'source-link.ipa'
    link.symlink_to(source)
    with pytest.raises(CatalogError):
        import_happ(registry, link)
    outside = tmp_path / 'outside'
    outside.mkdir()
    registry.artifact_root.mkdir(parents=True)
    (registry.artifact_root / 'happ').symlink_to(outside, target_is_directory=True)
    with pytest.raises(CatalogError):
        import_happ(registry, source)
    assert list(outside.iterdir()) == []
    (registry.artifact_root / 'happ').unlink()
    record = import_happ(registry, source)
    destination = Path(record['path'])
    destination.unlink()
    destination.symlink_to(source)
    with pytest.raises(CatalogError):
        registry.get(record['id'])
    assert source.read_bytes() == make_ipa(tmp_path / 'comparison.ipa').read_bytes()
    other_metadata = tmp_path / 'outside.json'
    other_metadata.write_text('{}')
    registry.registry_path.unlink()
    registry.registry_path.symlink_to(other_metadata)
    with pytest.raises(CatalogError):
        registry.read()
    assert other_metadata.read_text() == '{}'


def test_linked_lock_never_changes_the_outside_file_permissions(registry, tmp_path):
    outside = tmp_path / 'outside-lock'
    outside.write_text('outside')
    outside.chmod(0o600)
    registry.registry_path.parent.mkdir()
    os.link(outside, registry.lock_path)
    with pytest.raises(CatalogError):
        registry.set_product('happ', {'price_rub': '490.00'})
    assert outside.read_text() == 'outside'
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert not registry.registry_path.exists()


def test_record_path_must_equal_the_hash_bound_managed_location(registry, tmp_path):
    record = import_happ(registry, make_ipa(tmp_path / 'source.ipa'))
    pristine = registry.read()
    for escaped in (str(tmp_path / 'outside.ipa'), '../outside.ipa',
                    str(registry.artifact_root / 'happ' / 'not-the-hash.ipa')):
        forged = copy.deepcopy(pristine)
        forged['artifacts'][record['id']]['path'] = escaped
        write_registry(registry, forged)
        with pytest.raises(CatalogError):
            registry.get(record['id'])


def test_pinned_snapshot_cannot_resolve_through_a_replaced_directory_link(registry, tmp_path):
    record = import_happ(registry, make_ipa(tmp_path / 'source.ipa'))
    original = registry.artifact_root / 'happ'
    moved = tmp_path / 'moved'
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(CatalogError):
        resolve_artifact(record)
    with pytest.raises(CatalogError):
        registry.get(record['id'])


def test_concurrent_writers_keep_all_artifacts_and_product_updates(registry, tmp_path):
    source = make_ipa(tmp_path / 'source.ipa')

    def worker(index):
        own = ArtifactRegistry(registry.registry_path, registry.artifact_root)
        record = import_happ(own, source, source_account_id=f'account-{index}', activation_profile_id=f'profile-{index}')
        own.set_product(f'future-{index}', {'title': f'Future {index}', 'coming_soon': True})
        return record

    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(worker, range(6)))
    value = registry.read()
    assert len(value['artifacts']) == 6
    assert len(value['products']) == 6
    assert len({record['id'] for record in records}) == 6
    assert len({record['path'] for record in records}) == 1
    assert value['active'] == {}
    assert not list(registry.registry_path.parent.glob('.registry-*'))


def test_custom_product_requires_configuration_and_matching_bundle_before_publish(registry, tmp_path):
    record = registry.import_ipa(make_ipa(tmp_path / 'new.ipa', bundle='com.example.new'),
                                 app_slug='new-app', title='New App', expected_bundle_id='com.example.new')
    with pytest.raises(CatalogError):
        registry.publish('new-app', record['id'])
    registry.set_product('new-app', {'title': 'New App', 'expected_bundle_id': 'com.example.other'})
    with pytest.raises(CatalogError):
        registry.publish('new-app', record['id'])
    registry.set_product('new-app', {'expected_bundle_id': 'com.example.new'})
    registry.publish('new-app', record['id'])
    definition = registry.get_product_definition('new-app')
    assert definition['coming_soon'] is False
    assert definition['price_rub'] == '490.00'


def test_default_products_legacy_fallback_and_unpublished_ipa(registry, tmp_path):
    source = make_ipa(tmp_path / 'legacy.ipa')
    settings = settings_for(source)
    products = load_products(settings, registry)
    assert len(products) == 7
    assert products['happ']['price_rub'] == '1.00'
    assert products['happ']['ipa'].available
    assert products['happ']['artifact']['source_account_id'] is None
    assert products['happ']['artifact']['activation_profile_id'] == 'legacy'
    assert not registry.registry_path.exists()
    record = registry.import_ipa(make_ipa(tmp_path / 'future.ipa', bundle='com.example.future'),
                                 app_slug='v2raytun', title='V2RayTun', expected_bundle_id='com.example.future',
                                 source_account_id='account-b', activation_profile_id='profile-b')
    products = load_products(settings, registry)
    assert products['v2raytun']['ipa'] is None
    assert products['v2raytun']['price_rub'] is None
    assert products['v2raytun']['coming_soon'] is True
    registry.publish('v2raytun', record['id'])
    products = load_products(settings, registry)
    assert products['v2raytun']['ipa'].available
    assert products['v2raytun']['artifact_id'] == record['id']
    assert products['v2raytun']['source_account_id'] == 'account-b'
    assert products['v2raytun']['price_rub'] == '490.00'
    assert products['v2raytun']['coming_soon'] is False
    assert products['happ']['price_rub'] == '1.00'
    assert all(products[slug]['coming_soon'] for slug in ('sber', 'tbank', 'vtb', 'chatgpt', 'deepseek'))


def test_explicit_happ_price_and_release_override_only_the_legacy_environment(registry, tmp_path):
    legacy = make_ipa(tmp_path / 'legacy.ipa')
    settings = settings_for(legacy)
    registry.set_product('happ', {'price_rub': '777.00'})
    assert load_products(settings, registry)['happ']['price_rub'] == '777.00'
    release = import_happ(registry, make_ipa(tmp_path / 'new.ipa', version='2.0'))
    registry.publish('happ', release['id'])
    current = load_products(settings, registry)['happ']
    assert current['price_rub'] == '777.00'
    assert current['ipa'].version == '2.0'
    assert current['artifact_id'] == release['id']
    assert current['artifact']['source_account_id'] is None


def test_custom_definition_path_is_read_and_reloaded_without_registry_writes(registry, tmp_path):
    settings = settings_for(make_ipa(tmp_path / 'legacy.ipa'))
    definition_path = tmp_path / 'catalog.json'
    settings.catalog_definition_path = definition_path
    value = {'schema_version': 1, 'products': {'happ': {'title': 'Happ', 'price_rub': '123.00', 'coming_soon': False}}}
    definition_path.write_text(json.dumps(value))
    assert load_products(settings, registry)['happ']['price_rub'] == '123.00'
    value['products']['happ']['price_rub'] = '321.00'
    definition_path.write_text(json.dumps(value))
    assert load_products(settings, registry)['happ']['price_rub'] == '321.00'
    assert not registry.registry_path.exists()


def test_legacy_snapshot_identity_and_unknown_provenance_are_stable_when_missing(tmp_path):
    source = make_ipa(tmp_path / 'legacy.ipa')
    file = IPAFile(source, hashlib.sha256(source.read_bytes()).hexdigest())
    record = legacy_artifact('happ', {'title': 'Happ', 'ipa': file})
    assert record['id'].startswith('legacy-')
    assert record['source_account_id'] is None
    assert record['activation_profile_id'] == 'legacy'
    os.utime(source, None)
    assert legacy_artifact('happ', {'title': 'Happ', 'ipa': file}) == record
    source.unlink()
    assert legacy_artifact('happ', {'title': 'Happ', 'ipa': file}) == record
    assert not resolve_artifact(record).available
