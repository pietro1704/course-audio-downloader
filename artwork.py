"""Preserve attached artwork without re-encoding the validated audio stream."""
import hashlib
import json
import os
import subprocess


def _run(args):
    result = subprocess.run(args, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('artwork-processing-failed')
    return result.stdout


def embedded(source, folder):
    data = json.loads(_run(['ffprobe', '-v', 'error', '-show_streams',
                           '-of', 'json', str(source)]))
    covers = [s for s in data['streams'] if s.get('disposition', {}).get('attached_pic')]
    if not covers:
        return None
    stream = covers[0]
    suffix = '.jpg' if stream['codec_name'] == 'mjpeg' else '.png'
    path = folder / ('embedded-cover' + suffix)
    codec = 'copy' if stream['codec_name'] in {'mjpeg', 'png'} else 'png'
    _run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
          '-map', '0:' + str(stream['index']), '-frames:v', '1', '-c:v', codec, str(path)])
    if not path.is_file() or not 0 < path.stat().st_size <= 16 * 1024**2:
        raise RuntimeError('artwork-processing-failed')
    return path


def apply(source, encoded, candidate, adapter, folder):
    cover = embedded(source, folder)
    origin = 'embedded' if cover else 'none'
    fallback = getattr(adapter, 'thumbnail', None)
    if cover is None and fallback:
        cover = fallback(candidate, folder)
        origin = 'video' if cover else 'none'
    result = {'version': 1, 'origin': origin}
    if cover is None:
        return result
    temporary = folder / 'with-cover.m4a'
    _run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(encoded),
          '-i', str(cover), '-map', '0:a:0', '-map', '1:v:0', '-c', 'copy',
          '-disposition:v:0', 'attached_pic', str(temporary)])
    # Verify the actual attachment's bytes, not merely the presence of a video stream.
    attached = _run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(temporary),
                     '-map', '0:v:0', '-frames:v', '1', '-c:v', 'copy',
                     '-f', 'image2pipe', 'pipe:1'])
    if attached != cover.read_bytes():
        raise RuntimeError('artwork-verification-failed')
    result.update(sha256=hashlib.sha256(attached).hexdigest(), size=len(attached))
    os.replace(temporary, encoded)
    return result
