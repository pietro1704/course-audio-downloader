#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Iterable

AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac")
VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mov")
STREAM_EXTS = (".m3u8", ".mpd")
MEDIA_EXTS = AUDIO_EXTS + VIDEO_EXTS + STREAM_EXTS
DRM_HINT_RE = re.compile(r"\b(widevine|fairplay|playready|encrypted-media|drm|licenseUrl|license_url)\b", re.I)
URL_RE = re.compile(r"https?://[^\s'\"<>\\]+", re.I)
SRC_RE = re.compile(r"(?:src|href|content|data-[\w-]+)=[\"']([^\"']+)[\"']", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
HREF_RE = re.compile(r"href=[\"']([^\"'#]+)[\"']", re.I)
PANDA_EMBED_RE = re.compile(r"https?://player-([a-z0-9-]+)\.tv\.pandavideo\.com\.br/embed/[^\"'<>\s]*?[?&]v=([0-9a-f-]{20,})", re.I)


@dataclass
class Course:
    provider: str
    title: str
    cookies_file: Path | None
    urls: list[str]
    crawl: bool = False
    max_pages: int = 300


def expand_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


def slugify(value: str, fallback: str = "aula") -> str:
    value = re.sub(r"[\s/\\:;|]+", "-", value.strip().lower())
    value = re.sub(r"[^\w.()\-À-ÿ]+", "", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-._")
    return value[:120] or fallback


def load_config(path: Path) -> tuple[Path, list[Course]]:
    data = tomllib.loads(path.read_text())
    output_root = expand_path(data.get("output_root") or "~/Cursos Baixados")
    assert output_root is not None
    courses: list[Course] = []
    for i, raw in enumerate(data.get("course", []), 1):
        urls = [u.strip() for u in raw.get("urls", []) if str(u).strip()]
        courses.append(
            Course(
                provider=raw.get("provider") or f"curso-{i}",
                title=raw.get("title") or f"Curso {i}",
                cookies_file=expand_path(raw.get("cookies_file")),
                urls=urls,
                crawl=bool(raw.get("crawl", False)),
                max_pages=int(raw.get("max_pages", 300)),
            )
        )
    return output_root, courses


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return proc.returncode, proc.stdout


def cookies_opener(cookies_file: Path | None):
    if not cookies_file:
        return urllib.request.build_opener()
    jar = MozillaCookieJar(str(cookies_file))
    jar.load(ignore_discard=True, ignore_expires=True)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch_html(url: str, cookies_file: Path | None, timeout: int = 30) -> str:
    opener = cookies_opener(cookies_file)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) course-audio-downloader/0.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read(5_000_000)
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def page_title(html: str) -> str | None:
    match = TITLE_RE.search(html)
    if not match:
        return None
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return text or None


def clean_media_url(url: str) -> str:
    # trim common JSON/HTML punctuation accidentally included by regex
    return html_lib.unescape(url).replace("\\/", "/").rstrip("),.;]}\\")


def is_media_like_url(url: str) -> bool:
    low = urllib.parse.unquote(url.lower())
    path = urllib.parse.urlparse(url).path.lower()
    return (
        path.endswith(MEDIA_EXTS)
        or any(ext + "?" in low or ext + "&" in low or ext + "%" in low for ext in MEDIA_EXTS)
        or "response-content-type=audio" in low
        or "filename" in low and any(ext in low for ext in AUDIO_EXTS + VIDEO_EXTS)
    )


def media_candidates(html: str, base_url: str) -> list[str]:
    found: list[str] = []
    # Panda Video embeds expose a normal HLS playlist derived from library id + video id.
    for lib, video_id in PANDA_EMBED_RE.findall(html):
        found.append(f"https://b-{lib}.tv.pandavideo.com.br/{video_id}/playlist.m3u8")
    for raw in URL_RE.findall(html):
        url = clean_media_url(raw)
        if "player.mediadelivery.net/embed/" in url or "tv.pandavideo.com.br/embed/" in url:
            found.append(url)
        elif is_media_like_url(url):
            found.append(url)
    for raw in SRC_RE.findall(html):
        url = urllib.parse.urljoin(base_url, raw.replace("\\/", "/"))
        if "player.mediadelivery.net/embed/" in url or "tv.pandavideo.com.br/embed/" in url:
            found.append(clean_media_url(url))
        elif is_media_like_url(url):
            found.append(clean_media_url(url))
    # preserve order, de-duplicate
    out: list[str] = []
    seen: set[str] = set()
    for url in found:
        if url not in seen:
            seen.add(url)
            out.append(url)
    out.sort(key=lambda u: 1 if ("player.mediadelivery.net/embed/" in u or "tv.pandavideo.com.br/embed/" in u) else 0)
    return out


def page_links(html: str, base_url: str) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    out: list[str] = []
    seen: set[str] = set()
    for raw in HREF_RE.findall(html):
        url = urllib.parse.urljoin(base_url, raw.replace("&amp;", "&"))
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc:
            continue
        if not re.search(r"/(aulas|cursos|area/conteudo/(aula|produto))(/|$)", parsed.path):
            continue
        normalized = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def crawl_course_urls(course: Course) -> list[str]:
    queue = list(course.urls)
    seen: set[str] = set()
    ordered: list[str] = []
    while queue and len(seen) < course.max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            html = fetch_html(url, course.cookies_file)
        except Exception:
            ordered.append(url)
            continue
        if media_candidates(html, url) or re.search(r"/(aula|aulas)/", urllib.parse.urlparse(url).path):
            ordered.append(url)
        for link in page_links(html, url):
            if link not in seen and link not in queue:
                queue.append(link)
    return ordered


def looks_drm(html: str) -> bool:
    return bool(DRM_HINT_RE.search(html))


def ytdlp_download(url: str, out_dir: Path, stem_hint: str, cookies_file: Path | None, referer: str | None = None, output_format: str = "m4a") -> tuple[bool, str]:
    if not have("yt-dlp"):
        return False, "yt-dlp não encontrado"
    parsed_path = urllib.parse.urlparse(url).path.lower()
    if parsed_path.endswith(STREAM_EXTS) or "pandavideo.com.br" in url or "mediadelivery.net" in url:
        final_suffix = ".m4a" if output_format == "m4a" else ".mp3"
        final_path = out_dir / f"{slugify(stem_hint)}{final_suffix}"
        if final_path.exists() and final_path.stat().st_size > 0:
            return True, f"já existe: {final_path}"
        out_tmpl = str(out_dir / f"{slugify(stem_hint)}.%(ext)s")
    else:
        out_tmpl = str(out_dir / f"%(playlist_index|)s-%(title).180B [%(id)s].%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-progress",
        "--ignore-config",
        "--no-overwrites",
        "--restrict-filenames",
        "--extract-audio",
        "-f",
        "worst",
        "--audio-format",
        output_format,
        "--audio-quality",
        "16K",
        "--postprocessor-args",
        "ffmpeg:-vn -ac 1 -ar 22050 -b:a 16k",
        "-o",
        out_tmpl,
    ]
    if referer:
        cmd += ["--referer", referer, "--add-header", f"Origin:{urllib.parse.urlparse(referer).scheme}://{urllib.parse.urlparse(referer).netloc}"]
    if cookies_file:
        cmd += ["--cookies", str(cookies_file)]
    cmd.append(url)
    rc, out = run(cmd, timeout=int(os.environ.get("COURSE_AUDIO_YTDLP_TIMEOUT", "7200")))
    ok = rc == 0 and ("ERROR:" not in out)
    return ok, out[-4000:]


def verify_audio_output(path: Path) -> tuple[bool, str]:
    if not path.exists() or path.stat().st_size == 0:
        return False, "arquivo ausente/vazio"
    rc, out = run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,profile,sample_rate,channels", "-of", "json", str(path)], timeout=300)
    if rc != 0:
        return False, "ffprobe falhou"
    try:
        stream = json.loads(out)["streams"][0]
    except (KeyError, IndexError, json.JSONDecodeError):
        return False, "sem stream de áudio"
    expected = {"codec_name": "aac", "profile": "LC", "sample_rate": "44100", "channels": 1}
    if any(str(stream.get(k)) != str(v) for k, v in expected.items()):
        return False, f"parâmetros inválidos: {stream}"
    rc, _ = run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], timeout=600)
    return (rc == 0, "ok" if rc == 0 else "decode falhou")


