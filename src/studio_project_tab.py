from __future__ import annotations

import os
import queue
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui import SLOT_LABEL, SongPackageTab
from pack_library import (
    DEFAULT_PACK_FOLDER,
    DEFAULT_PACK_ID,
    DEFAULT_PACK_STYLE,
    DEFAULT_PACK_TITLE,
    PackDirectory,
)
from song_package import (
    ConversionError,
    export_song_folder,
    export_song_package,
)
from spc_converter import executable_dir, read_source
from studio_project import (
    StudioProject,
    find_chart_name_conflicts,
    infer_internal_code,
    load_studio_source,
    validate_internal_code,
)


class StudioProjectTab(SongPackageTab):
    """Import a complete InFalsus Studio project without modifying it."""

    project_kind = "Studio"
    path_label = "工程目录、infalsus.studio.json 或 ZIP"
    manifest_button_text = "选择 JSON/ZIP"
    manifest_dialog_title = "选择 infalsus.studio.json 或工程 ZIP"
    manifest_filetypes = (
        ("InFalsus Studio 工程", "*.zip infalsus.studio.json"),
        ("ZIP", "*.zip"),
        ("JSON", "*.json"),
        ("所有文件", "*.*"),
    )
    standalone_folder_name = "Studio Songs"

    def __init__(self, master: tk.Misc):
        self.project_path = tk.StringVar()
        self.project_status = tk.StringVar(
            value=f"尚未选择 {self.project_kind} 工程"
        )
        self.studio_project: StudioProject | None = None
        self._source_session = None
        self._action_buttons: list[ttk.Button] = []
        super().__init__(master)
        self.bind("<Destroy>", self._on_destroy, add="+")

    def _reload_profiles(self, initial: bool = False) -> None:
        # Studio import always writes the current schemaVersion 1 config.
        return

    def _profile_changed(self) -> None:
        return

    def _build(self) -> None:
        source = ttk.LabelFrame(
            self,
            text=f"{self.project_kind} 工程",
            padding=10,
        )
        source.pack(fill="x")
        ttk.Label(source, text=self.path_label).grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(source, textvariable=self.project_path).grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            source,
            text="选择文件夹",
            command=self.choose_project_folder,
        ).grid(row=0, column=2, padx=(6, 0), pady=4)
        ttk.Button(
            source,
            text=self.manifest_button_text,
            command=self.choose_project_manifest,
        ).grid(row=0, column=3, padx=(6, 0), pady=4)
        ttk.Button(
            source,
            text="重新读取",
            command=self.load_project,
        ).grid(row=0, column=4, padx=(6, 0), pady=4)
        ttk.Label(
            source,
            textvariable=self.project_status,
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))
        source.columnconfigure(1, weight=1)

        info = ttk.LabelFrame(self, text="歌曲信息（可修改）", padding=10)
        info.pack(fill="x", pady=(10, 0))
        fields = [
            ("song_title", "歌曲显示名", 0, 0),
            ("artist", "艺术家", 0, 2),
            ("internal_code", "内部代号（必填）", 1, 0),
            ("chart_designer", "谱师", 1, 2),
            ("jacket_designer", "曲绘作者", 2, 0),
            ("preview_start", "预览开始（秒）", 2, 2),
            ("preview_end", "预览结束（秒）", 3, 0),
        ]
        for key, label, row, col in fields:
            ttk.Label(info, text=label).grid(
                row=row, column=col, sticky="w", padx=(0, 6), pady=4
            )
            ttk.Entry(info, textvariable=self._var(key)).grid(
                row=row, column=col + 1, sticky="ew", pady=4
            )
        self.vars["preview_start"].set("0")
        self.vars["preview_end"].set("0")
        self.code_hint = ttk.Label(
            info,
            text="",
            foreground="#B00020",
        )
        self.code_hint.grid(
            row=4,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(2, 0),
        )
        self.vars["internal_code"].trace_add(
            "write",
            lambda *_args: self._update_code_hint(),
        )
        info.columnconfigure(1, weight=1)
        info.columnconfigure(3, weight=1)

        resources = ttk.LabelFrame(self, text="资源（可修改）", padding=10)
        resources.pack(fill="x", pady=(10, 0))
        for row, (key, label, kind) in enumerate(
            (
                ("audio", "歌曲音频", "audio"),
                ("jacket", "歌曲封面", "image"),
                ("song_background", "歌曲默认背景", "background"),
            )
        ):
            ttk.Label(resources, text=label).grid(
                row=row, column=0, sticky="w", pady=4
            )
            ttk.Entry(resources, textvariable=self._var(key)).grid(
                row=row, column=1, sticky="ew", padx=(6, 6), pady=4
            )
            ttk.Button(
                resources,
                text="浏览…",
                command=lambda k=key, value=kind: self.browse(k, value),
            ).grid(row=row, column=2, pady=4)
        resources.columnconfigure(1, weight=1)

        destination = ttk.LabelFrame(self, text="输出位置", padding=10)
        destination.pack(fill="x", pady=(10, 0))
        ttk.Label(destination, text="Steam 正式版").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=4
        )
        self.game_combo = ttk.Combobox(
            destination,
            textvariable=self.game_label,
            state="readonly",
        )
        self.game_combo.grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=4
        )
        self.game_combo.bind("<<ComboboxSelected>>", self._game_install_changed)

        ttk.Label(destination, text="游戏 CustomCharts").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(
            destination,
            textvariable=self._var("output_root"),
        ).grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(
            destination,
            text="浏览…",
            command=lambda: self.browse("output_root", "directory"),
        ).grid(row=1, column=3, padx=(6, 0), pady=4)

        ttk.Label(destination, text="目标曲包").grid(
            row=2, column=0, sticky="w", padx=(0, 6), pady=4
        )
        self.pack_combo = ttk.Combobox(
            destination,
            textvariable=self.pack_label,
            state="readonly",
        )
        self.pack_combo.grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=4
        )
        ttk.Button(
            destination,
            text="刷新曲包",
            command=self.refresh_pack_list,
        ).grid(row=2, column=3, padx=(6, 0), pady=4)

        ttk.Label(destination, text="单独导出父目录").grid(
            row=3, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(
            destination,
            textvariable=self._var("standalone_root"),
        ).grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(
            destination,
            text="浏览…",
            command=lambda: self.browse("standalone_root", "directory"),
        ).grid(row=3, column=3, padx=(6, 0), pady=4)
        destination.columnconfigure(1, weight=1)
        self.vars["output_root"].set(
            str(executable_dir() / "output" / "CustomCharts")
        )
        self.vars["standalone_root"].set(
            str(executable_dir() / "output" / self.standalone_folder_name)
        )

        difficulties = ttk.LabelFrame(
            self,
            text=f"{self.project_kind} 难度（非空难度默认全部导入）",
            padding=10,
        )
        difficulties.pack(fill="x", pady=(10, 0))
        headers = (
            "导入",
            "难度",
            "工作谱面",
            "背景覆盖",
            "",
            "Rating",
            "难度角标",
            "状态",
        )
        for col, text in enumerate(headers):
            ttk.Label(difficulties, text=text).grid(
                row=0, column=col, sticky="w", padx=(0, 6)
            )
        for slot in range(4):
            row_vars = {
                "include": tk.BooleanVar(value=False),
                "name": tk.StringVar(value=SLOT_LABEL[slot]),
                "chart": tk.StringVar(),
                "background": tk.StringVar(),
                "rating": tk.StringVar(value="0"),
                "indicator": tk.StringVar(value="?"),
                "status": tk.StringVar(value="尚未加载"),
            }
            self.diffs.append(row_vars)
            row = slot + 1
            ttk.Checkbutton(
                difficulties,
                variable=row_vars["include"],
            ).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Label(
                difficulties,
                textvariable=row_vars["name"],
            ).grid(row=row, column=1, sticky="w", padx=(0, 6), pady=3)
            ttk.Entry(
                difficulties,
                textvariable=row_vars["chart"],
            ).grid(row=row, column=2, sticky="ew", padx=(0, 6), pady=3)
            ttk.Entry(
                difficulties,
                textvariable=row_vars["background"],
            ).grid(row=row, column=3, sticky="ew", padx=(0, 6), pady=3)
            ttk.Button(
                difficulties,
                text="选择",
                command=lambda s=slot: self.choose_difficulty_background(s),
            ).grid(row=row, column=4, sticky="w", padx=(0, 6), pady=3)
            ttk.Entry(
                difficulties,
                textvariable=row_vars["rating"],
                width=7,
            ).grid(row=row, column=5, sticky="w", padx=(0, 6), pady=3)
            ttk.Entry(
                difficulties,
                textvariable=row_vars["indicator"],
                width=14,
            ).grid(row=row, column=6, sticky="w", padx=(0, 6), pady=3)
            ttk.Label(
                difficulties,
                textvariable=row_vars["status"],
            ).grid(row=row, column=7, sticky="w", pady=3)
        difficulties.columnconfigure(2, weight=1)
        difficulties.columnconfigure(3, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(12, 8))
        import_button = ttk.Button(
            actions,
            text="导入游戏",
            command=lambda: self.start_export("game"),
        )
        import_button.pack(side="left")
        folder_button = ttk.Button(
            actions,
            text="导出歌曲文件夹",
            command=lambda: self.start_export("folder"),
        )
        folder_button.pack(side="left", padx=8)
        self._action_buttons = [import_button, folder_button]
        self.open_after = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            actions,
            text="完成后打开目录",
            variable=self.open_after,
        ).pack(side="left", padx=12)
        ttk.Button(
            actions,
            text="打开单独导出目录",
            command=self.open_standalone_output,
        ).pack(side="right")

        self.log_text = tk.Text(self, height=10, wrap="word")
        self.log_text.pack(fill="both", expand=True, pady=(2, 0))

    def choose_project_folder(self) -> None:
        selected = filedialog.askdirectory(
            title=f"选择 {self.project_kind} 工程目录"
        )
        if selected:
            self.project_path.set(selected)
            self.load_project()

    def choose_project_manifest(self) -> None:
        selected = filedialog.askopenfilename(
            title=self.manifest_dialog_title,
            filetypes=self.manifest_filetypes,
        )
        if selected:
            self.project_path.set(selected)
            self.load_project()

    def choose_difficulty_background(self, slot: int) -> None:
        selected = filedialog.askopenfilename(
            title=f"选择 {SLOT_LABEL[slot]} 背景",
            filetypes=(
                ("背景图片/视频", "*.png *.jpg *.jpeg *.webp *.mp4"),
                ("所有文件", "*.*"),
            ),
        )
        if selected:
            self.diffs[slot]["background"].set(selected)

    def _load_project_from_path(self, path: Path) -> StudioProject:
        self._replace_source_session(None)
        project, session = load_studio_source(
            path,
            audio_chooser=self._choose_discovered_audio,
        )
        self._replace_source_session(session)
        return project

    def _choose_discovered_audio(
        self,
        configured: str,
        label: str,
        candidates: tuple[Path, ...],
    ) -> Path | None:
        if len(candidates) == 1:
            selected = candidates[0]
            messagebox.showinfo(
                "已找到音频",
                f"{label}配置路径不存在：\n{configured}\n\n"
                f"已在当前文件夹中找到并自动使用：\n{selected}",
                parent=self,
            )
            return selected

        dialog = tk.Toplevel(self)
        dialog.title(f"选择{label}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(True, True)

        ttk.Label(
            dialog,
            text=(
                f"{label}配置路径不存在：{configured}\n"
                "当前文件夹中找到多个有效音频，请选择："
            ),
            justify="left",
        ).pack(fill="x", padx=12, pady=(12, 6))
        listbox = tk.Listbox(
            dialog,
            width=100,
            height=min(16, max(5, len(candidates))),
            exportselection=False,
        )
        listbox.pack(fill="both", expand=True, padx=12, pady=6)
        for candidate in candidates:
            listbox.insert("end", str(candidate))
        listbox.selection_set(0)
        listbox.activate(0)

        result: list[Path | None] = [None]

        def accept() -> None:
            selection = listbox.curselection()
            if selection:
                result[0] = candidates[selection[0]]
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        controls = ttk.Frame(dialog)
        controls.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(
            controls,
            text="使用所选音频",
            command=accept,
        ).pack(side="left")
        ttk.Button(
            controls,
            text="取消",
            command=cancel,
        ).pack(side="right")
        listbox.bind("<Double-Button-1>", lambda _event: accept())
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        self.wait_window(dialog)
        return result[0]

    def _replace_source_session(self, session) -> None:
        old = self._source_session
        self._source_session = session
        if old is not None and old is not session:
            try:
                old.close()
            except Exception:
                pass

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            self._replace_source_session(None)

    def load_project(self) -> None:
        text = self.project_path.get().strip()
        if not text:
            messagebox.showerror(
                "未选择工程",
                f"请先选择 {self.project_kind} 工程",
                parent=self,
            )
            return
        try:
            project = self._load_project_from_path(Path(text))
            self._apply_project(project)
        except Exception as exc:
            self.studio_project = None
            self._replace_source_session(None)
            self.project_status.set(f"加载失败：{exc}")
            self.log(f"[失败] {self.project_kind} 工程加载失败：{exc}")
            messagebox.showerror(
                f"{self.project_kind} 工程加载失败",
                str(exc),
                parent=self,
            )
            return
        self.log(f"[完成] 已读取 {self.project_kind} 工程：{project.manifest}")

    def _apply_project(self, project: StudioProject) -> None:
        self.studio_project = project
        self.project_status.set(
            f"已读取：{project.title}，"
            f"{len(project.non_empty_difficulties)} 个非空难度"
        )
        self.vars["song_title"].set(project.title)
        self.vars["artist"].set(project.artist)
        self.vars["internal_code"].set(
            project.source_code_hint or infer_internal_code(project.title)
        )
        self.vars["audio"].set(str(project.audio))
        self.vars["jacket"].set(str(project.jacket) if project.jacket else "")
        self.vars["song_background"].set(
            str(project.background) if project.background else ""
        )
        self.vars["preview_start"].set(
            f"{project.preview_start_seconds:g}"
        )
        self.vars["preview_end"].set(
            f"{project.preview_end_seconds:g}"
        )
        chart_designer, jacket_designer = project.author_defaults()
        self.vars["chart_designer"].set(chart_designer)
        self.vars["jacket_designer"].set(jacket_designer)
        for item in project.difficulties:
            row = self.diffs[item.index]
            row["include"].set(not item.is_empty)
            row["name"].set(f"{SLOT_LABEL[item.index]} · {item.name}")
            row["chart"].set(str(item.chart) if item.chart else "")
            row["background"].set(
                str(item.background) if item.background else ""
            )
            row["rating"].set(str(item.rating))
            row["indicator"].set(item.level_section_indicator)
            row["status"].set(
                "空谱，将忽略"
                if item.is_empty
                else f"{item.note_count} note / {item.event_count} event"
            )
            if item.is_empty:
                reason = (
                    "谱面文件不存在"
                    if item.chart is None or not item.chart.is_file()
                    else "note_count=0"
                )
                self.log(
                    f"[警告] {SLOT_LABEL[item.index]} {reason}，"
                    f"按空难度处理：{item.chart or '(未提供路径)'}"
                )
        if not self.vars["internal_code"].get().strip():
            self.log(
                "[提示] 歌曲名包含非 ASCII 字母/数字字符，"
                "请手动填写内部代号。"
            )
        self._update_code_hint()

    def _update_code_hint(self) -> None:
        text = self.vars["internal_code"].get().strip()
        if not text:
            self.code_hint.configure(text="必须填写内部代号")
            return
        try:
            validate_internal_code(text)
        except ConversionError as exc:
            self.code_hint.configure(text=str(exc))
            return
        self.code_hint.configure(text="")

    def _edited_spec(self, output_root: Path, pack_folder_name: str):
        project = self.studio_project
        if project is None:
            raise ConversionError(f"请先加载 {self.project_kind} 工程")
        code = validate_internal_code(self.vars["internal_code"].get())
        included = [
            slot
            for slot, row in enumerate(self.diffs)
            if row["include"].get()
        ]
        spec = project.to_song_spec(
            code,
            output_root,
            pack_folder_name=pack_folder_name,
            included_slots=included,
        )
        title = self.vars["song_title"].get().strip()
        audio_text = self.vars["audio"].get().strip()
        if not title:
            raise ConversionError("歌曲显示名不能为空")
        if not audio_text:
            raise ConversionError("歌曲音频不能为空")
        try:
            preview_start = float(
                self.vars["preview_start"].get().strip() or "0"
            )
            preview_end_text = self.vars["preview_end"].get().strip()
            preview_end = float(preview_end_text) if preview_end_text else None
        except ValueError as exc:
            raise ConversionError("预览开始/结束必须是数字") from exc
        jacket_text = self.vars["jacket"].get().strip()
        background_text = self.vars["song_background"].get().strip()
        spec.song_title = title
        spec.artist = self.vars["artist"].get().strip()
        spec.chart_designer = self.vars["chart_designer"].get().strip()
        spec.jacket_designer = self.vars["jacket_designer"].get().strip()
        spec.audio = Path(audio_text)
        spec.jacket_large = Path(jacket_text) if jacket_text else None
        spec.jacket_small = spec.jacket_large
        spec.background = Path(background_text) if background_text else None
        spec.preview_start_seconds = preview_start
        spec.preview_end_seconds = (
            preview_end
            if preview_end is not None and preview_end > preview_start
            else None
        )
        by_slot = {item.slot: item for item in spec.difficulties}
        for slot in included:
            row = self.diffs[slot]
            difficulty = by_slot.get(slot)
            if difficulty is None:
                raise ConversionError(f"{SLOT_LABEL[slot]} 未包含谱面项")
            chart_text = row["chart"].get().strip()
            try:
                rating = int(row["rating"].get().strip() or "0")
            except ValueError as exc:
                raise ConversionError(
                    f"{SLOT_LABEL[slot]} 的 Rating 必须是整数"
                ) from exc
            if rating < 0:
                raise ConversionError(
                    f"{SLOT_LABEL[slot]} 的 Rating 不能为负数"
                )
            difficulty.chart = Path(chart_text) if chart_text else None
            difficulty.rating = rating
            difficulty.level_section_indicator = (
                row["indicator"].get().strip() or "?"
            )
            difficulty_background = row["background"].get().strip()
            difficulty.background = (
                Path(difficulty_background)
                if difficulty_background
                else None
            )
        return spec

    def start_export(self, mode: str) -> None:
        confirmation_pack = None
        overwrite_conflicts: list[Path] = []
        try:
            if self.studio_project is None:
                raise ConversionError(f"请先加载 {self.project_kind} 工程")
            if mode == "game":
                pack = self._selected_pack()
                if pack is None:
                    self.refresh_pack_list(show_errors=False)
                    pack = self._selected_pack()
                if pack is None:
                    root = Path(
                        self.vars["output_root"].get().strip()
                        or executable_dir() / "output" / "CustomCharts"
                    ).resolve()
                    pack = PackDirectory(
                        root / DEFAULT_PACK_FOLDER,
                        DEFAULT_PACK_ID,
                        DEFAULT_PACK_TITLE,
                        "",
                        "",
                        DEFAULT_PACK_STYLE,
                        0,
                    )
                confirmation_pack = pack
                output_root = pack.folder.parent
                pack_name = pack.folder.name
                target_folder = pack.folder / self.vars["internal_code"].get().strip()
                exporter = export_song_package
            elif mode == "folder":
                output_root = Path(
                    self.vars["standalone_root"].get().strip()
                    or executable_dir() / "output"
                    / self.standalone_folder_name
                )
                pack_name = "User Charts"
                target_folder = (
                    output_root / self.vars["internal_code"].get().strip()
                )
                exporter = export_song_folder
            else:
                raise ConversionError("未知导出模式")
            spec = self._edited_spec(output_root, pack_name)
            conflict_root = Path(
                self.vars["output_root"].get().strip() or output_root
            )
            conflicts = find_chart_name_conflicts(
                conflict_root,
                spec.chart_id,
                self._importable_slots(spec),
                ignore_folder=target_folder,
            )
            if conflicts:
                details = "\n".join(str(path) for path in conflicts[:12])
                raise ConversionError(
                    "内部代号生成的文件名与其他自制谱冲突：\n" + details
                )
            if mode == "game":
                overwrite_conflicts = [
                    target_folder / f"{spec.chart_id}{slot}.spc"
                    for slot in self._importable_slots(spec)
                    if (target_folder / f"{spec.chart_id}{slot}.spc").is_file()
                ]
        except Exception as exc:
            self.log(f"[失败] {exc}")
            messagebox.showerror("无法开始导出", str(exc), parent=self)
            return

        if mode == "game":
            assert confirmation_pack is not None
            if not self._confirm_game_import(
                confirmation_pack,
                spec,
                overwrite_conflicts,
            ):
                self.log(
                    f"[取消] 已取消导入，内部代号：{spec.chart_id}"
                )
                return
            actual_pack = self._pack_for_export()
            if actual_pack.folder != confirmation_pack.folder:
                messagebox.showerror(
                    "曲包状态已变化",
                    "确认后目标曲包发生变化，请重新执行导入。",
                    parent=self,
                )
                return

        for button in self._action_buttons:
            button.configure(state="disabled")
        self.log(
            "[信息] 正在导入到游戏曲包…"
            if mode == "game"
            else "[信息] 正在导出独立歌曲文件夹…"
        )

        def worker() -> None:
            try:
                self.result_queue.put((True, exporter(spec)))
            except Exception as exc:
                self.result_queue.put((False, exc))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll_export)

    def _poll_export(self) -> None:
        try:
            ok, value = self.result_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_export)
            return
        for button in self._action_buttons:
            button.configure(state="normal")
        if not ok:
            self.log(f"[失败] {value}")
            messagebox.showerror("导出失败", str(value), parent=self)
            return
        result = value
        for warning in getattr(result, "warnings", []):
            self.log(f"[警告] {warning}")
        self.log(f"[完成] 歌曲文件夹：{result.folder}")
        self.log(f"        config：{result.config}")
        self.log(f"        谱面：{[path.name for path in result.charts]}")
        if self.open_after.get():
            os.startfile(result.folder)  # type: ignore[attr-defined]

    def _confirm_game_import(
        self,
        pack,
        spec,
        overwrite_conflicts: list[Path] | None = None,
    ) -> bool:
        conflicts = overwrite_conflicts or []
        conflict_text = ""
        if conflicts:
            details = "\n".join(path.name for path in conflicts)
            conflict_text = (
                "\n\n以下同难度谱面已存在，确认后将覆盖：\n"
                + details
            )
        return messagebox.askokcancel(
            "确认导入并覆盖冲突",
            f"当前内部代号：{spec.chart_id}\n\n"
            f"目标曲包：{pack.title}\n"
            f"歌曲目录：{pack.folder / spec.folder_name}\n\n"
            "确定要导入游戏吗？"
            + conflict_text,
            parent=self,
        )

    @staticmethod
    def _importable_slots(spec) -> list[int]:
        slots: list[int] = []
        for item in spec.difficulties:
            if item.chart is None or not item.chart.is_file():
                continue
            try:
                source = read_source(item.chart)
                if source.standard.get("notes", []):
                    slots.append(item.slot)
            except Exception:
                continue
        return sorted(slots)

    def open_standalone_output(self) -> None:
        path = Path(self.vars["standalone_root"].get().strip())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]
