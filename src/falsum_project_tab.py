from __future__ import annotations

from pathlib import Path

from falsum_project import load_falsum_project
from studio_project import StudioProject
from studio_project_tab import StudioProjectTab


class FalsumProjectTab(StudioProjectTab):
    project_kind = "Falsum"
    path_label = ".falsum 文件或仅包含一个 .falsum 的文件夹"
    manifest_button_text = "选择 Falsum"
    manifest_dialog_title = "选择 .falsum 文件"
    manifest_filetypes = (
        ("Falsum 歌曲包", "*.falsum"),
        ("所有文件", "*.*"),
    )
    standalone_folder_name = "Falsum Songs"

    def _load_project_from_path(self, path: Path) -> StudioProject:
        session = load_falsum_project(
            path,
            audio_chooser=self._choose_discovered_audio,
        )
        self._replace_source_session(session)
        return session.project

    def _apply_project(self, project: StudioProject) -> None:
        super()._apply_project(project)
        if project.background is None:
            self.log(
                "[提示] Falsum 背景字段没有对应的包内文件，"
                "已留空，请手动选择歌曲或难度背景。"
            )
