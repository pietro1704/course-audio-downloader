import io
import subprocess
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import ppr_bunny
from pipeline import Candidate


class FakeResponse:
    def __init__(self, body, content_type="video/mp2t", content_length=None):
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self.body.read(size)

    def getcode(self):
        return 200


class BunnyTransferTests(unittest.TestCase):
    def test_plain_segments_rejects_signed_non_https_url_without_fetching(self):
        playlist = "#EXTM3U\n#EXTINF:1,\nhttp://cdn.example/seg.ts?token=secret\n#EXT-X-ENDLIST"
        with self.assertRaisesRegex(RuntimeError, "unsafe-player-media-url"):
            ppr_bunny.plain_segments(
                "https://cdn.example/master.m3u8?sig=secret", playlist
            )

    def test_plain_segments_returns_duration_and_order(self):
        playlist = "\n".join(
            [
                "#EXTM3U",
                "#EXTINF:1.25,",
                "first.ts?sig=one",
                "#EXTINF:2.5,",
                "second.ts?sig=two",
                "#EXT-X-ENDLIST",
            ]
        )
        self.assertEqual(
            ppr_bunny.plain_segments(
                "https://cdn.example/master.m3u8?sig=master", playlist
            ),
            (
                [
                    "https://cdn.example/first.ts?sig=one",
                    "https://cdn.example/second.ts?sig=two",
                ],
                3.75,
            ),
        )

    def test_read_fragment_caps_response_before_returning_bytes(self):
        oversized = b"x" * (16 * 1024**2 + 1)
        with patch(
            "urllib.request.urlopen",
            return_value=FakeResponse(io.BytesIO(oversized)),
        ):
            with self.assertRaisesRegex(RuntimeError, "player-segment-size-budget"):
                ppr_bunny.read_fragment(
                    "https://cdn.example/too-large.ts", "https://example.test/lesson"
                )

    def test_copy_hls_audio_writes_fragments_in_playlist_order(self):
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "out.aac"
            playlist = "#EXTM3U\n#EXTINF:1,\none.ts\n#EXTINF:1,\ntwo.ts\n#EXT-X-ENDLIST"
            calls = []

            def open_fragment(request, timeout=30):
                url = request.full_url
                calls.append(url)
                body = {"https://cdn.example/one.ts": b"ONE", "https://cdn.example/two.ts": b"TWO"}[url]
                return FakeResponse(io.BytesIO(body), content_length=len(body))

            class FakeStdin(io.BytesIO):
                def close(self):
                    self.closed_bytes = self.getvalue()
                    super().close()

            class FakeProcess:
                def __init__(self):
                    self.stdin = FakeStdin()
                    self.returncode = 0

                def poll(self):
                    return self.returncode

                def wait(self, timeout=None):
                    destination.write_bytes(b"fake-aac")
                    return self.returncode

                def kill(self):
                    self.returncode = -9

            process = FakeProcess()
            with patch("ppr_bunny.get_text", return_value=playlist), patch(
                "urllib.request.urlopen", side_effect=open_fragment
            ), patch("subprocess.Popen", return_value=process), patch(
                "course_audio_bot._probe_duration", return_value=2.0
            ):
                result = ppr_bunny.copy_hls_audio(
                    Candidate("https://cdn.example/master.m3u8", "hls-audio"),
                    destination,
                    1024,
                    "https://example.test/lesson",
                )
            self.assertEqual(result, destination)
            self.assertEqual(process.stdin.closed_bytes, b"ONETWO")
            self.assertCountEqual(calls, ["https://cdn.example/one.ts", "https://cdn.example/two.ts"])

    def test_copy_hls_audio_duration_mismatch_is_reported_and_output_removed_or_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "out.m4a"
            playlist = "#EXTM3U\n#EXTINF:10,\none.ts\n#EXT-X-ENDLIST"
            from unittest.mock import MagicMock
            process = MagicMock()
            process.stdin.closed = True
            process.poll.return_value = 0
            def finish(timeout=None):
                destination.write_bytes(b"complete-but-short-source")
                return 0
            process.wait.side_effect = finish
            with patch("ppr_bunny.get_text", return_value=playlist), patch(
                "ppr_bunny.read_fragment", return_value=b"ts-fragment"
            ), patch("subprocess.Popen", return_value=process), patch(
                "course_audio_bot._probe_duration", return_value=1.0
            ) as probe:
                with self.assertRaisesRegex(RuntimeError, "player-audio-download-failed"):
                    ppr_bunny.copy_hls_audio(
                        Candidate("https://cdn.example/master.m3u8?token=PRIVATE", "hls-audio"),
                        destination, 100000, "https://example.test/lesson",
                    )
                probe.assert_called_once_with(destination)
                self.assertTrue(destination.exists())  # preserved for pipeline quarantine

    def test_expired_fragment_url_is_sanitized_after_bounded_retries(self):
        from urllib.error import HTTPError
        url='https://cdn.example/seg.ts?token=PRIVATE'
        with patch('urllib.request.urlopen',side_effect=HTTPError(url,403,'expired',{},None)) as opener:
            with self.assertRaisesRegex(RuntimeError,'^player-segment-fetch-failed$') as failure:
                ppr_bunny.read_fragment(url,'https://example.test/player')
            self.assertNotIn('PRIVATE',str(failure.exception))
            self.assertEqual(opener.call_count,3)

    def test_broken_pipe_close_does_not_override_sanitized_failure(self):
        from unittest.mock import MagicMock
        class BadPipe:
            closed=False
            def write(self,data): raise BrokenPipeError('PRIVATE')
            def close(self):
                self.closed=True
                raise BrokenPipeError('PRIVATE')
        process=MagicMock();process.stdin=BadPipe();process.poll.return_value=None
        playlist='#EXTM3U\n#EXTINF:1,\none.ts\n#EXT-X-ENDLIST'
        with tempfile.TemporaryDirectory() as td, patch('ppr_bunny.get_text',return_value=playlist), patch(
            'ppr_bunny.read_fragment',return_value=b'TS'
        ), patch('subprocess.Popen',return_value=process):
            with self.assertRaisesRegex(RuntimeError,'^player-audio-download-failed$'):
                ppr_bunny.copy_hls_audio(Candidate('https://cdn.example/index.m3u8','hls-audio'),Path(td)/'out.m4a',1024,'https://example.test/player')
            process.kill.assert_called_once()

    def test_real_ffmpeg_pipe_remuxes_synthetic_ts_with_mocked_http(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = td / "source.ts"
            destination = td / "out.aac"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                 "-c:a", "aac", "-b:a", "32k", "-f", "mpegts", str(source)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            body = source.read_bytes()
            playlist = "#EXTM3U\n#EXTINF:1,\none.ts\n#EXT-X-ENDLIST"
            with patch("ppr_bunny.get_text", return_value=playlist), patch(
                "urllib.request.urlopen", return_value=FakeResponse(io.BytesIO(body), content_length=len(body))
            ):
                result = ppr_bunny.copy_hls_audio(
                    Candidate("https://cdn.example/master.m3u8", "hls-audio"),
                    destination, 1_000_000, "https://example.test/lesson",
                )
            self.assertEqual(result, destination)
            self.assertGreater(destination.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
