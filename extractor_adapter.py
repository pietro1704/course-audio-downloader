"""Optional extraction of externally hosted audio, bound to a verified lesson."""
from __future__ import annotations
from dataclasses import replace
import math
import re
from urllib.parse import urlsplit
from browser_adapter import SafariAdapter
from pipeline import Candidate
import course_audio_bot as audio


class QuietLogger:
    # Third-party errors may contain signed URLs. Keep them inside the adapter.
    def debug(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass


class ExtractorAdapter(SafariAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._media = {}

    @staticmethod
    def _factory():
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            raise RuntimeError('extractor-dependency-missing') from None
        return YoutubeDL

    @staticmethod
    def candidates(page, profile=None):
        settings = (profile or {}).get('extractor', {})
        pattern = settings.get('url_pattern', r'(?!)')
        urls = list(page.get('frames', []))
        for link in page.get('links', []):
            urls.append(link.get('url', ''))
            # Some authored pages put a URL in a label instead of its href.
            urls.extend(re.findall(r'https://[^\s<>"\x27]+', link.get('label', '')))
        matches = {}
        for url in urls:
            match = re.fullmatch(pattern, url)
            if match and urlsplit(url).scheme == 'https':
                matches.setdefault(match['id'], Candidate(url, 'extractor-audio', referer=page.get('url')))
        # Multiple distinct media cannot be assigned to one lesson implicitly.
        if len(matches) > 1:
            raise RuntimeError('ambiguous-external-media')
        return list(matches.values())

    def _options(self):
        return dict(format='bestaudio[protocol=https][vcodec=none]', noplaylist=True,
                    quiet=True, no_warnings=True, logger=QuietLogger(), cachedir=False,
                    socket_timeout=30, retries=2, fragment_retries=2,
                    fixup='never', max_filesize=self.max_source_bytes,
                    continuedl=False, overwrites=False, buffersize=65536)

    def _validate(self, info, expected):
        try:
            duration = float(info.get('duration'))
            size = info.get('filesize') or info.get('filesize_approx') or 0
            parsed = urlsplit(info.get('url', ''))
            valid = (info.get('_type', 'video') == 'video' and str(info.get('id')) == expected
                     and not info.get('is_live') and not info.get('has_drm')
                     and math.isfinite(duration) and duration > 0
                     and info.get('protocol') == 'https' and parsed.scheme == 'https'
                     and parsed.hostname and not parsed.username and not parsed.password
                     and info.get('vcodec') == 'none' and info.get('acodec') not in {None, 'none'}
                     and not info.get('requested_formats') and not info.get('entries'))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise RuntimeError('external-media-identity-or-format-mismatch')
        if size > self.max_source_bytes:
            raise RuntimeError('external-source-budget-exceeded')

    def resolve(self, lesson):
        candidates = super().resolve(lesson)
        candidate = candidates[0]
        expected = re.fullmatch(self.profile['extractor']['url_pattern'], candidate.url)['id']
        if isinstance(lesson, dict) and lesson.get('external_media_id', expected) != expected:
            raise RuntimeError('catalogue-media-identity-mismatch')
        factory = self._factory()
        try:
            with factory(self._options()) as session:
                info = session.extract_info(candidate.url, download=False)
        except Exception:
            raise RuntimeError('external-media-extraction-failed') from None
        self._validate(info, expected)
        thumbnail = info.get('thumbnail')
        try:
            parsed = urlsplit(thumbnail or '')
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                thumbnail = None
        except ValueError:
            thumbnail = None
        self._media[candidate.url] = info
        return [replace(candidate, thumbnail_url=thumbnail)]

    def download(self, candidate, destination):
        info = self._media.get(candidate.url)
        if info is None:
            raise RuntimeError('unresolved-external-media')
        expected = re.fullmatch(self.profile['extractor']['url_pattern'], candidate.url)['id']
        self._validate(info, expected)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError('external-output-already-exists')
        exceeded = False
        def guard(progress):
            nonlocal exceeded
            if progress.get('downloaded_bytes', 0) > self.max_source_bytes:
                exceeded = True
                raise RuntimeError('external-source-budget-exceeded')
        options = self._options()
        options.update(outtmpl=str(destination), progress_hooks=[guard])
        factory = self._factory()
        try:
            with factory(options) as session:
                # Reuse only the metadata already validated for this lesson.
                session.process_info(dict(info))
        except Exception:
            if exceeded:
                raise RuntimeError('external-source-budget-exceeded') from None
            raise RuntimeError('external-media-download-failed') from None
        if not destination.is_file() or destination.is_symlink() or not destination.stat().st_size:
            raise RuntimeError('external-media-download-failed')
        if destination.stat().st_size > self.max_source_bytes:
            raise RuntimeError('external-source-budget-exceeded')
        duration = audio._probe_duration(destination)
        if (duration is None or not math.isfinite(duration) or duration <= 0 or
                abs(duration - float(info['duration'])) > max(.25, min(2, float(info['duration']) * .02))):
            raise RuntimeError('incomplete-external-audio')
        return destination
