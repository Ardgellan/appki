"""Private, unchanged IPA delivery; only the main Info.plist is inspected."""
import base64
import hashlib
import html
import os
from pathlib import Path
import plistlib
import re
from urllib.parse import quote
import zipfile

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse


PRIVATE_HEADERS = {
    'Cache-Control': 'private, no-store, max-age=0',
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
    'X-Robots-Tag': 'noindex, nofollow, noarchive',
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
}

INSTALL_SCRIPT = '''(function () {
  const isIOS = /iPhone|iPad|iPod/i.test(navigator.userAgent || '') ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (!isIOS) return;
  const button = document.getElementById('install-app');
  if (!button) return;
  try { window.location.assign(button.href); } catch (_) {}
})();'''

INSTALL_PAGE_HEADERS = {
    **PRIVATE_HEADERS,
    'Content-Security-Policy': PRIVATE_HEADERS['Content-Security-Policy'] +
        "; script-src 'sha256-" + base64.b64encode(hashlib.sha256(INSTALL_SCRIPT.encode()).digest()).decode() + "'",
}


def byte_range(value: str, size: int):
    if len(value) > 128:
        raise ValueError('Invalid range')
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', value)
    if not match or not any(match.groups()) or not size:
        raise ValueError('Invalid range')
    first, last = match.groups()
    if not first:
        count = int(last)
        if count <= 0:
            raise ValueError('Empty suffix')
        return max(0, size - count), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError('Unsatisfiable range')
    return start, end


class IPAFile:
    def __init__(self, path: Path, expected_sha: str, *, expected_bundle_id: str = 'su.ffg.happ', title: str = 'Happ'):
        self.path = Path(path)
        self.expected_sha = expected_sha
        self.expected_bundle_id = expected_bundle_id
        self.title = title
        self.available = False
        self.size = 305092112
        self.version = '5.9.0'
        self.build = '2609171647'
        self.minimum_ios = '15.0'
        self.bundle_id = expected_bundle_id
        self.fingerprint = None
        self.refresh()

    @staticmethod
    def fingerprint_of(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def checked_stream(self):
        """Check and serve the same descriptor; no pathname TOCTOU window."""
        stream = self.path.open('rb')
        try:
            before = os.fstat(stream.fileno())
            if self.fingerprint_of(before) != self.fingerprint:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                after = os.fstat(stream.fileno())
                if digest != self.expected_sha or self.fingerprint_of(before) != self.fingerprint_of(after):
                    self.available = False
                    raise ValueError('IPA integrity check failed')
                self.fingerprint = self.fingerprint_of(after)
            stream.seek(0)
            return stream
        except BaseException:
            stream.close()
            raise

    def refresh(self):
        try:
            with self.checked_stream() as stream:
                with zipfile.ZipFile(stream) as archive:
                    names = [name for name in archive.namelist() if name.startswith('Payload/')
                             and name.endswith('.app/Info.plist') and name.count('/') == 2]
                    if len(names) != 1 or archive.getinfo(names[0]).file_size > 1024 * 1024:
                        raise ValueError('Invalid app metadata')
                    info = plistlib.loads(archive.read(names[0]))
                    if info.get('CFBundleIdentifier') != self.expected_bundle_id:
                        raise ValueError('Unexpected application')
                    self.bundle_id = info['CFBundleIdentifier']
                    self.version = str(info['CFBundleShortVersionString'])
                    self.build = str(info['CFBundleVersion'])
                    self.minimum_ios = str(info.get('MinimumOSVersion', '15.0'))
                    self.size = os.fstat(stream.fileno()).st_size
            self.available = True
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, plistlib.InvalidFileException):
            self.available = False
        return self.available

    def manifest(self, base: str, token: str):
        return plistlib.dumps({'items': [{
            'assets': [{'kind': 'software-package', 'url': f'{base}/install/{token}/app.ipa'}],
            'metadata': {'bundle-identifier': self.bundle_id, 'bundle-version': self.build,
                         'kind': 'software', 'title': self.title}
        }]}, fmt=plistlib.FMT_XML, sort_keys=False)

    def page(self, base: str, token: str):
        link = 'itms-services://?action=download-manifest&url=' + quote(f'{base}/install/{token}/manifest.plist', safe='')
        title = html.escape(self.title)
        return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Установить {title}</title>
<style>body{{font:17px/1.55 system-ui;background:#fff;color:#151515;margin:0;padding:24px}}main{{max-width:460px;margin:6vh auto;background:#f1f1f1;padding:28px;border-radius:24px}}h1{{font-size:34px}}a{{display:block;background:#0057ff;color:white;text-align:center;text-decoration:none;padding:17px;border-radius:14px;font-weight:600}}small{{display:block;color:#777;margin-top:24px}}.meta{{color:#777}}</style></head>
<body><main><p class="meta">Аппки · Установка на iPhone</p><h1>{title}</h1>
<p class="meta">{html.escape(self.version)} · {round(self.size/1000000)} МБ · iOS {html.escape(self.minimum_ios)}+</p>
<p>Подтвердите установку. Если окно не появилось, нажмите кнопку ниже.</p>
<a id="install-app" href="{html.escape(link, quote=True)}">Установить {title}</a>
<p>После установки вернитесь в Telegram для активации.</p>
<small>Ссылка временная. Не пересылайте её.</small>
</main><script>{INSTALL_SCRIPT}</script></body></html>'''

    def response(self, request: Request):
        try:
            stream = self.checked_stream()
        except (OSError, ValueError):
            raise HTTPException(503, 'Файл приложения временно недоступен.')
        etag = f'"{self.expected_sha}"'
        headers = {**PRIVATE_HEADERS, 'Accept-Ranges': 'bytes', 'ETag': etag}
        start, end, status = 0, self.size - 1, 200
        range_value = request.headers.get('range')
        if request.method != 'HEAD' and range_value and request.headers.get('if-range', etag) == etag:
            try:
                start, end = byte_range(range_value, self.size)
                status = 206
                headers['Content-Range'] = f'bytes {start}-{end}/{self.size}'
            except ValueError:
                stream.close()
                headers['Content-Range'] = f'bytes */{self.size}'
                return Response(status_code=416, headers=headers)
        length = end - start + 1
        headers['Content-Length'] = str(length)
        if request.method == 'HEAD':
            stream.close()
            return Response(headers=headers, media_type='application/octet-stream')

        def chunks():
            try:
                stream.seek(start)
                remaining = length
                while remaining:
                    block = stream.read(min(256 * 1024, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block
            finally:
                stream.close()
        return StreamingResponse(chunks(), status_code=status, media_type='application/octet-stream', headers=headers)