def compress_audio_file(path: Path, output_format: str = "m4a") -> tuple[Path, str]:
    """Compress speech audio to small mono M4A by default; MP3 only if requested."""
    if path.suffix.lower() not in AUDIO_EXTS:
        return path, "not-audio"
    output_format = output_format.lower()
    if output_format not in {"m4a", "mp3"}:
        raise ValueError(f"unsupported output_format: {output_format}")
    final_suffix = ".m4a" if output_format == "m4a" else ".mp3"
    tmp = path.with_suffix(path.suffix + f".compressed{final_suffix}")
    codec_args = ["-c:a", "aac"] if output_format == "m4a" else []
    cmd = [
        "ffmpeg", "-y", "-i", str(path), "-vn",
        *codec_args, "-ac", "1", "-ar", "44100", "-b:a", "32k",
        str(tmp),
    ]
    timeout = int(os.environ.get("COURSE_AUDIO_YTDLP_TIMEOUT", "7200"))
    rc, out = run(cmd, timeout=timeout)
    if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return path, "compress-failed: " + out[-500:]
    if output_format == "m4a":
        valid, detail = verify_audio_output(tmp)
        if not valid:
            tmp.unlink(missing_ok=True)
            return path, "verification-failed: " + detail
    final = path.with_suffix(final_suffix)
    original_size = path.stat().st_size
    compressed_size = tmp.stat().st_size
    if compressed_size < original_size:
        tmp.replace(final)
        return final, f"compressed {original_size}->{compressed_size} bytes"
    tmp.unlink(missing_ok=True)
    return path, f"kept original {original_size} bytes"


