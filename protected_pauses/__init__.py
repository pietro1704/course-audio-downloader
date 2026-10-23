"""Opt-in, bounded PCM pause compaction. Never writes the source recording."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
import wave

MODEL_HASH = '1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3'
PROFILE = {'name': 'protected-v1', 'model_sha256': MODEL_HASH,
           'silero_thresholds': [.35, .20], 'silero_frame': 512, 'silero_context': 64,
           'silero_state_shape': [2, 1, 128], 'webrtc_mode': 3, 'webrtc_frame': 160,
           'margin_ms': 50, 'peak_int16': 103, 'minimum_ms': 160,
           'retained_ms': 60, 'fade_ms': 5, 'edge_ms': 1000,
           'analysis': 'ffmpeg-swr-mono-s16-16000', 'unit': 'lesson'}
MAX_PCM_BYTES = 2 * 1024**3


def check_cancel(stop):
    if stop.is_set():
        raise RuntimeError('delivery-cancelled')


def command(args, stop, timeout=7200):
    """Poll children for cancellation; keep private codec errors off public logs."""
    check_cancel(stop)
    with subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
        deadline = time.monotonic() + timeout
        try:
            while child.poll() is None:
                check_cancel(stop)
                if time.monotonic() >= deadline:
                    raise RuntimeError('silence-processing-timeout')
                stop.wait(.05)
            if child.returncode:
                raise RuntimeError('silence-codec-failed')
        except BaseException:
            child.kill()
            child.wait()
            raise
    check_cancel(stop)


def project(scores, web, count):
    import numpy as np
    active, flags = False, []
    for score in scores:
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise RuntimeError('silence-inference-failed')
        active = score >= (.20 if active else .35)
        flags.append(active)
    if not flags or not web or count <= 0:
        raise RuntimeError('silence-inference-failed')
    output = np.zeros(count, bool)
    for i in range(count):
        # All times in integer 16kHz samples, avoiding boundary rounding.
        lo, hi = i*160//512, ((i+1)*160-1)//512
        output[i] = any(flags[lo:min(hi+1, len(flags))]) or web[min(i, len(web)-1)]
    return output


class Detectors:
    def __init__(self, model=None):
        import numpy as np
        import onnxruntime as ort
        import webrtcvad
        model = Path(model) if model else Path(__file__).parent/'assets/silero_vad.onnx'
        if hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_HASH:
            raise RuntimeError('silence-model-hash-mismatch')
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model), sess_options=opts,
                                            providers=['CPUExecutionProvider'])

    def speech(self, analysis, count, stop):
        import numpy as np
        import webrtcvad
        web_vad = webrtcvad.Vad(3)
        state = np.zeros((2, 1, 128), np.float32)
        context = np.zeros((1, 64), np.float32)
        scores, web = [], []
        try:
            for i in range(0, len(analysis), 512):
                check_cancel(stop)
                chunk = analysis[i:i+512].astype(np.float32)/32768
                chunk = np.pad(chunk, (0, 512-len(chunk)))[None, :]
                inputs = np.concatenate([context, chunk], axis=1)
                score, state = self.session.run(None, {'input': inputs, 'state': state,
                                                       'sr': np.array(16000, np.int64)})
                if state.shape != (2, 1, 128) or not np.isfinite(state).all():
                    raise RuntimeError('silence-inference-failed')
                context = inputs[:, -64:].copy()
                scores.append(float(score[0, 0]))
            for i in range(0, len(analysis), 160):
                check_cancel(stop)
                chunk = analysis[i:i+160]
                web.append(web_vad.is_speech(np.pad(chunk, (0, 160-len(chunk))).astype('<i2').tobytes(), 16000))
            return project(scores, web, count)
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError('silence-inference-failed') from None


def plan(pcm, rate, speech, stop=None):
    """Integer frame map; peak guard sees every channel at the editing rate."""
    import numpy as np
    total = len(pcm)
    count = (total*100 + rate-1)//rate
    if len(speech) != count or total <= 0 or rate <= 0:
        raise ValueError('silence-invalid-pcm')
    # Dilation in 10ms cells, including short (<11-cell) lessons.
    protected = np.zeros(count, bool)
    for shift in range(-min(5, count-1), min(5, count-1)+1):
        if shift < 0:
            protected[:shift] |= speech[-shift:]
        elif shift:
            protected[shift:] |= speech[:-shift]
        else:
            protected |= speech
    eligible = ~protected
    for i in range(count):
        if stop is not None and i % 100 == 0:
            check_cancel(stop)
        a, b = i*rate//100, min(total, (i+1)*rate//100)
        if a < rate or b > total-rate or b <= a:
            eligible[i] = False
        elif np.abs(pcm[a:b].astype(np.int32)).max() > 103:
            eligible[i] = False
    removed, start = [], None
    for i in range(count+1):
        active = i < count and eligible[i]
        if active and start is None:
            start = i
        if not active and start is not None:
            a, b = start*rate//100, min(total, i*rate//100)
            if (b-a)*1000 >= rate*160:
                removed.append([a+round(rate*.03), b-round(rate*.03)])
            start = None
    kept, previous, offset = [], 0, 0
    for a, b in removed:
        kept.append({'source_start': previous, 'source_end': a,
                     'output_start': offset, 'output_end': offset+a-previous})
        offset += a-previous
        previous = b
    kept.append({'source_start': previous, 'source_end': total,
                 'output_start': offset, 'output_end': offset+total-previous})
    return {'profile': dict(PROFILE), 'sample_rate': rate, 'channels': pcm.shape[1],
            'input_frames': total, 'output_frames': offset+total-previous,
            'input_seconds': total/rate, 'output_seconds': (offset+total-previous)/rate,
            'removed': removed, 'mapping': kept}


def render(pcm, rate, receipt, destination, stop):
    import numpy as np
    mapping = receipt['mapping']
    with wave.open(str(destination), 'wb') as out:
        out.setnchannels(pcm.shape[1]); out.setsampwidth(2); out.setframerate(rate)
        for index, item in enumerate(mapping):
            a, b = item['source_start'], item['source_end']
            fade = min(round(rate*.005), (b-a)//2)
            for pos in range(a, b, 65536):
                check_cancel(stop)
                end = min(b, pos+65536)
                chunk = pcm[pos:end].copy()
                for left, right, up in [(a, a+fade, True), (b-fade, b, False)]:
                    if (up and not index) or (not up and index == len(mapping)-1) or not fade:
                        continue
                    lo, hi = max(pos, left), min(end, right)
                    if lo >= hi:
                        continue
                    weights = np.linspace(0, 1, fade) if up else np.linspace(1, 0, fade)
                    part = chunk[lo-pos:hi-pos]
                    # Refuse to modify a loud sample even at an unexpected junction.
                    low = np.abs(part.astype(np.int32)) <= 103
                    faded = np.rint(part*weights[lo-left:hi-left, None]).astype('<i2')
                    part[low] = faded[low]
                out.writeframesraw(chunk.astype('<i2').tobytes())


class Compactor:
    def __init__(self):
        try:
            import numpy, onnxruntime, webrtcvad
        except ImportError:
            raise RuntimeError('silence-dependency-missing') from None
        # Identity includes runtime/resampler; each audio owns its recurrent state.
        version = subprocess.check_output(['ffmpeg', '-version'], text=True).splitlines()[0]
        self.identity = {**PROFILE, 'ffmpeg': version, 'numpy': numpy.__version__,
                         'onnxruntime': onnxruntime.__version__, 'webrtcvad': webrtcvad.__version__}

    def budget(self, source):
        data = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                        '-show_streams', '-of', 'json', str(source)], timeout=60))['streams'][0]
        rate, channels = int(data['sample_rate']), int(data['channels'])
        if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
            raise RuntimeError('silence-invalid-pcm')
        return rate, channels

    def prepare(self, source, work, stop, reserve):
        import numpy as np
        import course_audio_bot as audio
        rate, channels = self.budget(source)
        duration = audio._decoded_audio_duration(source)
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise RuntimeError('silence-invalid-pcm')
        pcm_bytes = math.ceil((duration+1)*rate)*channels*2
        if pcm_bytes > MAX_PCM_BYTES:
            raise RuntimeError('silence-pcm-budget-exceeded')
        # decoded PCM + edited WAV + analysis + encoded/remux headroom.
        if not reserve(2*pcm_bytes + math.ceil((duration+1)*32000) + 32*1024**2):
            raise RuntimeError('disk-reserve')
        raw, analysis = work/'source.s16', work/'analysis.s16'
        base = ['ffmpeg', '-v', 'error', '-xerror', '-nostdin', '-y']
        command([*base, '-i', str(source), '-map', '0:a:0', '-vn', '-f', 's16le',
                 '-c:a', 'pcm_s16le', '-fs', str(pcm_bytes), str(raw)], stop)
        if not 0 < raw.stat().st_size < pcm_bytes or raw.stat().st_size % (channels*2):
            raise RuntimeError('silence-pcm-budget-exceeded')
        pcm = np.memmap(raw, mode='r', dtype='<i2').reshape(-1, channels)
        if abs(len(pcm)/rate-duration) > .1:
            raise RuntimeError('silence-invalid-pcm')
        command([*base, '-f', 's16le', '-ar', str(rate), '-ac', str(channels), '-i', str(raw),
                 '-ac', '1', '-ar', '16000', '-af', 'aresample=resampler=swr', '-f', 's16le', str(analysis)], stop)
        mono = np.memmap(analysis, mode='r', dtype='<i2')
        if abs(len(mono)/16000-len(pcm)/rate) > 1/16000:
            raise RuntimeError('silence-invalid-pcm')
        detectors = Detectors()
        count = (len(pcm)*100+rate-1)//rate
        speech = detectors.speech(mono, count, stop)
        receipt = plan(pcm, rate, speech, stop)
        receipt['identity'] = dict(self.identity)
        destination = work/'compacted.wav'
        render(pcm, rate, receipt, destination, stop)
        (work/'silence-receipt.json').write_text(json.dumps(receipt))
        return destination, receipt

    @staticmethod
    def metadata(source, encoded, work, stop):
        temporary = work/'with-metadata.m4a'
        command(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(encoded), '-i', str(source),
                 '-map', '0:a:0', '-map_metadata', '1', '-map_metadata:s:a:0', '1:s:a:0',
                 '-c', 'copy', str(temporary)], stop)
        temporary.replace(encoded)

    @staticmethod
    def validate(encoded, receipt, stop):
        import course_audio_bot as audio
        duration = audio._probe_duration(encoded)
        expected = receipt['output_frames']/receipt['sample_rate']
        # AAC packet padding must not license missing seconds of content.
        if not (duration is not None and abs(duration-expected) <= .1
                and audio.verify_audio_output(encoded)[0]):
            return False
        command(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(encoded),
                 '-map', '0:a:0', '-f', 'null', '-'], stop)
        return True
