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
    for raw in re.findall(r'<iframe[^>]+src=["\']([^"\']+)', html, re.I):
        url = urllib.parse.urljoin(base_url, raw)
        if urllib.parse.urlsplit(url).scheme == 'https': found.append(url)
    for raw in URL_RE.findall(html):
        url = clean_media_url(raw)
        if "/embed/" in urllib.parse.urlsplit(url).path:
            found.append(url)
        elif is_media_like_url(url):
            found.append(url)
    for raw in SRC_RE.findall(html):
        url = urllib.parse.urljoin(base_url, raw.replace("\\/", "/"))
        if "/embed/" in urllib.parse.urlsplit(url).path:
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
    out.sort(key=lambda u: 0 if is_media_like_url(u) else 1)
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


def _probe_duration(path: Path) -> float | None:
    rc, out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(out.strip()) if rc == 0 else None
    except ValueError:
        return None

def _decoded_audio_duration(path: Path) -> float | None:
    # Header estimates can be wrong for MP3 without a trustworthy VBR index.
    # Decode every audio frame and count samples instead of widening tolerance.
    rc, out = run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                   "stream=sample_rate:frame=nb_samples", "-of", "json", str(path)])
    if rc != 0:
        return None
    try:
        data = json.loads(out)
        rate = int(data['streams'][0]['sample_rate'])
        samples = [int(frame['nb_samples']) for frame in data['frames']]
        if rate <= 0 or not samples or any(count <= 0 for count in samples):
            return None
        return sum(samples) / rate
    except (ValueError, TypeError, KeyError, IndexError):
        return None

def verify_audio_output(path: Path, source: Path | None = None) -> tuple[bool, str]:
    if not path.exists() or path.stat().st_size == 0:
        return False, "arquivo ausente/vazio"
    rc, out = run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,profile,sample_rate,channels,bit_rate", "-of", "json", str(path)])
    if rc != 0:
        return False, "ffprobe falhou"
    try: stream = json.loads(out)["streams"][0]
    except (KeyError, IndexError, json.JSONDecodeError): return False, "sem stream de áudio"
    expected = {"codec_name":"aac", "profile":"LC", "sample_rate":"44100", "channels":"1"}
    if any(str(stream.get(k)) != v for k,v in expected.items()): return False, f"parâmetros inválidos: {stream}"
    bitrate = int(stream.get("bit_rate") or 0)
    if not 1000 <= bitrate <= 64000: return False, f"bitrate inválido: {bitrate}"
    duration = _probe_duration(path)
    if duration is None or duration <= 0: return False, "duração inválida"
    if source is not None:
        source_duration = _probe_duration(source)
        if source_duration is None:
            return False, f"duração divergente: fonte={source_duration} saída={duration}"
        if abs(duration-source_duration) > max(0.25, min(2.0, source_duration*0.02)):
            decoded_source = _decoded_audio_duration(source)
            decoded_output = _decoded_audio_duration(path)
            if (decoded_source is None or decoded_output is None
                    or abs(decoded_output-decoded_source) > max(0.25, min(2.0, decoded_source*0.02))):
                return False, f"duração divergente: fonte={source_duration} saída={duration}"
    rc, _ = run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"])
    return (rc == 0, "ok" if rc == 0 else "decode falhou")

def compress_audio_file(path: Path, output_format: str = "m4a") -> tuple[Path | None, str]:
    if path.suffix.lower() not in AUDIO_EXTS + VIDEO_EXTS: return None, "not-media"
    output_format = output_format.lower()
    if output_format not in {"m4a", "mp3"}: raise ValueError(f"unsupported output_format: {output_format}")
    final_suffix = ".m4a" if output_format == "m4a" else ".mp3"
    tmp = path.with_suffix(path.suffix + f".compressed{final_suffix}")
    codec_args = ["-c:a", "aac"] if output_format == "m4a" else []
    rc, out = run(["ffmpeg", "-y", "-i", str(path), "-vn", *codec_args, "-ac", "1", "-ar", "44100", "-b:a", "32k", str(tmp)], timeout=int(os.environ.get("COURSE_AUDIO_YTDLP_TIMEOUT", "7200")))
    if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True); return None, "compress-failed: " + out[-500:]
    valid, detail = verify_audio_output(tmp, source=path) if output_format == "m4a" else (True, "ok")
    if not valid: tmp.unlink(missing_ok=True); return None, "verification-failed: " + detail
    final = path.with_suffix(final_suffix)
    size_note = f"encoded {path.stat().st_size}->{tmp.stat().st_size} bytes"
    tmp.replace(final)
    return final, size_note

