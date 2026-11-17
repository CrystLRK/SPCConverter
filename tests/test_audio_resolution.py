from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from audio_resolution import (  # noqa: E402
    AudioDiscoveryError,
    detect_audio_format,
    discover_audio_files,
    resolve_discovered_audio,
)


class AudioResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="audio-resolution-tests-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def write_audio(self, relative: str, header: bytes) -> Path:
        path = self.temp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header)
        return path

    def test_detects_supported_audio_by_header(self) -> None:
        ogg = self.write_audio("nested/song.bin", b"OggS\0\0\0\0")
        wav = self.write_audio("nested/song.dat", b"RIFF\0\0\0\0WAVE")
        mp3 = self.write_audio("nested/song.txt", b"ID3\4\0\0")
        invalid = self.write_audio("nested/not-audio.ogg", b"not audio")
        self.assertEqual(detect_audio_format(ogg), "OGG")
        self.assertEqual(detect_audio_format(wav), "WAV")
        self.assertEqual(detect_audio_format(mp3), "MP3")
        self.assertIsNone(detect_audio_format(invalid))
        self.assertEqual(
            discover_audio_files(self.temp),
            tuple(sorted((ogg.resolve(), wav.resolve(), mp3.resolve()))),
        )

    def test_unique_audio_is_selected_automatically(self) -> None:
        expected = self.write_audio("one/song.ogg", b"OggS\0\0\0\0")
        selected = resolve_discovered_audio(
            self.temp,
            "missing.ogg",
            "歌曲音频",
        )
        self.assertEqual(selected, expected.resolve())

    def test_multiple_audio_requires_chooser(self) -> None:
        first = self.write_audio("a.ogg", b"OggS\0\0\0\0")
        self.write_audio("b.mp3", b"ID3\4\0\0")
        with self.assertRaisesRegex(AudioDiscoveryError, "多个有效音频"):
            resolve_discovered_audio(
                self.temp,
                "missing.ogg",
                "歌曲音频",
            )

        def choose(_configured, _label, candidates):
            self.assertEqual(len(candidates), 2)
            return first

        self.assertEqual(
            resolve_discovered_audio(
                self.temp,
                "missing.ogg",
                "歌曲音频",
                choose,
            ),
            first.resolve(),
        )

    def test_cancel_and_empty_discovery_fail_closed(self) -> None:
        with self.assertRaisesRegex(AudioDiscoveryError, "没有找到"):
            resolve_discovered_audio(
                self.temp,
                "missing.ogg",
                "歌曲音频",
            )
        self.write_audio("a.ogg", b"OggS\0\0\0\0")
        with self.assertRaisesRegex(AudioDiscoveryError, "取消"):
            resolve_discovered_audio(
                self.temp,
                "missing.ogg",
                "歌曲音频",
                lambda *_args: None,
            )


if __name__ == "__main__":
    unittest.main()
