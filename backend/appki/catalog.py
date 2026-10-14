"""Validated product configuration and immutable, account-bound IPA releases.

Readers never create files. Writers use one private metadata lock and atomic
replacement; published pointers can change without changing any old artifact.
Only opaque profile IDs are stored here, never Apple credentials or sessions.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from .ota import IPAFile


DEFAULT_PRICE_RUB = '490.00'
LEGACY_CREATED_AT = '1970-01-01T00:00:00Z'
PRODUCT_FIELDS = frozenset({
    'title', 'subtitle', 'description', 'price_rub', 'category', 'features',
    'coming_soon', 'app_store_id', 'expected_bundle_id',
})
ARTIFACT_FIELDS = frozenset({
    'id', 'app_slug', 'title', 'path', 'sha256', 'bundle_id', 'version', 'build',
    'minimum_ios', 'size_bytes', 'source_account_id', 'activation_profile_id', 'created_at',
})
_SLUG = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z')
_PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_ARTIFACT_ID = re.compile(r'(?:ipa|legacy)-[0-9a-f]{64}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_BUNDLE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)*\Z')
_VERSION = re.compile(r'[0-9][A-Za-z0-9.+_-]{0,63}\Z')
_IOS = re.compile(r'[0-9]+(?:\.[0-9]+){0,2}\Z')
_MAX_REGISTRY_BYTES = 8 * 1024 * 1024


class CatalogError(ValueError):
    """Invalid configuration or artifact; messages intentionally omit input data."""


def _identifier(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CatalogError(f'Invalid {label}.')
    return value


def _bundle(value):
    if not isinstance(value, str) or len(value) > 255 or not _BUNDLE.fullmatch(value):
        raise CatalogError('Invalid bundle identifier.')
    return value


def _profile(value):
    return None if value is None else _identifier(value, _PROFILE, 'profile identifier')


def _text(value, *, limit, empty=False, multiline=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(char) < 32 and (not multiline or char not in '\n\t') for char in value)):
        raise CatalogError('Invalid product text.')
    return value


def _price(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', value):
        raise CatalogError('Invalid product price.')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 < amount <= 1000000:
            raise CatalogError('Invalid product price.')
    except InvalidOperation:
        raise CatalogError('Invalid product price.') from None
    return format(amount, '.2f')


def _product(definition):
    if not isinstance(definition, dict) or set(definition) - PRODUCT_FIELDS:
        raise CatalogError('Unsupported product fields.')
    result = {
        'title': _text(definition.get('title'), limit=160),
        'subtitle': _text(definition.get('subtitle', ''), limit=300, empty=True),
        'description': _text(definition.get('description', ''), limit=10000, empty=True, multiline=True),
        'category': _text(definition.get('category', 'Другое'), limit=80),
        'price_rub': _price(definition.get('price_rub')),
        'coming_soon': definition.get('coming_soon', True),
        'features': definition.get('features', []),
        'app_store_id': definition.get('app_store_id'),
        'expected_bundle_id': definition.get('expected_bundle_id'),
    }
    if type(result['coming_soon']) is not bool:
        raise CatalogError('Invalid product availability flag.')
    features = result['features']
    if not isinstance(features, list) or len(features) > 40:
        raise CatalogError('Invalid product features.')
    result['features'] = [_text(feature, limit=500) for feature in features]
    store_id = result['app_store_id']
    if store_id is not None and (type(store_id) is not int or not 0 < store_id < 2 ** 63):
        raise CatalogError('Invalid App Store identifier.')
    if result['expected_bundle_id'] is not None:
        result['expected_bundle_id'] = _bundle(result['expected_bundle_id'])
    return result


def _definition_file(path):
    path = _base_path(path)
    _check_file(path, missing=False)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            if os.fstat(stream.fileno()).st_size > _MAX_REGISTRY_BYTES:
                raise CatalogError('Product catalog is too large.')
            value = json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise CatalogError('Invalid product catalog JSON.') from None
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'products'}
            or type(value.get('schema_version')) is not int or value.get('schema_version') != 1
            or not isinstance(value.get('products'), dict)):
        raise CatalogError('Invalid product catalog.')
    return {
        _identifier(slug, _SLUG, 'application slug'): _product(definition)
        for slug, definition in value['products'].items()
    }


@lru_cache(maxsize=1)
def _defaults():
    return _definition_file(Path(__file__).with_name('catalog.json'))


def _empty_registry():
    return {'schema_version': 1, 'artifacts': {}, 'active': {}, 'products': {}}


def _artifact_id(record, *, legacy=False):
    identity = [record['app_slug'], record['sha256'], record['bundle_id']]
    if not legacy:
        identity.extend([record['source_account_id'], record['activation_profile_id']])
    digest = hashlib.sha256(json.dumps(identity, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    return ('legacy-' if legacy else 'ipa-') + digest


def _record(value):
    if not isinstance(value, dict) or set(value) != ARTIFACT_FIELDS:
        raise CatalogError('Invalid artifact fields.')
    record = dict(value)
    _identifier(record['id'], _ARTIFACT_ID, 'artifact identifier')
    _identifier(record['app_slug'], _SLUG, 'application slug')
    _identifier(record['sha256'], _SHA, 'artifact digest')
    _bundle(record['bundle_id'])
    _text(record['title'], limit=160)
    _identifier(record['version'], _VERSION, 'artifact version')
    _identifier(record['build'], _VERSION, 'artifact build')
    if (not isinstance(record['minimum_ios'], str) or len(record['minimum_ios']) > 32
            or not _IOS.fullmatch(record['minimum_ios'])):
        raise CatalogError('Invalid minimum iOS version.')
    if type(record['size_bytes']) is not int or not 0 < record['size_bytes'] < 2 ** 63:
        raise CatalogError('Invalid artifact size.')
    _profile(record['source_account_id'])
    _profile(record['activation_profile_id'])
    path = record['path']
    if not isinstance(path, str) or not Path(path).is_absolute() or '..' in Path(path).parts:
        raise CatalogError('Invalid artifact path.')
    created = record['created_at']
    try:
        if not isinstance(created, str) or len(created) != 20 or not created.endswith('Z'):
            raise ValueError
        datetime.strptime(created, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise CatalogError('Invalid artifact creation time.') from None
    legacy = record['id'].startswith('legacy-')
    if legacy and (record['source_account_id'] is not None or record['activation_profile_id'] != 'legacy'):
        raise CatalogError('Invalid legacy artifact provenance.')
    if record['id'] != _artifact_id(record, legacy=legacy):
        raise CatalogError('Artifact identity does not match its metadata.')
    return record


def _base_path(value):
    path = Path(value).expanduser().absolute()
    if '..' in path.parts or not path.name or path.is_symlink():
        raise CatalogError('Invalid storage path.')
    # Canonicalize trusted configured ancestors (including macOS /var), while
    # rejecting links in the managed root itself and every managed child.
    return path.parent.resolve() / path.name


def _check_directory(path, *, create=False):
    existed = path.exists() or path.is_symlink()
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o2750)
    try:
        status = path.lstat()
    except FileNotFoundError:
        if create:
            raise CatalogError('Storage directory is unavailable.') from None
        return
    if not stat.S_ISDIR(status.st_mode):
        raise CatalogError('Storage directory must not be a link or file.')
    if create and not existed:
        # Setgid keeps the operator-configured shared group on new IPA files.
        # Existing configured roots retain their ownership and permissions.
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, 0o2750)
        finally:
            os.close(descriptor)


def _check_file(path, *, missing=True, immutable=False):
    try:
        status = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise CatalogError('Artifact file is unavailable.') from None
    if not stat.S_ISREG(status.st_mode):
        raise CatalogError('Artifact path must be a regular file.')
    if immutable and (stat.S_IMODE(status.st_mode) != 0o440 or status.st_nlink != 1):
        raise CatalogError('Artifact file is not immutable.')
    return status


@lru_cache(maxsize=128)
def _ipa(path, digest, bundle, title):
    return IPAFile(Path(path), digest, expected_bundle_id=bundle, title=title)


def resolve_artifact(record):
    """Resolve a validated snapshot, including legacy snapshots outside the store.

    Missing or corrupted file bytes leave IPAFile.available false, as in legacy
    delivery. Invalid metadata and symlink targets fail closed with CatalogError.
    """
    record = _record(record)
    path = Path(record['path'])
    if path.parent.resolve() != path.parent:
        raise CatalogError('Artifact directory must not contain links.')
    if record['id'].startswith('ipa-'):
        if path.name != record['sha256'] + '.ipa' or path.parent.name != record['app_slug']:
            raise CatalogError('Artifact path escapes its managed location.')
        _check_directory(path.parent.parent)
        _check_directory(path.parent)
    _check_file(path, immutable=record['id'].startswith('ipa-'))
    file = _ipa(record['path'], record['sha256'], record['bundle_id'], record['title'])
    file.refresh()
    if file.available and any([
        file.version != record['version'], file.build != record['build'],
        file.minimum_ios != record['minimum_ios'], file.size != record['size_bytes'],
    ]):
        raise CatalogError('Artifact metadata does not match its file.')
    return file


def legacy_artifact(app_slug, product):
    """Stable legacy identity; unknown acquisition date and account stay unknown."""
    _identifier(app_slug, _SLUG, 'application slug')
    file = product.get('ipa')
    if not isinstance(file, IPAFile):
        raise CatalogError('Legacy product has no IPA definition.')
    record = {
        'id': '', 'app_slug': app_slug, 'title': product['title'],
        'path': str(_base_path(file.path)), 'sha256': file.expected_sha,
        'bundle_id': file.expected_bundle_id, 'version': file.version,
        'build': file.build, 'minimum_ios': file.minimum_ios, 'size_bytes': file.size,
        'source_account_id': None, 'activation_profile_id': 'legacy',
        'created_at': LEGACY_CREATED_AT,
    }
    record['id'] = _artifact_id(record, legacy=True)
    return _record(record)


class ArtifactRegistry:
    def __init__(self, registry_path: Path, artifact_root: Path):
        self.registry_path = _base_path(registry_path)
        self.artifact_root = _base_path(artifact_root)
        self.lock_path = self.registry_path.with_name(self.registry_path.name + '.lock')

    def _managed_record(self, record):
        record = _record(record)
        expected = self.artifact_root / record['app_slug'] / (record['sha256'] + '.ipa')
        if record['path'] != str(expected):
            raise CatalogError('Artifact path escapes its managed location.')
        _check_directory(self.artifact_root)
        _check_directory(expected.parent)
        _check_file(expected, immutable=True)
        return record

    def read(self):
        _check_file(self.registry_path)
        try:
            descriptor = os.open(self.registry_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return _empty_registry()
        try:
            with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
                if os.fstat(stream.fileno()).st_size > _MAX_REGISTRY_BYTES:
                    raise CatalogError('Artifact registry is too large.')
                value = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CatalogError('Invalid artifact registry JSON.') from None
        if (not isinstance(value, dict) or set(value) != set(_empty_registry())
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or any(not isinstance(value[key], dict) for key in ('artifacts', 'active', 'products'))):
            raise CatalogError('Invalid artifact registry schema.')
        artifacts = {}
        for identifier, record in value['artifacts'].items():
            validated = self._managed_record(record)
            if identifier != validated['id']:
                raise CatalogError('Artifact map identity does not match its record.')
            artifacts[identifier] = validated
        for slug, identifier in value['active'].items():
            _identifier(slug, _SLUG, 'application slug')
            _identifier(identifier, _ARTIFACT_ID, 'artifact identifier')
            if identifier not in artifacts or artifacts[identifier]['app_slug'] != slug:
                raise CatalogError('Active release does not match its application.')
        products = {
            _identifier(slug, _SLUG, 'application slug'): _product(definition)
            for slug, definition in value['products'].items()
        }
        return {'schema_version': 1, 'artifacts': artifacts, 'active': dict(value['active']), 'products': products}

    def get(self, artifact_id):
        _identifier(artifact_id, _ARTIFACT_ID, 'artifact identifier')
        record = self.read()['artifacts'].get(artifact_id)
        return None if record is None else self._managed_record(record)

    def active(self, app_slug):
        _identifier(app_slug, _SLUG, 'application slug')
        value = self.read()
        identifier = value['active'].get(app_slug)
        return None if identifier is None else self._managed_record(value['artifacts'][identifier])

    def product_definitions(self, definition_path=None):
        custom = _definition_file(definition_path) if definition_path is not None else {}
        definitions = {**_defaults(), **custom, **self.read()['products']}
        return {slug: _product(definition) for slug, definition in definitions.items()}

    def get_product_definition(self, app_slug, definition_path=None):
        _identifier(app_slug, _SLUG, 'application slug')
        definition = self.product_definitions(definition_path).get(app_slug)
        return None if definition is None else _product(definition)

    @contextmanager
    def _writer(self):
        _check_directory(self.registry_path.parent, create=True)
        _check_file(self.registry_path)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o640)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise CatalogError('Invalid registry lock file.')
            os.fchmod(descriptor, 0o640)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _save(self, value):
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
        if len(payload.encode()) > _MAX_REGISTRY_BYTES:
            raise CatalogError('Artifact registry is too large.')
        descriptor, temporary = tempfile.mkstemp(prefix='.registry-', suffix='.json', dir=self.registry_path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o640)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _check_file(self.registry_path)
            os.replace(temporary, self.registry_path)
            directory = os.open(self.registry_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def set_product(self, app_slug, definition):
        _identifier(app_slug, _SLUG, 'application slug')
        if not isinstance(definition, dict) or set(definition) - PRODUCT_FIELDS:
            raise CatalogError('Unsupported product fields.')
        with self._writer():
            value = self.read()
            previous = value['products'].get(app_slug, _defaults().get(app_slug, {}))
            value['products'][app_slug] = _product({**previous, **definition})
            self._save(value)

    def import_ipa(self, source_path, *, app_slug, title, expected_bundle_id,
                   source_account_id=None, activation_profile_id=None):
        _identifier(app_slug, _SLUG, 'application slug')
        _text(title, limit=160)
        _bundle(expected_bundle_id)
        _profile(source_account_id)
        _profile(activation_profile_id)
        source_path = _base_path(source_path)
        _check_file(source_path, missing=False)
        with self._writer():
            value = self.read()
            _check_directory(self.artifact_root, create=True)
            directory_path = self.artifact_root / app_slug
            _check_directory(directory_path, create=True)
            directory = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            temporary = '.import-' + secrets.token_hex(16)
            try:
                source = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o440, dir_fd=directory)
                except BaseException:
                    os.close(source)
                    raise
                digest = hashlib.sha256()
                with os.fdopen(source, 'rb') as incoming, os.fdopen(target, 'wb') as outgoing:
                    before = os.fstat(incoming.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise CatalogError('IPA source must be a regular file.')
                    while block := incoming.read(1024 * 1024):
                        digest.update(block)
                        outgoing.write(block)
                    outgoing.flush()
                    os.fchmod(outgoing.fileno(), 0o440)
                    os.fsync(outgoing.fileno())
                    if IPAFile.fingerprint_of(before) != IPAFile.fingerprint_of(os.fstat(incoming.fileno())):
                        raise CatalogError('IPA source changed during import.')
                checksum = digest.hexdigest()
                try:
                    staged = IPAFile(directory_path / temporary, checksum, expected_bundle_id=expected_bundle_id, title=title)
                except (TypeError, AttributeError):
                    raise CatalogError('IPA application metadata is invalid.') from None
                if not staged.available:
                    raise CatalogError('IPA failed its digest or application metadata check.')
                record = {
                    'id': '', 'app_slug': app_slug, 'title': title,
                    'path': str(directory_path / (checksum + '.ipa')), 'sha256': checksum,
                    'bundle_id': staged.bundle_id, 'version': staged.version, 'build': staged.build,
                    'minimum_ios': staged.minimum_ios, 'size_bytes': staged.size,
                    'source_account_id': source_account_id, 'activation_profile_id': activation_profile_id,
                    'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                }
                record['id'] = _artifact_id(record)
                record = _record(record)
                filename = checksum + '.ipa'
                try:
                    os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError:
                    self._managed_record(record)
                    existing = resolve_artifact(record)
                    if not existing.available:
                        raise CatalogError('Existing immutable artifact does not match its digest.')
                os.unlink(temporary, dir_fd=directory)
                os.fsync(directory)
                if record['id'] in value['artifacts']:
                    existing = self._managed_record(value['artifacts'][record['id']])
                    if any(existing[field] != record[field] for field in ARTIFACT_FIELDS - {'created_at'}):
                        raise CatalogError('Artifact identity is already used by different metadata.')
                    return existing
                value['artifacts'][record['id']] = record
                self._save(value)
                return record
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                os.close(directory)

    def publish(self, app_slug, artifact_id):
        _identifier(app_slug, _SLUG, 'application slug')
        _identifier(artifact_id, _ARTIFACT_ID, 'artifact identifier')
        with self._writer():
            value = self.read()
            record = value['artifacts'].get(artifact_id)
            if record is None or record['app_slug'] != app_slug:
                raise CatalogError('Release artifact does not belong to this application.')
            record = self._managed_record(record)
            file = resolve_artifact(record)
            if not file.available:
                raise CatalogError('Release artifact is unavailable or corrupted.')
            definition = value['products'].get(app_slug, _defaults().get(app_slug))
            if definition is None:
                raise CatalogError('Configure the product before publishing a new application.')
            definition = _product(definition)
            expected = definition['expected_bundle_id']
            if expected is not None and expected != record['bundle_id']:
                raise CatalogError('Release bundle does not match the product configuration.')
            definition['expected_bundle_id'] = record['bundle_id']
            definition['coming_soon'] = False
            if definition['price_rub'] is None:
                definition['price_rub'] = DEFAULT_PRICE_RUB
            value['products'][app_slug] = definition
            value['active'][app_slug] = artifact_id
            self._save(value)


def load_products(settings, registry: ArtifactRegistry):
    """Current releases plus the legacy Happ fallback; no metadata writes."""
    value = registry.read()
    definition_path = getattr(settings, 'catalog_definition_path', None)
    custom = _definition_file(definition_path) if definition_path is not None else {}
    definitions = {**_defaults(), **custom, **value['products']}
    products = {}
    for slug, configured in definitions.items():
        definition = _product(configured)
        record = value['artifacts'].get(value['active'].get(slug))
        file = None
        if record is not None:
            file = resolve_artifact(registry._managed_record(record))
        elif slug == 'happ':
            if slug not in value['products'] and slug not in custom:
                definition['price_rub'] = settings.happ_price_rub
            file = _ipa(str(_base_path(settings.happ_ipa_path)), settings.happ_ipa_sha256,
                        definition['expected_bundle_id'] or 'su.ffg.happ', definition['title'])
            file.refresh()
            record = legacy_artifact(slug, {**definition, 'ipa': file})
        products[slug] = {
            **definition, 'ipa': file, 'artifact_id': record['id'] if record else None,
            'artifact': record, 'source_account_id': record['source_account_id'] if record else None,
            'activation_profile_id': record['activation_profile_id'] if record else None,
        }
    return products