def _promote_verified(candidate: Path, out_dir: Path, stem: str, source: Path | None, output_format: str) -> tuple[bool,str]:
    if candidate.suffix.lower() == ".m4a":
        valid, detail = verify_audio_output(candidate, source=source)
    else:
        valid = candidate.exists() and candidate.stat().st_size > 0 and _probe_duration(candidate) is not None
        detail = "ok" if valid else "invalid encoded output"
        if valid:
            rc, _ = run(["ffmpeg", "-v", "error", "-i", str(candidate), "-f", "null", "-"])
            valid = rc == 0
            detail = "ok" if valid else "decode falhou"
    if not valid: return False, "verification-failed: " + detail
    promoted = out_dir / (slugify(stem) + (".m4a" if output_format == "m4a" else ".mp3"))
    candidate.replace(promoted)
    return True, str(promoted)

def ytdlp_download(url: str, out_dir: Path, stem_hint: str, cookies_file: Path | None, referer: str | None = None, output_format: str = "m4a") -> tuple[bool,str]:
    if not have("yt-dlp"): return False, "yt-dlp não encontrado"
    final = out_dir / f"{slugify(stem_hint)}.{output_format}"
    if final.exists() and final.stat().st_size > 0:
        valid, detail = verify_audio_output(final) if output_format == "m4a" else (True, "ok")
        if valid: return True, f"já existe verificado: {final}"
        final.unlink(missing_ok=True)
    staging = out_dir / ".staging"; originals = out_dir / "originals"; staging.mkdir(exist_ok=True); originals.mkdir(exist_ok=True)
    source_tmpl = staging / f"{slugify(stem_hint)}.%(ext)s"
    cmd=["yt-dlp","--no-progress","--ignore-config","--no-overwrites","--restrict-filenames","-f","bestaudio/best","--keep-video","-o",str(source_tmpl)]
    if referer: cmd += ["--referer",referer,"--add-header",f"Origin:{urllib.parse.urlparse(referer).scheme}://{urllib.parse.urlparse(referer).netloc}"]
    if cookies_file: cmd += ["--cookies",str(cookies_file)]
    cmd.append(url); rc,out=run(cmd,timeout=int(os.environ.get("COURSE_AUDIO_YTDLP_TIMEOUT","7200")))
    if rc != 0 or "ERROR:" in out: return False,out[-4000:]
    candidates=sorted((p for p in staging.glob(f"{slugify(stem_hint)}.*") if ".compressed" not in p.name), key=lambda p:p.stat().st_mtime, reverse=True)
    candidate=next((p for p in candidates if p.suffix.lower() in AUDIO_EXTS + VIDEO_EXTS and p.is_file()),None)
    if candidate is None: return False,"yt-dlp não produziu saída exata"
    shutil.copy2(candidate, originals/candidate.name)
    if candidate.suffix.lower() == ".m4a" and output_format == "m4a":
        ok,detail=_promote_verified(candidate,out_dir,stem_hint,originals/candidate.name,output_format)
    else:
        converted,detail=compress_audio_file(candidate,output_format); ok=converted is not None
        if ok: ok,detail=_promote_verified(converted,out_dir,stem_hint, candidate,output_format)
    return (ok, detail if ok else str(detail))
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


def direct_download(url: str, out_dir: Path, stem: str, cookies_file: Path | None, referer: str | None = None, output_format: str = "m4a") -> tuple[bool,str]:
    suffix=infer_suffix_from_url(url)
    if suffix in STREAM_EXTS or suffix in VIDEO_EXTS or "/embed/" in urllib.parse.urlsplit(url).path:
        return ytdlp_download(url,out_dir,stem,cookies_file,referer,output_format)
    desired=out_dir/(slugify(stem)+f".{output_format}")
    if desired.exists() and desired.stat().st_size>0:
        valid, detail = verify_audio_output(desired) if output_format == "m4a" else (True, "ok")
        if valid: return True,f"já existe verificado: {desired}"
        desired.unlink(missing_ok=True)
    staging=out_dir/".staging"; originals=out_dir/"originals"; staging.mkdir(exist_ok=True); originals.mkdir(exist_ok=True)
    dest=staging/(slugify(stem)+suffix)
    if not dest.exists() or dest.stat().st_size==0:
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 course-audio-downloader/0.1",**({"Referer":referer} if referer else {})})
            with cookies_opener(cookies_file).open(req,timeout=120) as resp,dest.open("wb") as f: shutil.copyfileobj(resp,f)
        except Exception as e: dest.unlink(missing_ok=True); return False,f"falha download direto: {e}"
    shutil.copy2(dest,originals/dest.name)
    converted,detail=compress_audio_file(dest,output_format)
    if converted is None: return False,detail
    ok,pdetail=_promote_verified(converted,out_dir,stem,dest,output_format)
    if ok: dest.unlink(missing_ok=True)
    return ok,pdetail if ok else pdetail
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