def infer_suffix_from_url(url: str) -> str:
    path_suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if path_suffix:
        return path_suffix
    low = urllib.parse.unquote(url.lower())
    for ext in AUDIO_EXTS + VIDEO_EXTS:
        if ext in low:
            return ext
    if "response-content-type=audio" in low:
        return ".mp3"
    return ".bin"


def direct_download(url: str, out_dir: Path, stem: str, cookies_file: Path | None, referer: str | None = None, output_format: str = "m4a") -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(url)
    suffix = infer_suffix_from_url(url)
    if suffix in STREAM_EXTS or suffix in VIDEO_EXTS or "player.mediadelivery.net/embed/" in url or "tv.pandavideo.com.br/embed/" in url:
        return ytdlp_download(url, out_dir, stem, cookies_file, referer=referer, output_format=output_format)
    desired_suffix = ".m4a" if output_format == "m4a" else ".mp3"
    desired = out_dir / (slugify(stem) + desired_suffix)
    if desired.exists() and desired.stat().st_size > 0:
        return True, f"já existe: {desired}"
    filename = slugify(stem) + suffix
    staging = out_dir / ".staging"
    originals = out_dir / "originals"
    staging.mkdir(exist_ok=True)
    originals.mkdir(exist_ok=True)
    dest = staging / filename
    if dest.exists() and dest.stat().st_size > 0:
        final, cdetail = compress_audio_file(dest, output_format=output_format)
        return True, f"já existe convertido: {final} ({final.stat().st_size} bytes; {cdetail})"
    opener = cookies_opener(cookies_file)
    headers = {"User-Agent": "Mozilla/5.0 course-audio-downloader/0.1"}
    if referer:
        headers["Referer"] = referer
        parsed_ref = urllib.parse.urlparse(referer)
        headers["Origin"] = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=120) as resp, dest.open("wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception as e:
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
        return False, f"falha download direto: {e}"
    if dest.exists() and dest.stat().st_size > 0:
        shutil.copy2(dest, originals / filename)
        final, cdetail = compress_audio_file(dest, output_format=output_format)
        if final.suffix in {".m4a", ".mp3"} and final.exists():
            promoted = out_dir / (slugify(stem) + final.suffix)
            final.replace(promoted)
            dest.unlink(missing_ok=True)
            if promoted.suffix == ".m4a":
                valid, detail = verify_audio_output(promoted)
                if not valid:
                    promoted.unlink(missing_ok=True)
                    return False, "verification-failed: " + detail
            return True, f"baixado: {promoted} ({promoted.stat().st_size} bytes; {cdetail})"
    return False, f"download vazio: {dest}"


def write_manifest(out_dir: Path, record: dict) -> None:
    record = dict(record)
    record["ts"] = int(time.time())
    with (out_dir / "manifest.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_url(course: Course, output_root: Path, url: str, index: int, prefer_ytdlp: bool = True, output_format: str = "m4a") -> dict:
    out_dir = output_root / slugify(course.provider) / slugify(course.title)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{index:03d}-{slugify(urllib.parse.urlparse(url).path or 'aula')}"
    record = {"provider": course.provider, "course": course.title, "url": url, "status": "pending"}

    if course.cookies_file and not course.cookies_file.exists():
        record.update(status="error", reason=f"cookies_file não existe: {course.cookies_file}")
        write_manifest(out_dir, record)
        return record

    if prefer_ytdlp:
        ok, log = ytdlp_download(url, out_dir, stem, course.cookies_file, output_format=output_format)
        if ok:
            outputs = sorted(out_dir.glob("*.m4a"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not outputs or not verify_audio_output(outputs[0])[0]:
                record.update(status="error", reason="saída yt-dlp falhou verificação AAC-LC/44.1kHz/mono")
                write_manifest(out_dir, record)
                return record
            record.update(status="ok", method="yt-dlp", detail=log[-1000:])
            write_manifest(out_dir, record)
            return record
        record["ytdlp_error"] = log[-1500:]

    try:
        html = fetch_html(url, course.cookies_file)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        record.update(status="error", reason=f"não consegui abrir página autenticada: {e}")
        write_manifest(out_dir, record)
        return record

    title = page_title(html)
    if title:
        stem = f"{index:03d}-{slugify(title)}"
    candidates = media_candidates(html, url)
    record["candidates"] = candidates[:20]
    if not candidates:
        reason = "nenhuma URL de mídia encontrada na página"
        if looks_drm(html):
            reason += "; página contém sinais de DRM/streaming protegido"
        record.update(status="blocked", reason=reason)
        write_manifest(out_dir, record)
        return record

    errors = []
    for media_url in candidates:
        ok, detail = direct_download(media_url, out_dir, stem, course.cookies_file, referer=url, output_format=output_format)
        if ok:
            record.update(status="ok", method="scrape", media_url=media_url, detail=detail)
            write_manifest(out_dir, record)
            return record
        errors.append({"media_url": media_url, "error": detail})
    record.update(status="error", reason="candidatos encontrados, mas nenhum baixou", errors=errors[-5:])
    write_manifest(out_dir, record)
    return record


def command_download(args: argparse.Namespace) -> int:
    output_root, courses = load_config(Path(args.config).expanduser())
    if not courses:
        print("Config sem [[course]].", file=sys.stderr)
        return 2
    total = ok = 0
    for course in courses:
        print(f"## {course.provider} / {course.title}")
        if not course.urls:
            print("- sem URLs configuradas")
            continue
        urls = crawl_course_urls(course) if course.crawl else course.urls
        print(f"- URLs para processar: {len(urls)}" + (" (crawler)" if course.crawl else ""))
        for i, url in enumerate(urls, 1):
            total += 1
            rec = process_url(course, output_root, url, i, prefer_ytdlp=not args.no_ytdlp_first, output_format=args.audio_format)
            status = rec.get("status")
            ok += 1 if status == "ok" else 0
            reason = rec.get("reason") or rec.get("method") or ""
            print(f"- {status}: {url} {reason}")
    print(f"Resultado: {ok}/{total} baixados")
    return 0 if ok == total else 1


def command_audit(args: argparse.Namespace) -> int:
    cookies = expand_path(args.cookies_file)
    html = fetch_html(args.url, cookies)
    print(json.dumps({
        "title": page_title(html),
        "drm_hints": looks_drm(html),
        "media_candidates": media_candidates(html, args.url),
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Authenticated personal course audio downloader")
    p.add_argument("--config", default="config.toml", help="TOML config path")
    p.add_argument("--no-ytdlp-first", action="store_true", help="Skip first yt-dlp attempt and scrape page first")
    p.add_argument("--audio-format", choices=["m4a", "mp3"], default="m4a", help="Output audio format; default m4a for iPhone/Apple. Use mp3 only when explicitly requested.")
    p.add_argument("--mp3", dest="audio_format", action="store_const", const="mp3", help="Explicit shortcut for --audio-format mp3")
    sub = p.add_subparsers(dest="cmd")
    audit = sub.add_parser("audit-url", help="List media URLs found in one authenticated page")
    audit.add_argument("url")
    audit.add_argument("--cookies-file")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "audit-url":
        return command_audit(args)
    return command_download(args)


if __name__ == "__main__":
    raise SystemExit(main())
