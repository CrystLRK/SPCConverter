from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pack_library  # noqa: E402


class PackLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="ifce-pack-library-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def write_pack(
        self,
        folder: str,
        pack_id: str,
        title: str,
        *,
        schema: int = 1,
    ) -> Path:
        path = self.temp / folder
        path.mkdir(parents=True)
        (path / "pack.json").write_text(
            json.dumps({
                "schemaVersion": schema,
                "id": pack_id,
                "name": title,
            }),
            encoding="utf-8",
        )
        return path

    def test_scan_reports_valid_invalid_and_duplicate_packs(self) -> None:
        self.write_pack("Valid", "valid", "Valid Pack")
        self.write_pack("BadSchema", "bad-schema", "Bad Schema", schema=2)
        self.write_pack("DuplicateA", "duplicate", "Duplicate A")
        self.write_pack("DuplicateB", "DUPLICATE", "Duplicate B")

        scan = pack_library.scan_pack_directories(self.temp)
        self.assertEqual([pack.pack_id for pack in scan.packs], ["valid"])
        reasons = {item.folder.name: item.reason for item in scan.invalid}
        self.assertIn("schemaVersion", reasons["BadSchema"])
        self.assertIn("id 重复", reasons["DuplicateA"])
        self.assertIn("id 重复", reasons["DuplicateB"])

    def test_discover_game_installs_uses_formal_app_manifest(self) -> None:
        library = self.temp / "SteamLibrary"
        common = library / "steamapps" / "common"
        game = common / "In Falsus"
        game.mkdir(parents=True)
        (game / "infalsus.exe").write_bytes(b"exe")
        (game / "GameAssembly.dll").write_bytes(b"dll")
        manifest = library / "steamapps" / f"appmanifest_{pack_library.GAME_APP_ID}.acf"
        manifest.write_text(
            '"AppState"\n{\n  "installdir" "In Falsus"\n}\n',
            encoding="utf-8",
        )
        steam = self.temp / "Steam"
        steam.mkdir()

        with mock.patch.object(pack_library, "_steam_roots", return_value=[steam]), \
                mock.patch.object(
                    pack_library,
                    "_library_roots",
                    return_value=[library],
                ):
            installs = pack_library.discover_game_installs()

        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0].install_dir, game.resolve())
        self.assertEqual(installs[0].charts_root, game.resolve() / "Charts")

    def test_preferred_game_root_is_remembered(self) -> None:
        settings = self.temp / "settings.json"
        game = self.temp / "In Falsus"
        game.mkdir()
        pack_library.save_preferred_game_root(game, settings)
        self.assertEqual(
            pack_library.load_preferred_game_root(settings),
            game.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
