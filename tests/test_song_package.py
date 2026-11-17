from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = Path(__file__).resolve().parents[2]
TEST_CHARTS = ROOT / "testdata" / "charts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from song_package import (  # noqa: E402
    DifficultyInput,
    SongPackageSpec,
    create_pack_folder,
    ensure_default_pack,
    export_song_folder,
    export_song_package,
)
import song_package as song_package_module  # noqa: E402


class SongPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="spc-song-package-tests-"))
        self.audio = (
            ROOT
            / "In Falsus 1.0.5"
            / "CustomCharts"
            / "User Charts"
            / "When_the_rain"
            / "audio.ogg"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def spec(self) -> SongPackageSpec:
        return SongPackageSpec(
            output_root=self.temp / "CustomCharts",
            folder_name="test_song",
            chart_id="test_song",
            base_name="test_song",
            song_title="Test Song",
            artist="Test Artist",
            chart_designer="Tester",
            jacket_designer="Cover",
            audio=self.audio,
            jacket_large=None,
            jacket_small=None,
            difficulties=[
                DifficultyInput(
                    0, TEST_CHARTS / "demo_scenecontrol.spc", 2, "1"
                ),
                DifficultyInput(1, ROOT / "charts" / "alamode0.spc", 8, "2"),
            ],
        )

    def prepare_default_pack(self) -> None:
        ensure_default_pack(self.temp / "CustomCharts")

    def test_current_folder_package(self) -> None:
        self.prepare_default_pack()
        result = export_song_package(self.spec())
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertEqual(config["schemaVersion"], 1)
        self.assertEqual(result.config.name, "if")
        self.assertEqual(config["name"], "Test Song")
        self.assertEqual(config["composer"], "Test Artist")
        self.assertEqual(config["song"], "audio.ogg")
        self.assertEqual(config["chart"][0], "test_song0.spc")
        self.assertEqual(config["chart"][1], "test_song1.spc")
        self.assertIsNone(config["chart"][2])
        self.assertIsNone(config["chart"][3])
        self.assertEqual(result.pack_folder.name, "User Charts")
        self.assertTrue(result.pack_config.is_file())
        self.assertEqual(result.pack_config.name, "pack.json")
        pack = json.loads(
            result.pack_config.read_text(encoding="utf-8")
        )
        self.assertEqual(pack["name"], "User Charts")
        self.assertEqual(pack["id"], "user-charts")
        self.assertEqual(result.charts[0].read_bytes()[:4], b"ICP1")
        self.assertEqual(result.charts[1].read_bytes()[:4], b"ICP1")
        self.assertEqual(result.jacket_large.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(result.jacket_small.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_overwrite_restores_previous_folder_when_publish_fails(self) -> None:
        self.prepare_default_pack()
        first = export_song_package(self.spec())
        marker = first.folder / "old-marker.txt"
        marker.write_text("old", encoding="utf-8")

        package_spec = self.spec()
        package_spec.overwrite = True
        real_replace = song_package_module.os.replace
        def flaky_replace(source: Path, target: Path) -> None:
            if (
                Path(source).name.startswith(".spc-merge-")
                and Path(target).name == "test_song"
            ):
                raise OSError("simulated publish failure")
            real_replace(source, target)

        with patch("song_package.os.replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(
                song_package_module.ConversionError,
                "旧目录已恢复",
            ):
                export_song_package(package_spec)

        self.assertTrue(first.folder.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "old")
        self.assertFalse(list(first.folder.parent.glob("test_song.backup_*")))
        self.assertFalse(list(first.folder.parent.glob(".spc-package-*")))
        self.assertFalse(list(first.folder.parent.glob(".spc-merge-*")))

    def test_import_merge_preserves_unselected_difficulty(self) -> None:
        self.prepare_default_pack()
        first = export_song_package(self.spec())
        retained = first.folder / "test_song1.spc"
        retained_hash = retained.read_bytes()

        incoming = self.spec()
        incoming.song_title = "Updated Song"
        incoming.difficulties = [
            DifficultyInput(0, TEST_CHARTS / "demo_scenecontrol.spc", 9, "R9"),
        ]
        result = export_song_package(incoming)
        config = json.loads(result.config.read_text(encoding="utf-8"))

        self.assertEqual(config["name"], "Updated Song")
        self.assertEqual(config["chart"][0], "test_song0.spc")
        self.assertEqual(config["chart"][1], "test_song1.spc")
        self.assertEqual(retained.read_bytes(), retained_hash)
        self.assertEqual(result.charts[0].name, "test_song0.spc")

    def test_all_empty_difficulties_are_rejected(self) -> None:
        self.prepare_default_pack()
        package_spec = self.spec()
        empty = self.temp / "empty.spc"
        empty.write_text("chart(120,4)\n", encoding="utf-8")
        package_spec.difficulties = [
            DifficultyInput(0, empty, 1, "1"),
            DifficultyInput(1, self.temp / "missing.spc", 1, "1"),
        ]
        with self.assertRaisesRegex(
            song_package_module.ConversionError,
            "所有选择难度均为空",
        ):
            export_song_package(package_spec)

    def test_successful_overwrite_removes_backup_directory(self) -> None:
        self.prepare_default_pack()
        export_song_package(self.spec())
        package_spec = self.spec()
        package_spec.overwrite = True
        result = export_song_package(package_spec)
        self.assertFalse(
            list(result.folder.parent.glob("test_song.backup_*"))
        )

    def test_sparse_difficulties_are_named_by_semantics(self) -> None:
        self.prepare_default_pack()
        package_spec = self.spec()
        package_spec.difficulties = [
            DifficultyInput(
                2, TEST_CHARTS / "demo_scenecontrol.spc", 14, "1"
            ),
            DifficultyInput(3, ROOT / "charts" / "alamode0.spc", 15, "2"),
        ]
        result = export_song_package(package_spec)
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertIsNone(config["chart"][0])
        self.assertIsNone(config["chart"][1])
        self.assertEqual(config["chart"][2], "test_song2.spc")
        self.assertEqual(config["chart"][3], "test_song3.spc")
        self.assertEqual(config["lv"][2], 14)
        self.assertEqual(config["lv"][3], 15)

    def test_audio_output_extension_uses_file_header(self) -> None:
        self.prepare_default_pack()
        mislabeled_audio = self.temp / "audio.bin"
        mislabeled_audio.write_bytes(b"ID3\4\0\0")
        package_spec = self.spec()
        package_spec.audio = mislabeled_audio
        result = export_song_package(package_spec)
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertEqual(config["song"], "audio.mp3")
        self.assertTrue((result.folder / "audio.mp3").is_file())

    def test_default_level_indicator_matches_rating(self) -> None:
        self.prepare_default_pack()
        package_spec = self.spec()
        package_spec.difficulties[0].level_section_indicator = ""
        package_spec.difficulties[1].level_section_indicator = ""
        result = export_song_package(package_spec)
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertEqual(config["sp"][0], "2")
        self.assertEqual(config["sp"][1], "8")

    def test_background_is_copied_and_referenced(self) -> None:
        self.prepare_default_pack()
        package_spec = self.spec()
        package_spec.difficulties[0].background = (
            ROOT
            / "In Falsus 1.0.5"
            / "CustomCharts"
            / "User Charts"
            / "cyaegha"
            / "background.png"
        )
        result = export_song_package(package_spec)
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertEqual(config["background"][0], "background_0.png")
        self.assertTrue((result.folder / "background_0.png").is_file())

    def test_song_level_background_is_default_for_each_difficulty(self) -> None:
        self.prepare_default_pack()
        package_spec = self.spec()
        package_spec.background = (
            ROOT
            / "In Falsus 1.0.5"
            / "CustomCharts"
            / "User Charts"
            / "cyaegha"
            / "background.png"
        )
        result = export_song_package(package_spec)
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertEqual(config["background"][0], "background.png")
        self.assertEqual(config["background"][1], "background.png")
        self.assertTrue((result.folder / "background.png").is_file())

    def test_song_background_uses_first_available_difficulty(self) -> None:
        self.prepare_default_pack()
        package_spec = self.spec()
        package_spec.difficulties[1].background = (
            ROOT
            / "In Falsus 1.0.5"
            / "CustomCharts"
            / "User Charts"
            / "cyaegha"
            / "background.png"
        )
        result = export_song_package(package_spec)
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertIsNone(config["background"][0])
        self.assertEqual(config["background"][1], "background_1.png")

    def test_jpeg_content_with_png_extension_is_normalized(self) -> None:
        from PIL import Image

        self.prepare_default_pack()
        mislabeled = self.temp / "jacket.png"
        Image.new("RGB", (32, 32), (120, 40, 200)).save(
            mislabeled,
            "JPEG",
        )
        package_spec = self.spec()
        package_spec.jacket_large = mislabeled
        package_spec.jacket_small = mislabeled
        result = export_song_package(package_spec)
        self.assertEqual(
            result.jacket_large.read_bytes()[:8],
            b"\x89PNG\r\n\x1a\n",
        )
        self.assertEqual(
            result.jacket_small.read_bytes()[:8],
            b"\x89PNG\r\n\x1a\n",
        )

    def test_pack_config_and_preview(self) -> None:
        preview = self.audio
        package_spec = self.spec()
        package_spec.pack_folder_name = "Custom Pack"
        package_spec.pack_id = "custom-pack"
        package_spec.pack_title = "Custom Pack"
        package_spec.preview_audio = preview
        package_spec.preview_audio_start = 3.0
        package_spec.preview_audio_end = 5.0
        create_pack_folder(
            package_spec.output_root,
            "Custom Pack",
            "custom-pack",
            "Custom Pack",
        )
        result = export_song_package(package_spec)
        pack = json.loads(result.pack_config.read_text(encoding="utf-8"))
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertEqual(pack["id"], "custom-pack")
        self.assertEqual(pack["name"], "Custom Pack")
        self.assertEqual(config["preview_audio"], "preview.ogg")
        self.assertEqual(config["preview_seconds"], [3.0, 5.0])
        self.assertTrue((result.folder / "preview.ogg").is_file())

    def test_export_requires_existing_pack(self) -> None:
        with self.assertRaisesRegex(
            song_package_module.ConversionError,
            "目标曲包不存在",
        ):
            export_song_package(self.spec())

    def test_standalone_song_folder_has_no_pack_metadata(self) -> None:
        package_spec = self.spec()
        result = export_song_folder(package_spec)
        self.assertIsNone(result.pack_folder)
        self.assertIsNone(result.pack_config)
        self.assertEqual(result.folder, self.temp / "CustomCharts" / "test_song")
        self.assertTrue(result.config.is_file())
        self.assertEqual(result.config.name, "if")
        self.assertFalse((result.folder.parent / "pack.json").exists())
        self.assertEqual(
            sorted(path.name for path in result.charts),
            ["test_song0.spc", "test_song1.spc"],
        )

    def test_export_does_not_modify_existing_pack_metadata(self) -> None:
        pack = create_pack_folder(
            self.temp / "CustomCharts",
            "Existing Pack",
            "existing-pack",
            "Existing Pack",
            artist="Original Artist",
        )
        original_config = pack.folder.joinpath("pack.json").read_bytes()
        package_spec = self.spec()
        package_spec.pack_folder_name = "Existing Pack"
        export_song_package(package_spec)
        self.assertEqual(
            pack.folder.joinpath("pack.json").read_bytes(),
            original_config,
        )
        self.assertFalse((pack.folder / "pack_songSelect.png").exists())

    def test_default_pack_uses_fixed_values(self) -> None:
        pack, created = ensure_default_pack(self.temp / "CustomCharts")
        self.assertTrue(created)
        self.assertEqual(pack.folder.name, "User Charts")
        self.assertEqual(pack.pack_id, "user-charts")
        self.assertEqual(pack.title, "User Charts")
        self.assertEqual(pack.style_pack_slug, "act-4")
        self.assertEqual(pack.sort_order, 0)

    def test_empty_pack_metadata_is_omitted(self) -> None:
        pack = create_pack_folder(
            self.temp / "CustomCharts",
            "Empty Meta",
            "empty-meta",
            "Empty Meta",
            artist="   ",
            description="",
        )
        config = json.loads(
            pack.folder.joinpath("pack.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("artist", config)
        self.assertNotIn("description", config)

    def test_default_pack_conflict_fails_closed(self) -> None:
        invalid = self.temp / "CustomCharts" / "User Charts"
        invalid.mkdir(parents=True)
        with self.assertRaisesRegex(
            song_package_module.ConversionError,
            "pack.json",
        ):
            ensure_default_pack(self.temp / "CustomCharts")

    def test_create_pack_rejects_duplicate_id(self) -> None:
        create_pack_folder(
            self.temp / "CustomCharts",
            "First Pack",
            "same-id",
            "First Pack",
        )
        with self.assertRaisesRegex(
            song_package_module.ConversionError,
            "曲包 id 已被使用",
        ):
            create_pack_folder(
                self.temp / "CustomCharts",
                "Second Pack",
                "SAME-ID",
                "Second Pack",
            )

    def test_external_profile_writes_declared_filename(self) -> None:
        self.prepare_default_pack()
        package_spec = self.spec()
        package_spec.profile = "in_falsus_chart_loader"
        result = export_song_package(package_spec)
        self.assertEqual(result.config.name, "if")
        config = json.loads(result.config.read_text(encoding="utf-8"))
        self.assertEqual(config["name"], "Test Song")
        self.assertEqual(config["chart"][0], "test_song0.spc")

    def test_profile_config_filename_cannot_escape_song_folder(self) -> None:
        self.prepare_default_pack()
        profiles = self.temp / "profiles.json"
        profiles.write_text(
            json.dumps({
                "profiles": {
                    "escape": {
                        "configFile": "../outside.json",
                        "template": {"title": "${song_title}"},
                    }
                }
            }),
            encoding="utf-8",
        )
        package_spec = self.spec()
        package_spec.profile = "escape"
        package_spec.profile_config = profiles
        with self.assertRaisesRegex(
            song_package_module.ConversionError,
            "配置文件名不能包含目录",
        ):
            export_song_package(package_spec)


if __name__ == "__main__":
    unittest.main()
