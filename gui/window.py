from __future__ import annotations

import concurrent.futures
import difflib
import json
import math
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from config_store import (
    load_app_config,
    load_last_start_dir,
    save_app_config,
    save_last_start_dir,
)
from constants import (
    BLENDER_PRESET_CHOICES,
    GUI_EXT_CHOICES,
    GUI_IMAGE_EXT,
    RENDERER_CHOICES,
)
from renderers import detect_blender_executable, render_stl
from scanner import (
    ScanSummary,
    collect_directories,
    iter_render_sources,
    iter_render_sources_in_directory,
    list_display_files,
    needs_render,
    scan_summary,
    target_image_path,
)
from gui.models import RenderProgress
from gui.utils import format_file_size
from gui.i18n import LANGUAGE_CHOICES, detect_default_language, month_label, normalize_language, tr


def launch_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class STLPreviewApp:
        def __init__(self, root: tk.Tk, source: Path):
            self.root = root
            self.root.title(tr("de", "app.title"))
            self.root.geometry("1300x800")
            self.root.minsize(980, 640)
            self._setup_styles()

            self.source = source.resolve()
            self.config = load_app_config()
            self.gui_config = self.config.get("gui", {}) if isinstance(self.config.get("gui"), dict) else {}
            self.render_width = self._config_int("render_width", 500)
            self.render_height = self._config_int("render_height", 300)
            self.render_threads = self._config_int("render_threads", 4)
            self.render_margin = self._config_float("render_margin", 0.18, 0.0, 1.0)
            self.image_ext = self._config_ext("image_ext", GUI_IMAGE_EXT)
            self.renderer = self._config_renderer("renderer", "blender")
            self.blender_preset = self._config_blender_preset("blender_preset", "kontrast")
            self.language = self._config_language("language", detect_default_language())
            self.blender_path = self._resolve_optional_path(self.gui_config.get("blender_path"))
            self.bambu_studio_path = self._resolve_optional_path(self.gui_config.get("bambu_studio_path"))
            self.index_dir = self._resolve_index_dir(
                self.gui_config.get("index_dir"), self.source / "Index"
            )
            self.tree_paths: dict[str, object] = {}
            self.thumbnail_cache: list[tk.PhotoImage] = []
            self.thumbnail_images: dict[Path, tk.PhotoImage] = {}
            self.thumb_items: dict[Path, dict[str, object]] = {}
            self.directory_snapshot: list[Path] = []
            self.path_to_tree_id: dict[Path, str] = {}
            self.time_to_tree_id: dict[str, str] = {}
            self.year_to_tree_id: dict[str, str] = {}
            self.model_records: list[dict] = []
            self.summary = ScanSummary()
            self.render_progress: RenderProgress | None = None
            self.render_running = False
            self.render_cancel_event = threading.Event()
            self.current_render_scope: str | None = None
            self.current_render_overwrite = False
            self.current_render_dir: Path | None = None
            self.render_inflight_paths: set[Path] = set()
            self.thumb_job_token = 0
            self.thumb_relayout_after_id: str | None = None
            self.current_thumb_files: list[Path] = []
            self.thumb_columns = 0
            self.ui_queue: queue.Queue[tuple] = queue.Queue()
            self.selected_directory = self.source
            self.selected_timeline_key: str | None = None
            self.selected_timeline_year: str | None = None
            self.nav_mode = tk.StringVar(value="tree")
            self.current_model_files: list[Path] = []
            self.table_sort_column = "name"
            self.table_sort_desc = False
            self.hover_preview_win = None
            self.hover_preview_label = None
            self.hover_preview_image = None
            self.hover_preview_model: Path | None = None
            self._initial_sash_set = False
            self.search_var = tk.StringVar()
            self.search_after_id: str | None = None
            self.search_match_files: set[Path] = set()
            self.search_match_dirs: set[Path] = set()
            self.file_item_paths: dict[str, Path] = {}
            self.context_model_path: Path | None = None
            self.nav_toggle_tooltip_win = None
            self.nav_toggle_tooltip_label = None

            self._build_menu()
            self._build_layout()
            self._set_path_text()
            self.root.title(self._t("app.title"))
            self._set_status(self._t("state.ready"))
            self._set_activity(self._t("state.initial_scan"))
            self._append_log(self._t("log.gui_started"))
            self._set_progress_indeterminate()
            self.root.after(100, self._process_ui_queue)
            self._start_initial_scan()

            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        def _setup_styles(self):
            import tkinter as tk
            from tkinter import ttk

            self.style = ttk.Style(self.root)
            try:
                self.style.theme_use("clam")
            except tk.TclError:
                pass
            self.root.configure(bg="#eef2f7")
            self.style.configure("Top.TFrame", background="#eef2f7")
            self.style.configure("Card.TFrame", background="#ffffff", relief="flat")
            self.style.configure("Title.TLabel", font=("Segoe UI", 11, "bold"), background="#eef2f7")
            self.style.configure("Panel.TFrame", background="#e6ebf2")
            self.style.configure("SectionTitle.TLabel", font=("Segoe UI", 11, "bold"), background="#e6ebf2")
            self.style.configure("Path.TLabel", font=("Segoe UI", 10), background="#eef2f7", foreground="#34495e")
            self.style.configure("Status.TLabel", font=("Segoe UI", 10), background="#eef2f7")
            self.style.configure("StatName.TLabel", font=("Segoe UI", 9), background="#ffffff", foreground="#576574")
            self.style.configure("StatValue.TLabel", font=("Segoe UI", 14, "bold"), background="#ffffff", foreground="#1f2d3d")
            self.style.configure("Toolbar.TButton", padding=(10, 4))
            self.style.configure("Search.TEntry", padding=(6, 4))
            self.style.configure("SearchGroup.TFrame", background="#eef2f7", relief="solid", borderwidth=1)

        def _build_menu(self):
            menubar = tk.Menu(self.root)
            file_menu = tk.Menu(menubar, tearoff=0)
            file_menu.add_command(label=self._t("menu.file.change_start_dir"), command=self.change_start_dir)
            file_menu.add_command(label=self._t("menu.file.settings"), command=self.open_config_dialog)
            file_menu.add_command(label=self._t("menu.file.rescan"), command=lambda: self._start_initial_scan(force=True))
            file_menu.add_command(label=self._t("menu.file.delete_index"), command=self.delete_index_directory)
            file_menu.add_separator()
            file_menu.add_command(label=self._t("menu.file.exit"), command=self.on_close)
            menubar.add_cascade(label=self._t("menu.file"), menu=file_menu)

            render_menu = tk.Menu(menubar, tearoff=0)
            render_menu.add_command(
                label=self._t("menu.render.start_all"),
                command=lambda: self.start_background_render("all", overwrite=False),
            )
            render_menu.add_command(
                label=self._t("menu.render.start_current"),
                command=lambda: self.start_background_render("current", overwrite=True),
            )
            render_menu.add_separator()
            render_menu.add_command(label=self._t("menu.render.abort"), command=self.abort_background_render)
            menubar.add_cascade(label=self._t("menu.render"), menu=render_menu)

            about_menu = tk.Menu(menubar, tearoff=0)
            about_menu.add_command(label=self._t("about.menu"), command=self.show_about_dialog)
            menubar.add_cascade(label=self._t("about.menu"), menu=about_menu)

            self.root.config(menu=menubar)

        def show_about_dialog(self):
            messagebox.showinfo(
                self._t("about.title"),
                self._t("about.text"),
            )

        def _build_layout(self):
            header = ttk.Frame(self.root, padding=10, style="Top.TFrame")
            header.pack(fill="x")

            self.path_label = ttk.Label(header, text="", anchor="w", style="Path.TLabel")
            self.path_label.pack(fill="x")

            toolbar = ttk.Frame(header, style="Top.TFrame")
            toolbar.pack(fill="x", pady=(8, 4))
            self.btn_rescan = ttk.Button(
                toolbar,
                text=self._t("toolbar.rescan"),
                style="Toolbar.TButton",
                command=lambda: self._start_initial_scan(force=True),
            )
            self.btn_rescan.pack(side="left")
            self.btn_render_current = ttk.Button(
                toolbar,
                text=self._t("toolbar.render_current"),
                style="Toolbar.TButton",
                command=lambda: self.start_background_render("current", overwrite=True),
            )
            self.btn_render_current.pack(side="left", padx=(6, 0))
            self.btn_render_all = ttk.Button(
                toolbar,
                text=self._t("toolbar.render_all"),
                style="Toolbar.TButton",
                command=lambda: self.start_background_render("all", overwrite=False),
            )
            self.btn_render_all.pack(side="left", padx=(6, 0))
            self.btn_abort = ttk.Button(toolbar, text=self._t("toolbar.abort"), style="Toolbar.TButton", command=self.abort_background_render)
            self.btn_abort.pack(
                side="left", padx=(6, 0)
            )
            self.btn_delete_index = ttk.Button(toolbar, text=self._t("toolbar.delete_index"), style="Toolbar.TButton", command=self.delete_index_directory)
            self.btn_delete_index.pack(
                side="left", padx=(6, 0)
            )
            ttk.Frame(toolbar, style="Top.TFrame").pack(side="left", fill="x", expand=True)
            search_group = ttk.Frame(toolbar, style="SearchGroup.TFrame", padding=(8, 4))
            search_group.pack(side="right", padx=(20, 0))
            self.search_label = ttk.Label(search_group, text=self._t("toolbar.search"), style="Status.TLabel")
            self.search_label.pack(side="left", padx=(0, 6))
            search_entry = ttk.Entry(search_group, textvariable=self.search_var, width=30, style="Search.TEntry")
            search_entry.pack(side="left")
            search_entry.bind("<KeyRelease>", self._on_search_keyrelease)
            self.btn_clear_search = ttk.Button(
                search_group,
                text=self._t("toolbar.search_clear"),
                style="Toolbar.TButton",
                command=self._clear_search,
            )
            self.btn_clear_search.pack(side="left", padx=(6, 0))

            cards = ttk.Frame(header, style="Top.TFrame")
            cards.pack(fill="x", pady=(2, 6))

            def make_card(parent, title: str):
                frame = ttk.Frame(parent, style="Card.TFrame", padding=(12, 8))
                frame.pack(side="left", fill="x", expand=True, padx=(0, 8))
                title_label = ttk.Label(frame, text=title, style="StatName.TLabel")
                title_label.pack(anchor="w")
                value = ttk.Label(frame, text="0", style="StatValue.TLabel")
                value.pack(anchor="w", pady=(2, 0))
                return title_label, value

            self.stat_stl_title, self.stat_stl = make_card(cards, self._t("stats.stl"))
            self.stat_blend_title, self.stat_blend = make_card(cards, self._t("stats.blend_only"))
            self.stat_total_title, self.stat_total = make_card(cards, self._t("stats.total"))
            self.stat_existing_title, self.stat_existing = make_card(cards, self._t("stats.images"))
            self.stat_todo_title, self.stat_todo = make_card(cards, self._t("stats.todo"))

            info_row = ttk.Frame(header, style="Top.TFrame")
            info_row.pack(fill="x", pady=(6, 0))
            left_info = ttk.Frame(info_row, style="Top.TFrame")
            left_info.pack(side="left", fill="x", expand=True)
            self.status_label = ttk.Label(left_info, text="", anchor="w", style="Status.TLabel")
            self.status_label.pack(fill="x")
            self.activity_label = ttk.Label(left_info, text="", anchor="w", style="Status.TLabel")
            self.activity_label.pack(fill="x", pady=(2, 0))
            progress_wrap = ttk.Frame(info_row, style="Top.TFrame")
            progress_wrap.pack(side="right", padx=(8, 0))
            self.progress_title_label = ttk.Label(progress_wrap, text=self._t("progress.label"), style="Status.TLabel")
            self.progress_title_label.pack(anchor="w")
            self.progress_bar = ttk.Progressbar(progress_wrap, mode="determinate", length=260)
            self.progress_bar.pack(anchor="e")

            separator = ttk.Separator(self.root, orient="horizontal")
            separator.pack(fill="x")

            self.vertical_pane = ttk.PanedWindow(self.root, orient="vertical")
            self.vertical_pane.pack(fill="both", expand=True)

            main_area = ttk.Frame(self.vertical_pane)
            footer_area = ttk.Frame(self.vertical_pane)
            self.vertical_pane.add(main_area, weight=5)
            self.vertical_pane.add(footer_area, weight=1)

            # Set a safe initial split once geometry is ready.
            self.root.after_idle(self._set_initial_vertical_split)

            main_pane = ttk.PanedWindow(main_area, orient="horizontal")
            main_pane.pack(fill="both", expand=True)

            nav_frame = ttk.Frame(main_pane, padding=(8, 8, 4, 8), style="Panel.TFrame")
            content_pane = ttk.PanedWindow(main_pane, orient="horizontal")
            main_pane.add(nav_frame, weight=1)
            main_pane.add(content_pane, weight=3)

            nav_header = ttk.Frame(nav_frame, style="Panel.TFrame")
            nav_header.pack(fill="x", pady=(0, 6))
            self.nav_title_label = ttk.Label(nav_header, text=self._t("nav.tree"), style="SectionTitle.TLabel")
            self.nav_title_label.pack(side="left", anchor="w")
            self.nav_toggle_button = ttk.Button(
                nav_header,
                text="⇆",
                width=3,
                style="Toolbar.TButton",
                command=self._toggle_nav_mode,
            )
            self.nav_toggle_button.pack(side="right")
            self.nav_toggle_button.bind("<Enter>", self._show_nav_toggle_tooltip)
            self.nav_toggle_button.bind("<Motion>", self._move_nav_toggle_tooltip)
            self.nav_toggle_button.bind("<Leave>", self._hide_nav_toggle_tooltip)

            self.dir_tree = ttk.Treeview(nav_frame, show="tree")
            nav_scroll = ttk.Scrollbar(nav_frame, orient="vertical", command=self.dir_tree.yview)
            self.dir_tree.configure(yscrollcommand=nav_scroll.set)
            self.dir_tree.pack(side="left", fill="both", expand=True)
            nav_scroll.pack(side="right", fill="y")
            self.dir_tree.bind("<<TreeviewSelect>>", self.on_tree_select)

            list_frame = ttk.Frame(content_pane, padding=(4, 8, 4, 8), style="Panel.TFrame")
            thumb_frame = ttk.Frame(content_pane, padding=(4, 8, 8, 8), style="Panel.TFrame")
            content_pane.add(list_frame, weight=2)
            content_pane.add(thumb_frame, weight=3)

            self.model_list_title_label = ttk.Label(
                list_frame,
                text=self._t("model_list.title"),
                style="SectionTitle.TLabel",
            )
            self.model_list_title_label.pack(anchor="w", pady=(0, 6))

            self.file_table = ttk.Treeview(
                list_frame,
                columns=("status", "name", "size", "date"),
                show="headings",
                height=18,
            )
            self.file_table.heading("status", text=self._t("table.status"), command=lambda: self._on_table_heading_click("status"))
            self.file_table.heading("name", text=self._t("table.name"), command=lambda: self._on_table_heading_click("name"))
            self.file_table.heading("size", text=self._t("table.size"), command=lambda: self._on_table_heading_click("size"))
            self.file_table.heading("date", text=self._t("table.date"), command=lambda: self._on_table_heading_click("date"))
            self.file_table.column("status", width=54, minwidth=54, stretch=False, anchor="center")
            self.file_table.column("name", width=280, anchor="w")
            self.file_table.column("size", width=110, anchor="e")
            self.file_table.column("date", width=170, anchor="w")
            self.file_table.tag_configure("status_missing", foreground="#8f98a3")
            self.file_table.tag_configure("status_computing", foreground="#c69214")
            self.file_table.tag_configure("status_ready", foreground="#2c8f5c")
            self.file_table.tag_configure("status_stale", foreground="#6f8c7b")

            table_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_table.yview)
            self.file_table.configure(yscrollcommand=table_scroll.set)
            self.file_table.pack(side="left", fill="both", expand=True)
            table_scroll.pack(side="right", fill="y")
            self.file_context_menu = tk.Menu(self.root, tearoff=0)
            self.file_context_menu.add_command(label=self._t("context.open_explorer"), command=self._open_selected_in_explorer)
            self.file_context_menu.add_command(label=self._t("context.open_blender"), command=self._open_selected_in_blender)
            self.file_context_menu.add_command(label=self._t("context.open_bambu"), command=self._open_selected_in_bambu_studio)
            self.file_table.bind("<Button-3>", self._on_file_table_context_menu)

            preview_header = ttk.Frame(thumb_frame, style="Panel.TFrame")
            preview_header.pack(fill="x", pady=(0, 6))
            preview_title_wrap = ttk.Frame(preview_header, style="Panel.TFrame")
            preview_title_wrap.pack(side="left", fill="x", expand=True)
            self.preview_title_label = ttk.Label(preview_title_wrap, text=self._t("preview.title"), style="SectionTitle.TLabel")
            self.preview_title_label.pack(
                side="left", anchor="w"
            )
            self.preview_dir_label = ttk.Label(
                preview_title_wrap,
                text="",
                style="SectionTitle.TLabel",
            )
            self.preview_dir_label.pack(side="left", padx=(8, 0))
            ttk.Button(
                preview_header,
                text="▶",
                width=3,
                style="Toolbar.TButton",
                command=self.page_thumbnails_next,
            ).pack(side="right", padx=(6, 0))
            ttk.Button(
                preview_header,
                text="◀",
                width=3,
                style="Toolbar.TButton",
                command=self.page_thumbnails_prev,
            ).pack(side="right")

            self.thumb_canvas = tk.Canvas(thumb_frame, highlightthickness=0, bg="#e6ebf2")
            self.thumb_scroll = ttk.Scrollbar(
                thumb_frame, orient="vertical", command=self.thumb_canvas.yview
            )
            self.thumb_canvas.configure(yscrollcommand=self.thumb_scroll.set)

            self.thumb_inner = ttk.Frame(self.thumb_canvas)
            self.thumb_window = self.thumb_canvas.create_window(
                (0, 0), window=self.thumb_inner, anchor="nw"
            )

            self.thumb_inner.bind(
                "<Configure>",
                lambda _: self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all")),
            )
            self.thumb_canvas.bind("<Configure>", self._on_canvas_resize)

            self.thumb_canvas.pack(side="left", fill="both", expand=True)
            self.thumb_scroll.pack(side="right", fill="y")

            footer_sep = ttk.Separator(footer_area, orient="horizontal")
            footer_sep.pack(fill="x")

            footer = ttk.Frame(footer_area, padding=(8, 6, 8, 8), style="Top.TFrame")
            footer.pack(fill="both", expand=True)
            self.activity_title_label = ttk.Label(footer, text=self._t("activity.title"), style="Title.TLabel")
            self.activity_title_label.pack(anchor="w")
            log_row = ttk.Frame(footer)
            log_row.pack(fill="both", expand=True, pady=(4, 0))
            self.log_text = tk.Text(log_row, height=5, wrap="word", state="disabled")
            log_scroll = ttk.Scrollbar(log_row, orient="vertical", command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=log_scroll.set)
            self.log_text.configure(bg="#ffffff", fg="#1f2d3d", insertbackground="#1f2d3d")
            self.log_text.tag_configure("INFO", foreground="#1f2d3d")
            self.log_text.tag_configure("WARN", foreground="#b26a00")
            self.log_text.tag_configure("ERROR", foreground="#b42318")
            self.log_text.pack(side="left", fill="both", expand=True)
            log_scroll.pack(side="right", fill="y")

        def _set_initial_vertical_split(self):
            if self._initial_sash_set:
                return
            total_h = self.vertical_pane.winfo_height()
            if total_h <= 1:
                self.root.after(40, self._set_initial_vertical_split)
                return
            # Keep central area clearly visible and default log area around ~5 lines.
            footer_h = 145
            main_h = max(320, total_h - footer_h)
            self.vertical_pane.sashpos(0, main_h)
            self._initial_sash_set = True

        def _append_log(self, message: str):
            timestamp = datetime.now().strftime("%H:%M:%S")
            upper = message.upper()
            tag = "INFO"
            if "FEHLER" in upper or "ERROR" in upper or "ABGEBROCHEN" in upper:
                tag = "ERROR"
            elif "WARN" in upper:
                tag = "WARN"
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{timestamp}] {message}\n", (tag,))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def _t(self, key: str, **kwargs) -> str:
            return tr(self.language, key, **kwargs)

        def _config_int(self, key: str, default: int) -> int:
            value = self.gui_config.get(key)
            if not isinstance(value, int) or value <= 0:
                return default
            return value

        def _config_ext(self, key: str, default: str) -> str:
            value = self.gui_config.get(key)
            if isinstance(value, str) and value in GUI_EXT_CHOICES:
                return value
            return default

        def _config_float(self, key: str, default: float, min_v: float, max_v: float) -> float:
            value = self.gui_config.get(key)
            try:
                num = float(value)
            except (TypeError, ValueError):
                return default
            return max(min_v, min(max_v, num))

        def _config_renderer(self, key: str, default: str) -> str:
            value = self.gui_config.get(key)
            if value == "auto":
                return "blender"
            if isinstance(value, str) and value in RENDERER_CHOICES:
                return value
            return default

        def _config_blender_preset(self, key: str, default: str) -> str:
            value = self.gui_config.get(key)
            if isinstance(value, str) and value in BLENDER_PRESET_CHOICES:
                return value
            return default

        def _config_language(self, key: str, default: str) -> str:
            return normalize_language(self.gui_config.get(key, default))

        def _resolve_index_dir(self, value, fallback: Path) -> Path:
            if not isinstance(value, str) or not value.strip():
                return fallback.resolve()
            index_path = Path(value.strip()).expanduser()
            if not index_path.is_absolute():
                index_path = (self.source / index_path).resolve()
            return index_path.resolve()

        def _resolve_optional_path(self, value) -> Path | None:
            if not isinstance(value, str) or not value.strip():
                return None
            return Path(value.strip()).expanduser().resolve()

        def _save_gui_config(self):
            # Reload before writing so we do not overwrite fields saved elsewhere
            # (for example last_start_dir) with stale in-memory data.
            cfg = load_app_config()
            cfg["last_start_dir"] = str(self.source.resolve())
            cfg["gui"] = {
                "index_dir": str(self.index_dir),
                "render_width": self.render_width,
                "render_height": self.render_height,
                "render_threads": self.render_threads,
                "render_margin": self.render_margin,
                "image_ext": self.image_ext,
                "renderer": self.renderer,
                "blender_preset": self.blender_preset,
                "language": self.language,
                "blender_path": str(self.blender_path) if self.blender_path else "",
                "bambu_studio_path": str(self.bambu_studio_path) if self.bambu_studio_path else "",
            }
            save_app_config(cfg)
            self.config = cfg

        def _default_index_for_source(self) -> Path:
            return (self.source / "Index").resolve()

        def _set_activity(self, text: str):
            self.activity_label.config(text=text)

        def _clear_activity(self):
            self.activity_label.config(text="")

        def _on_canvas_resize(self, event):
            self.thumb_canvas.itemconfigure(self.thumb_window, width=event.width)
            if not self.current_thumb_files:
                return
            new_cols = self._compute_thumb_columns()
            if new_cols == self.thumb_columns:
                return
            if self.thumb_relayout_after_id is not None:
                self.root.after_cancel(self.thumb_relayout_after_id)
            self.thumb_relayout_after_id = self.root.after(120, self._relayout_thumbnails)

        def _relayout_thumbnails(self):
            self.thumb_relayout_after_id = None
            if not self.current_thumb_files:
                return
            self._populate_thumbnails(self.current_thumb_files, update_progress=False)

        def _compute_thumb_columns(self) -> int:
            thumb_w = 320
            tile_w = thumb_w + 20
            available = max(1, self.thumb_canvas.winfo_width() - 10)
            cols = available // tile_w
            cols = max(2, cols)
            return min(8, cols)

        def _set_path_text(self):
            self.path_label.config(
                text=self._t(
                    "path.info",
                    source=self.source,
                    index_dir=self.index_dir,
                    width=self.render_width,
                    height=self.render_height,
                    ext=self.image_ext,
                    renderer=self.renderer,
                    preset=self.blender_preset,
                    threads=self.render_threads,
                    margin=self.render_margin,
                )
            )

        def _set_summary_text(self):
            self.stat_stl.config(text=str(self.summary.stl_count))
            self.stat_blend.config(text=str(self.summary.blend_only_count))
            self.stat_total.config(text=str(self.summary.total_models))
            self.stat_existing.config(text=str(self.summary.images_available))
            self.stat_todo.config(text=str(self.summary.images_to_generate))

        def _apply_language_to_ui(self):
            self.root.title(self._t("app.title"))
            self._build_menu()
            self.btn_rescan.config(text=self._t("toolbar.rescan"))
            self.btn_render_current.config(text=self._t("toolbar.render_current"))
            self.btn_render_all.config(text=self._t("toolbar.render_all"))
            self.btn_abort.config(text=self._t("toolbar.abort"))
            self.btn_delete_index.config(text=self._t("toolbar.delete_index"))
            self.search_label.config(text=self._t("toolbar.search"))
            self.btn_clear_search.config(text=self._t("toolbar.search_clear"))
            self.stat_stl_title.config(text=self._t("stats.stl"))
            self.stat_blend_title.config(text=self._t("stats.blend_only"))
            self.stat_total_title.config(text=self._t("stats.total"))
            self.stat_existing_title.config(text=self._t("stats.images"))
            self.stat_todo_title.config(text=self._t("stats.todo"))
            self.nav_title_label.config(text=self._t("nav.timeline") if self._is_timeline_mode() else self._t("nav.tree"))
            self.model_list_title_label.config(text=self._t("model_list.title"))
            self.preview_title_label.config(text=self._t("preview.title"))
            self.activity_title_label.config(text=self._t("activity.title"))
            self.progress_title_label.config(text=self._t("progress.label"))
            self.file_context_menu.entryconfig(0, label=self._t("context.open_explorer"))
            self.file_context_menu.entryconfig(1, label=self._t("context.open_blender"))
            self.file_context_menu.entryconfig(2, label=self._t("context.open_bambu"))
            self._set_path_text()
            self._update_table_heading_indicators()
            if self.nav_toggle_tooltip_label is not None:
                self.nav_toggle_tooltip_label.config(text=self._nav_toggle_tooltip_text())
            self._build_tree()
            self._refresh_current_view()

        def _is_timeline_mode(self) -> bool:
            return self.nav_mode.get() == "timeline"

        def _timeline_keys(self, groups: dict[str, list[Path]]) -> list[str]:
            return sorted(groups.keys(), reverse=True)

        def _month_label(self, month_key: str) -> str:
            return month_label(self.language, month_key)

        def _build_timeline_groups(self) -> dict[str, list[Path]]:
            groups: dict[str, list[Path]] = {}
            query_active = bool(self.search_var.get().strip())
            allowed = self.search_match_files if query_active else None
            for rec in self.model_records:
                rel = rec.get("rel_path")
                month = rec.get("month")
                if not isinstance(rel, str) or not isinstance(month, str):
                    continue
                path = (self.source / rel).resolve()
                if allowed is not None and path not in allowed:
                    continue
                groups.setdefault(month, []).append(path)
            for month in list(groups.keys()):
                groups[month] = sorted(groups[month], key=lambda p: p.name.lower())
            return groups

        def _toggle_nav_mode(self):
            if self._is_timeline_mode():
                self.nav_mode.set("tree")
            else:
                self.nav_mode.set("timeline")
            self._on_nav_mode_changed()

        def _nav_toggle_tooltip_text(self) -> str:
            if self._is_timeline_mode():
                return self._t("nav.switch_to_tree")
            return self._t("nav.switch_to_timeline")

        def _show_nav_toggle_tooltip(self, event=None):
            if self.nav_toggle_tooltip_win is None or not self.nav_toggle_tooltip_win.winfo_exists():
                self.nav_toggle_tooltip_win = tk.Toplevel(self.root)
                self.nav_toggle_tooltip_win.overrideredirect(True)
                self.nav_toggle_tooltip_win.attributes("-topmost", True)
                self.nav_toggle_tooltip_label = tk.Label(
                    self.nav_toggle_tooltip_win,
                    text=self._nav_toggle_tooltip_text(),
                    bg="#1f2d3d",
                    fg="#ffffff",
                    padx=8,
                    pady=4,
                    relief="solid",
                    bd=1,
                )
                self.nav_toggle_tooltip_label.pack()
            elif self.nav_toggle_tooltip_label is not None:
                self.nav_toggle_tooltip_label.config(text=self._nav_toggle_tooltip_text())
            self._move_nav_toggle_tooltip(event)
            self.nav_toggle_tooltip_win.deiconify()

        def _move_nav_toggle_tooltip(self, event=None):
            if self.nav_toggle_tooltip_win is None or not self.nav_toggle_tooltip_win.winfo_exists():
                return
            x = self.root.winfo_pointerx() + 14
            y = self.root.winfo_pointery() + 14
            self.nav_toggle_tooltip_win.geometry(f"+{x}+{y}")

        def _hide_nav_toggle_tooltip(self, _event=None):
            if self.nav_toggle_tooltip_win is not None and self.nav_toggle_tooltip_win.winfo_exists():
                self.nav_toggle_tooltip_win.withdraw()

        def _on_nav_mode_changed(self):
            self.nav_title_label.config(text=self._t("nav.timeline") if self._is_timeline_mode() else self._t("nav.tree"))
            if self.nav_toggle_tooltip_label is not None:
                self.nav_toggle_tooltip_label.config(text=self._nav_toggle_tooltip_text())
            self._build_tree()
            self._refresh_current_view()

        def _refresh_current_view(self):
            if self._is_timeline_mode():
                groups = self._build_timeline_groups()
                if not groups:
                    self.selected_timeline_key = None
                    self.selected_timeline_year = None
                    self._show_models([], self._t("nav.timeline"))
                    return
                if self.selected_timeline_year:
                    self._show_timeline_year(self.selected_timeline_year)
                    return
                keys = self._timeline_keys(groups)
                if self.selected_timeline_key not in groups:
                    self.selected_timeline_key = keys[0]
                self._show_timeline_month(self.selected_timeline_key)
            else:
                self._show_directory(self.selected_directory)

        def _show_timeline_month(self, month_key: str):
            groups = self._build_timeline_groups()
            self.selected_timeline_year = None
            self.selected_timeline_key = month_key
            files = groups.get(month_key, [])
            self._show_models(files, self._month_label(month_key))

        def _show_timeline_year(self, year: str):
            groups = self._build_timeline_groups()
            self.selected_timeline_year = year
            self.selected_timeline_key = None
            files: list[Path] = []
            for month_key in self._timeline_keys(groups):
                if month_key.startswith(year + "-"):
                    files.extend(groups.get(month_key, []))
            self._show_models(files, year)

        def _visible_directories_for_nav(self) -> list[Path]:
            query_active = bool(self.search_var.get().strip())
            dirs = []
            for d in self.directory_snapshot:
                if query_active and d not in self.search_match_dirs:
                    continue
                dirs.append(d)
            if self.source not in dirs:
                dirs.insert(0, self.source)
            return dirs

        def _select_directory(self, directory: Path):
            self.selected_directory = directory
            tree_id = self.path_to_tree_id.get(directory)
            if tree_id:
                self.dir_tree.selection_set(tree_id)
                self.dir_tree.see(tree_id)
            self._show_directory(directory)

        def _next_directory_for_paging(self, current: Path) -> Path | None:
            dirs = self._visible_directories_for_nav()
            dir_set = set(dirs)

            # 1) Prefer first child directory.
            children = sorted([d for d in dirs if d.parent == current], key=lambda p: p.name.lower())
            if children:
                return children[0]

            # 2) Otherwise search for next sibling; if none, climb to parent and retry.
            node = current
            while node != self.source:
                parent = node.parent
                if parent not in dir_set:
                    break
                siblings = sorted([d for d in dirs if d.parent == parent], key=lambda p: p.name.lower())
                for idx, d in enumerate(siblings):
                    if d == node and idx + 1 < len(siblings):
                        return siblings[idx + 1]
                node = parent
            return None

        def page_thumbnails_prev(self):
            if self._is_timeline_mode():
                groups = self._build_timeline_groups()
                keys = self._timeline_keys(groups)
                if self.selected_timeline_year:
                    years = sorted({k.split("-", 1)[0] for k in keys}, reverse=True)
                    if self.selected_timeline_year in years:
                        idx = years.index(self.selected_timeline_year)
                        if idx - 1 >= 0:
                            prev_year = years[idx - 1]
                            self.selected_timeline_year = prev_year
                            tree_id = self.year_to_tree_id.get(prev_year)
                            if tree_id:
                                self.dir_tree.selection_set(tree_id)
                                self.dir_tree.see(tree_id)
                            self._show_timeline_year(prev_year)
                            self._append_log(self._t("timeline.prev_year", year=prev_year))
                elif self.selected_timeline_key and self.selected_timeline_key in keys:
                    idx = keys.index(self.selected_timeline_key)
                    if idx - 1 >= 0:
                        prev_key = keys[idx - 1]
                        self.selected_timeline_key = prev_key
                        tree_id = self.time_to_tree_id.get(prev_key)
                        if tree_id:
                            self.dir_tree.selection_set(tree_id)
                            self.dir_tree.see(tree_id)
                        self._show_timeline_month(prev_key)
                        self._append_log(self._t("timeline.prev_month", month=self._month_label(prev_key)))
                return
            top, bottom = self.thumb_canvas.yview()
            page = max(0.08, bottom - top)
            self.thumb_canvas.yview_moveto(max(0.0, top - page))

        def page_thumbnails_next(self):
            top, bottom = self.thumb_canvas.yview()
            page = max(0.08, bottom - top)
            if bottom < 0.999:
                self.thumb_canvas.yview_moveto(min(1.0, top + page))
                return
            if self._is_timeline_mode():
                groups = self._build_timeline_groups()
                keys = self._timeline_keys(groups)
                if self.selected_timeline_year:
                    years = sorted({k.split("-", 1)[0] for k in keys}, reverse=True)
                    if self.selected_timeline_year in years:
                        idx = years.index(self.selected_timeline_year)
                        if idx + 1 < len(years):
                            next_year = years[idx + 1]
                            self.selected_timeline_year = next_year
                            tree_id = self.year_to_tree_id.get(next_year)
                            if tree_id:
                                self.dir_tree.selection_set(tree_id)
                                self.dir_tree.see(tree_id)
                            self._show_timeline_year(next_year)
                            self._append_log(self._t("timeline.next_year", year=next_year))
                elif self.selected_timeline_key in keys:
                    idx = keys.index(self.selected_timeline_key)
                    if idx + 1 < len(keys):
                        next_key = keys[idx + 1]
                        self.selected_timeline_key = next_key
                        tree_id = self.time_to_tree_id.get(next_key)
                        if tree_id:
                            self.dir_tree.selection_set(tree_id)
                            self.dir_tree.see(tree_id)
                        self._show_timeline_month(next_key)
                        self._append_log(self._t("timeline.next_month", month=self._month_label(next_key)))
                return
            next_dir = self._next_directory_for_paging(self.selected_directory)
            if next_dir is not None:
                self._select_directory(next_dir)
                self._append_log(self._t("paging.next_dir", directory=next_dir))

        def _set_status(self, text: str):
            self.status_label.config(text=text)

        def _set_progress_indeterminate(self):
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start(12)

        def _set_progress(self, value: int, maximum: int):
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=max(1, maximum), value=value)

        def _clear_progress(self):
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=1, value=0)

        def _cache_path(self) -> Path:
            return self.index_dir / ".stlpreview_scan_cache.json"

        def _collect_model_records(self) -> list[dict]:
            records: list[dict] = []
            src = self.source.resolve()
            for model in iter_render_sources(src, self.index_dir):
                try:
                    stat = model.stat()
                except OSError:
                    continue
                out_path = target_image_path(model, src, self.index_dir, self.image_ext)
                if not out_path.exists():
                    status = "missing"
                elif needs_render(model, out_path, overwrite=False):
                    status = "stale"
                else:
                    status = "ready"
                try:
                    rel = str(model.resolve().relative_to(src))
                except Exception:
                    continue
                modified = datetime.fromtimestamp(stat.st_mtime)
                records.append(
                    {
                        "rel_path": rel,
                        "name": model.name,
                        "dir_rel": str(model.parent.resolve().relative_to(src)),
                        "size": int(stat.st_size),
                        "mtime": float(stat.st_mtime),
                        "modified": modified.isoformat(timespec="seconds"),
                        "month": modified.strftime("%Y-%m"),
                        "type": model.suffix.lower().lstrip("."),
                        "status": status,
                    }
                )
            records.sort(key=lambda r: str(r.get("rel_path", "")).lower())
            return records

        def _save_scan_cache(self, summary: ScanSummary, directories: list[Path], model_records: list[dict]):
            try:
                rel_dirs = []
                src = self.source.resolve()
                for d in directories:
                    try:
                        rel_dirs.append(str(d.resolve().relative_to(src)))
                    except Exception:
                        continue
                payload = {
                    "version": 2,
                    "scanned_at": datetime.now().isoformat(timespec="seconds"),
                    "source": str(src),
                    "index_dir": str(self.index_dir.resolve()),
                    "image_ext": self.image_ext,
                    "summary": asdict(summary),
                    "directories_rel": rel_dirs,
                    "model_files": model_records,
                }
                self.index_dir.mkdir(parents=True, exist_ok=True)
                self._cache_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except Exception:
                pass

        def _try_load_scan_cache(self) -> tuple[ScanSummary, list[Path], list[dict]] | None:
            cache_path = self._cache_path()
            if not cache_path.exists():
                return None
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return None
                if data.get("version") != 2:
                    return None
                if data.get("source") != str(self.source.resolve()):
                    return None
                if data.get("index_dir") != str(self.index_dir.resolve()):
                    return None
                if data.get("image_ext") != self.image_ext:
                    return None
                scanned_raw = data.get("scanned_at")
                if not isinstance(scanned_raw, str):
                    return None
                scanned_at = datetime.fromisoformat(scanned_raw)
                if datetime.now() - scanned_at > timedelta(days=3):
                    return None

                summary_obj = data.get("summary")
                if not isinstance(summary_obj, dict):
                    return None
                summary = ScanSummary(
                    stl_count=int(summary_obj.get("stl_count", 0)),
                    blend_only_count=int(summary_obj.get("blend_only_count", 0)),
                    total_models=int(summary_obj.get("total_models", 0)),
                    images_available=int(summary_obj.get("images_available", 0)),
                    images_to_generate=int(summary_obj.get("images_to_generate", 0)),
                )

                dirs_rel = data.get("directories_rel")
                directories: list[Path] = []
                if isinstance(dirs_rel, list):
                    for rel in dirs_rel:
                        if not isinstance(rel, str):
                            continue
                        d = (self.source / rel).resolve()
                        if d.exists() and d.is_dir():
                            directories.append(d)
                if self.source not in directories:
                    directories.insert(0, self.source)
                directories = sorted(set(directories), key=lambda p: str(p).lower())
                model_files_raw = data.get("model_files")
                model_files: list[dict] = []
                if isinstance(model_files_raw, list):
                    for rec in model_files_raw:
                        if not isinstance(rec, dict):
                            continue
                        rel = rec.get("rel_path")
                        month = rec.get("month")
                        modified = rec.get("modified")
                        status = rec.get("status")
                        if not isinstance(rel, str) or not isinstance(month, str) or not isinstance(modified, str):
                            continue
                        if not isinstance(status, str):
                            status = "missing"
                        model_files.append(
                            {
                                "rel_path": rel,
                                "name": rec.get("name", Path(rel).name),
                                "dir_rel": rec.get("dir_rel", str(Path(rel).parent)),
                                "size": int(rec.get("size", 0)),
                                "mtime": float(rec.get("mtime", 0.0)),
                                "modified": modified,
                                "month": month,
                                "type": rec.get("type", Path(rel).suffix.lower().lstrip(".")),
                                "status": status,
                            }
                        )
                return summary, directories, model_files
            except Exception:
                return None

        def _start_initial_scan(self, force: bool = False):
            if self.render_running:
                self._set_status(self._t("scan.skipped_render_running"))
                return

            if not force:
                cached = self._try_load_scan_cache()
                if cached is not None:
                    summary, directories, model_records = cached
                    self.summary = summary
                    self.directory_snapshot = directories
                    self.model_records = model_records
                    self._set_summary_text()
                    self._build_tree()
                    self._refresh_current_view()
                    self._set_status(self._t("scan.loaded_cache"))
                    self._clear_progress()
                    self._clear_activity()
                    self._append_log(self._t("scan.cache_used"))
                    return

            self._set_path_text()
            self._set_status(self._t("scan.loading_tree"))
            self._set_activity(self._t("state.initial_scan"))
            self._set_progress_indeterminate()
            thread = threading.Thread(target=self._initial_scan_worker, daemon=True)
            thread.start()

        def _initial_scan_worker(self):
            try:
                directories = collect_directories(self.source, self.index_dir)
                self.ui_queue.put(("dirs_ready", directories))
                summary = scan_summary(self.source, self.index_dir, self.image_ext)
                model_records = self._collect_model_records()
                self.ui_queue.put(("scan_complete", summary, directories, model_records))
            except Exception as exc:
                self.ui_queue.put(("scan_error", str(exc)))

        def _process_ui_queue(self):
            while True:
                try:
                    event = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                self._handle_ui_event(event)
            self.root.after(100, self._process_ui_queue)

        def _handle_ui_event(self, event: tuple):
            kind = event[0]
            if kind == "dirs_ready":
                self.directory_snapshot = event[1]
                self._build_tree()
                self._refresh_current_view()
                self._set_status(self._t("scan.tree_loaded_scanning"))
                self._append_log(self._t("scan.tree_loaded_n", count=len(self.directory_snapshot)))
            elif kind == "scan_complete":
                self.summary = event[1]
                directories = event[2]
                model_records = event[3]
                self.directory_snapshot = directories
                self.model_records = model_records
                self._set_summary_text()
                self._set_status(self._t("scan.complete"))
                self._clear_progress()
                if not self.render_running:
                    self._clear_activity()
                self._save_scan_cache(self.summary, directories, model_records)
                self._build_tree()
                self._refresh_current_view()
                self._append_log(
                    self._t(
                        "scan.complete.log",
                        stl=self.summary.stl_count,
                        blend_only=self.summary.blend_only_count,
                        total=self.summary.total_models,
                        images=self.summary.images_available,
                        todo=self.summary.images_to_generate,
                    )
                )
            elif kind == "scan_error":
                self._set_status(self._t("scan.failed", error=event[1]))
                self._clear_progress()
                self._clear_activity()
                self._append_log(self._t("scan.error_log", error=event[1]))
            elif kind == "render_started":
                total, scope_label = event[1], event[2]
                self.render_progress = RenderProgress(total=total)
                self.render_running = True
                self._set_status(self._t("render.started_status", scope=scope_label))
                self._set_activity(self._t("render.running", done=0, total=total))
                self._set_progress(0, max(1, total))
                self._refresh_table_statuses()
                self._append_log(
                    self._t("render.started_log", scope=scope_label, renderer=self.renderer, tasks=total)
                )
            elif kind == "render_collecting":
                scanned, queued, scope_label = event[1], event[2], event[3]
                self._set_status(self._t("render.preparing_status", scope=scope_label))
                self._set_activity(self._t("render.preparing_activity", scanned=scanned, queued=queued))
            elif kind == "render_progress":
                if self.render_progress is None:
                    return
                ok, missing_preview, filename, out_path, err, model_path_str = (
                    event[1],
                    event[2],
                    event[3],
                    event[4],
                    event[5],
                    event[6],
                )
                self.render_inflight_paths.discard(Path(model_path_str).resolve())
                model_path = Path(model_path_str).resolve()
                self.render_progress.processed += 1
                if ok:
                    self.render_progress.succeeded += 1
                    self.summary.images_to_generate = max(0, self.summary.images_to_generate - 1)
                    if missing_preview:
                        self.summary.images_available += 1
                else:
                    self.render_progress.failed += 1
                self._set_summary_text()
                self._set_progress(self.render_progress.processed, max(1, self.render_progress.total))
                self._set_activity(
                    self._t(
                        "render.running_detail",
                        done=self.render_progress.processed,
                        total=self.render_progress.total,
                        ok=self.render_progress.succeeded,
                        failed=self.render_progress.failed,
                        file=filename,
                    )
                )
                self._refresh_table_statuses()
                self._update_thumbnail_for_model(model_path, in_progress=False)
                if ok:
                    self._append_log(self._t("log.ok", file=filename, target=out_path))
                else:
                    self._append_log(self._t("log.error", file=filename, error=err, target=out_path))
            elif kind == "render_done":
                self.render_running = False
                self.current_render_scope = None
                self.current_render_overwrite = False
                self.current_render_dir = None
                self.render_inflight_paths.clear()
                rendered, failed = event[1], event[2]
                self._set_status(self._t("render.done_status", rendered=rendered, failed=failed))
                self._clear_progress()
                self._clear_activity()
                self._refresh_current_view()
                self._append_log(self._t("render.done_log", rendered=rendered, failed=failed))
            elif kind == "render_cancelled":
                self.render_running = False
                self.current_render_scope = None
                self.current_render_overwrite = False
                self.current_render_dir = None
                self.render_inflight_paths.clear()
                rendered, failed, remaining = event[1], event[2], event[3]
                self._set_status(self._t("render.cancel_status", rendered=rendered, failed=failed, remaining=remaining))
                self._clear_progress()
                self._clear_activity()
                self._refresh_current_view()
                self._append_log(self._t("render.cancel_log", rendered=rendered, failed=failed, remaining=remaining))
            elif kind == "render_error":
                self.render_running = False
                self.current_render_scope = None
                self.current_render_overwrite = False
                self.current_render_dir = None
                self.render_inflight_paths.clear()
                self._set_status(self._t("render.failed_status", error=event[1]))
                self._clear_progress()
                self._clear_activity()
                self._append_log(self._t("render.failed_log", error=event[1]))
            elif kind == "render_task_start":
                model_path = Path(event[1]).resolve()
                self.render_inflight_paths.add(model_path)
                self._refresh_table_statuses()
                self._update_thumbnail_for_model(model_path, in_progress=True)
            elif kind == "thumb_progress":
                if self.render_running:
                    return
                done, total = event[1], event[2]
                self._set_progress(done, max(1, total))
                self._set_activity(self._t("preview.building", done=done, total=total))
            elif kind == "thumb_done":
                if self.render_running:
                    return
                self._clear_progress()
                self._clear_activity()
                self._set_status(self._t("preview.updated"))
            elif kind == "log":
                self._append_log(event[1])

        def _build_tree(self):
            self.dir_tree.delete(*self.dir_tree.get_children())
            self.tree_paths.clear()
            self.path_to_tree_id.clear()
            self.time_to_tree_id.clear()
            self.year_to_tree_id.clear()

            query_active = bool(self.search_var.get().strip())
            root_label = self.source.name if self.source.name else str(self.source)
            root_id = self.dir_tree.insert("", "end", text=root_label, open=True)
            self.tree_paths[root_id] = ("dir", self.source)
            self.path_to_tree_id[self.source] = root_id
            if self._is_timeline_mode():
                groups = self._build_timeline_groups()
                year_nodes: dict[str, str] = {}
                for month_key in self._timeline_keys(groups):
                    year = month_key.split("-", 1)[0]
                    year_id = year_nodes.get(year)
                    if year_id is None:
                        year_id = self.dir_tree.insert(root_id, "end", text=year, open=True)
                        self.tree_paths[year_id] = ("timeline_year", year)
                        year_nodes[year] = year_id
                        self.year_to_tree_id[year] = year_id
                    month_id = self.dir_tree.insert(
                        year_id,
                        "end",
                        text=self._month_label(month_key),
                        open=False,
                    )
                    self.tree_paths[month_id] = ("timeline_month", month_key)
                    self.time_to_tree_id[month_key] = month_id
                if groups:
                    if self.selected_timeline_year and self.selected_timeline_year in year_nodes:
                        sel_id = year_nodes[self.selected_timeline_year]
                    else:
                        if self.selected_timeline_key not in groups:
                            self.selected_timeline_key = self._timeline_keys(groups)[0]
                        sel_id = self.time_to_tree_id.get(self.selected_timeline_key, root_id)
                else:
                    sel_id = root_id
            else:
                node_ids: dict[Path, str] = {self.source: root_id}
                for directory in self.directory_snapshot:
                    if directory == self.source:
                        continue
                    if query_active and directory not in self.search_match_dirs:
                        continue
                    parent = directory.parent
                    parent_id = node_ids.get(parent)
                    if parent_id is None:
                        continue
                    item_id = self.dir_tree.insert(parent_id, "end", text=directory.name, open=False)
                    self.tree_paths[item_id] = ("dir", directory)
                    self.path_to_tree_id[directory] = item_id
                    node_ids[directory] = item_id
                selected = self.selected_directory
                if selected not in self.path_to_tree_id:
                    if query_active and self.search_match_dirs:
                        selected = next(iter(sorted(self.search_match_dirs, key=lambda p: str(p).lower())), self.source)
                    else:
                        selected = self.source
                sel_id = self.path_to_tree_id[selected]
            self.dir_tree.selection_set(sel_id)
            self.dir_tree.see(sel_id)

        def on_tree_select(self, _event=None):
            selection = self.dir_tree.selection()
            if not selection:
                return
            entry = self.tree_paths.get(selection[0])
            if not isinstance(entry, tuple) or len(entry) != 2:
                return
            kind, value = entry
            if kind == "dir" and isinstance(value, Path):
                self.selected_directory = value
                self._show_directory(value)
            elif kind == "timeline_month" and isinstance(value, str):
                self.selected_timeline_key = value
                self.selected_timeline_year = None
                self._show_timeline_month(value)
            elif kind == "timeline_year" and isinstance(value, str):
                self.selected_timeline_year = value
                self.selected_timeline_key = None
                self._show_timeline_year(value)

        def _show_models(self, model_files: list[Path], title: str):
            self._hide_hover_preview()
            self.thumb_canvas.yview_moveto(0.0)
            self.preview_dir_label.config(text=title)
            self.current_model_files = list(model_files)
            model_files = self._sort_model_files(model_files)
            self._update_table_heading_indicators()
            self.file_table.delete(*self.file_table.get_children())
            self.file_item_paths.clear()
            for model in model_files:
                try:
                    stat = model.stat()
                except OSError:
                    continue
                modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                status_key, status_symbol = self._status_for_model(model)
                item_id = self.file_table.insert(
                    "",
                    "end",
                    values=(status_symbol, model.name, format_file_size(stat.st_size), modified),
                    tags=(f"status_{status_key}",),
                )
                self.file_item_paths[item_id] = model

            self._populate_thumbnails(model_files)

        def _on_table_heading_click(self, column: str):
            if self.table_sort_column == column:
                self.table_sort_desc = not self.table_sort_desc
            else:
                self.table_sort_column = column
                self.table_sort_desc = False
            self._refresh_current_view()

        def _update_table_heading_indicators(self):
            titles = {
                "status": self._t("table.status"),
                "name": self._t("table.name"),
                "size": self._t("table.size"),
                "date": self._t("table.date"),
            }
            arrow = " ▼" if self.table_sort_desc else " ▲"
            for col, base in titles.items():
                text = base + arrow if col == self.table_sort_column else base
                self.file_table.heading(col, text=text, command=lambda c=col: self._on_table_heading_click(c))

        def _sort_model_files(self, model_files: list[Path]) -> list[Path]:
            status_order = {"missing": 0, "computing": 1, "stale": 2, "ready": 3}

            def sort_key(model: Path):
                try:
                    stat = model.stat()
                    size = stat.st_size
                    mtime = stat.st_mtime
                except OSError:
                    size = -1
                    mtime = 0.0
                if self.table_sort_column == "size":
                    return (size, model.name.lower())
                if self.table_sort_column == "date":
                    return (mtime, model.name.lower())
                if self.table_sort_column == "status":
                    status_key, _ = self._status_for_model(model)
                    return (status_order.get(status_key, 99), model.name.lower())
                return model.name.lower()

            return sorted(model_files, key=sort_key, reverse=self.table_sort_desc)

        def _show_directory(self, directory: Path):
            if directory == self.source:
                rel_dir = self.source.name if self.source.name else str(self.source)
            else:
                try:
                    rel_dir = str(directory.relative_to(self.source))
                except ValueError:
                    rel_dir = str(directory)
            model_files = list_display_files(directory)
            query = self.search_var.get().strip()
            if query:
                model_files = [p for p in model_files if p in self.search_match_files]
            self._show_models(model_files, rel_dir)

        def _open_in_file_manager(self, path: Path):
            target = path.resolve()
            if not target.exists():
                raise FileNotFoundError(str(target))
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", str(target)])
                return
            # Linux: generic way opens containing directory.
            folder = target.parent if target.is_file() else target
            try:
                subprocess.Popen(["xdg-open", str(folder)])
            except Exception:
                subprocess.Popen(["gio", "open", str(folder)])

        def _open_selected_in_explorer(self):
            if self.context_model_path is None:
                return
            try:
                self._open_in_file_manager(self.context_model_path)
                self._append_log(f"Open in Explorer: {self.context_model_path}")
            except Exception as exc:
                messagebox.showerror(self._t("dialog.error"), self._t("open.explorer.error", error=exc))
                self._append_log(self._t("open.explorer.error_log", error=exc))

        def _detect_bambu_studio_executable(self) -> Path | None:
            found = shutil.which("bambu-studio") or shutil.which("BambuStudio") or shutil.which("bambu_studio")
            if found:
                return Path(found).resolve()

            windows_candidates = [
                Path("C:/Program Files/Bambu Studio/bambu-studio.exe"),
                Path("C:/Program Files/Bambu Studio/BambuStudio.exe"),
                Path("C:/Program Files (x86)/Bambu Studio/bambu-studio.exe"),
                Path("C:/Program Files (x86)/Bambu Studio/BambuStudio.exe"),
                Path.home() / "AppData/Local/Programs/Bambu Studio/bambu-studio.exe",
                Path.home() / "AppData/Local/Programs/Bambu Studio/BambuStudio.exe",
            ]
            for candidate in windows_candidates:
                if candidate.exists() and candidate.is_file():
                    return candidate.resolve()

            linux_candidates = [
                Path("/usr/bin/bambu-studio"),
                Path("/usr/local/bin/bambu-studio"),
                Path("/opt/bambu-studio/bambu-studio"),
            ]
            for candidate in linux_candidates:
                if candidate.exists() and candidate.is_file():
                    return candidate.resolve()
            return None

        def _open_selected_in_blender(self):
            if self.context_model_path is None:
                return
            blender_exe = detect_blender_executable(self.blender_path)
            if blender_exe is None:
                messagebox.showerror(self._t("dialog.error"), self._t("open.blender.not_found"))
                self._append_log(self._t("open.blender.not_found_log"))
                return
            try:
                subprocess.Popen([str(blender_exe), str(self.context_model_path)])
                self._append_log(f"Open in Blender: {self.context_model_path}")
            except Exception as exc:
                messagebox.showerror(self._t("dialog.error"), self._t("open.blender.start_error", error=exc))
                self._append_log(self._t("open.blender.start_error_log", error=exc))

        def _open_selected_in_bambu_studio(self):
            if self.context_model_path is None:
                return
            if self.context_model_path.suffix.lower() != ".stl":
                self._append_log(self._t("open.bambu.skip_non_stl", path=self.context_model_path))
                return
            bambu_exe = self.bambu_studio_path
            if not (bambu_exe and bambu_exe.exists() and bambu_exe.is_file()):
                bambu_exe = self._detect_bambu_studio_executable()
            if bambu_exe is None:
                messagebox.showerror(self._t("dialog.error"), self._t("open.bambu.not_found"))
                self._append_log(self._t("open.bambu.not_found_log"))
                return
            try:
                subprocess.Popen([str(bambu_exe), str(self.context_model_path)])
                self._append_log(f"Open in Bambu Studio: {self.context_model_path}")
            except Exception as exc:
                messagebox.showerror(self._t("dialog.error"), self._t("open.bambu.start_error", error=exc))
                self._append_log(self._t("open.bambu.start_error_log", error=exc))

        def _on_file_table_context_menu(self, event):
            row_id = self.file_table.identify_row(event.y)
            if not row_id:
                return
            self.file_table.selection_set(row_id)
            model = self.file_item_paths.get(row_id)
            if model is None:
                return
            self.context_model_path = model
            self.file_context_menu.entryconfig(
                self._t("context.open_bambu"),
                state=("normal" if model.suffix.lower() == ".stl" else "disabled"),
            )
            try:
                self.file_context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.file_context_menu.grab_release()

        def _position_hover_preview(self, x_root: int, y_root: int):
            if self.hover_preview_win is None:
                return
            self.hover_preview_win.update_idletasks()
            win_w = self.hover_preview_win.winfo_width()
            win_h = self.hover_preview_win.winfo_height()
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            x = x_root + 18
            y = y_root + 18
            if x + win_w > screen_w - 8:
                x = max(8, x_root - win_w - 18)
            if y + win_h > screen_h - 8:
                y = max(8, screen_h - win_h - 8)
            self.hover_preview_win.geometry(f"+{x}+{y}")

        def _show_hover_preview(self, model: Path, x_root: int, y_root: int):
            out_path = target_image_path(model, self.source, self.index_dir, self.image_ext)
            if not out_path.exists():
                self._hide_hover_preview()
                return
            try:
                image = tk.PhotoImage(file=str(out_path))
            except tk.TclError:
                self._hide_hover_preview()
                return

            max_w = max(240, int(self.root.winfo_screenwidth() * 0.6))
            max_h = max(180, int(self.root.winfo_screenheight() * 0.6))
            factor = max(1, math.ceil(image.width() / max_w), math.ceil(image.height() / max_h))
            preview = image.subsample(factor, factor)

            if self.hover_preview_win is None or not self.hover_preview_win.winfo_exists():
                self.hover_preview_win = tk.Toplevel(self.root)
                self.hover_preview_win.overrideredirect(True)
                self.hover_preview_win.attributes("-topmost", True)
                self.hover_preview_win.configure(bg="#2b3440")
                self.hover_preview_label = tk.Label(
                    self.hover_preview_win,
                    bg="#2b3440",
                    bd=1,
                    relief="solid",
                    highlightthickness=0,
                )
                self.hover_preview_label.pack(padx=1, pady=1)

            self.hover_preview_image = preview
            self.hover_preview_model = model
            if self.hover_preview_label is not None:
                self.hover_preview_label.configure(image=preview)
            self._position_hover_preview(x_root, y_root)
            self.hover_preview_win.deiconify()

        def _bind_thumbnail_hover(self, widget, model: Path):
            widget.bind("<Enter>", lambda e, m=model: self._show_hover_preview(m, e.x_root, e.y_root))
            widget.bind("<Motion>", lambda e, m=model: self._show_hover_preview(m, e.x_root, e.y_root))
            widget.bind("<Leave>", lambda _e: self._hide_hover_preview())

        def _hide_hover_preview(self):
            self.hover_preview_model = None
            self.hover_preview_image = None
            if self.hover_preview_win is not None and self.hover_preview_win.winfo_exists():
                self.hover_preview_win.withdraw()

        def _status_for_model(self, model: Path) -> tuple[str, str]:
            out_path = target_image_path(model, self.source, self.index_dir, self.image_ext)
            if self.render_running and model.resolve() in self.render_inflight_paths:
                return "computing", "⬤"
            if not out_path.exists():
                return "missing", "⬤"
            if needs_render(model, out_path, overwrite=False):
                return "stale", "⬤"
            return "ready", "⬤"

        def _refresh_table_statuses(self):
            for item_id, model in list(self.file_item_paths.items()):
                if not self.file_table.exists(item_id):
                    continue
                status_key, status_symbol = self._status_for_model(model)
                values = list(self.file_table.item(item_id, "values"))
                if values:
                    values[0] = status_symbol
                    self.file_table.item(item_id, values=values, tags=(f"status_{status_key}",))

        @staticmethod
        def _norm_text(text: str) -> str:
            return "".join(ch for ch in text.lower() if ch.isalnum())

        @staticmethod
        def _levenshtein_similarity(a: str, b: str) -> float:
            if not a and not b:
                return 1.0
            if not a or not b:
                return 0.0
            prev = list(range(len(b) + 1))
            for i, ca in enumerate(a, start=1):
                curr = [i]
                for j, cb in enumerate(b, start=1):
                    ins = curr[j - 1] + 1
                    dele = prev[j] + 1
                    repl = prev[j - 1] + (0 if ca == cb else 1)
                    curr.append(min(ins, dele, repl))
                prev = curr
            dist = prev[-1]
            return 1.0 - (dist / max(len(a), len(b)))

        def _similarity_score(self, query: str, candidate: str) -> float:
            qn = self._norm_text(query)
            cn = self._norm_text(candidate)
            if not qn or not cn:
                return 0.0
            if qn in cn:
                return 1.0
            seq = difflib.SequenceMatcher(None, qn, cn).ratio()
            lev = self._levenshtein_similarity(qn, cn)

            # Token overlap helps for names with separators.
            q_tokens = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if t]
            c_tokens = [t for t in re.split(r"[^a-z0-9]+", candidate.lower()) if t]
            token_score = 0.0
            if q_tokens and c_tokens:
                q_set, c_set = set(q_tokens), set(c_tokens)
                token_score = len(q_set & c_set) / max(len(q_set), len(c_set))

            return max(seq, lev, token_score)

        def _file_matches_exact(self, path: Path, query: str) -> bool:
            q = query.lower()
            return q in path.name.lower() or q in path.stem.lower()

        def _file_matches_fuzzy(self, path: Path, query: str) -> bool:
            qn = self._norm_text(query)
            if not qn:
                return False
            candidates = [path.name, path.stem]
            best = 0.0
            for cand in candidates:
                best = max(best, self._similarity_score(query, cand))
            threshold = 0.8 if len(qn) <= 4 else 0.68
            return best >= threshold

        def _iter_search_candidates(self):
            source = self.source.resolve()
            index_dir = self.index_dir.resolve()
            for p in source.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in (".stl", ".blend"):
                    continue
                try:
                    p.relative_to(index_dir)
                    continue
                except ValueError:
                    pass
                yield p

        def _apply_search(self):
            query = self.search_var.get().strip()
            if not query:
                self.search_match_files.clear()
                self.search_match_dirs.clear()
                self._build_tree()
                self._refresh_current_view()
                return

            candidates = list(self._iter_search_candidates())
            exact = {p for p in candidates if self._file_matches_exact(p, query)}
            if exact:
                matches = exact
                mode = self._t("search.exact")
            else:
                matches = {p for p in candidates if self._file_matches_fuzzy(p, query)}
                mode = self._t("search.fuzzy")

            self.search_match_files = matches
            dirs: set[Path] = set()
            for p in matches:
                d = p.parent.resolve()
                while True:
                    dirs.add(d)
                    if d == self.source:
                        break
                    if self.source not in d.parents:
                        break
                    d = d.parent
            self.search_match_dirs = dirs
            self._build_tree()
            self._refresh_current_view()
            self._append_log(self._t("search.result", query=query, count=len(matches), mode=mode))

        def _clear_search(self):
            if self.search_after_id is not None:
                self.root.after_cancel(self.search_after_id)
                self.search_after_id = None
            self.search_var.set("")
            self._apply_search()

        def _on_search_keyrelease(self, _event=None):
            if self.search_after_id is not None:
                self.root.after_cancel(self.search_after_id)
            # Debounce: run search after a short typing pause.
            self.search_after_id = self.root.after(320, self._run_debounced_search)

        def _run_debounced_search(self):
            self.search_after_id = None
            self._apply_search()

        def _populate_thumbnails(self, stl_files: list[Path], update_progress: bool = True):
            self.thumb_job_token += 1
            token = self.thumb_job_token
            self.current_thumb_files = list(stl_files)
            self.thumbnail_cache.clear()
            self.thumbnail_images.clear()
            self.thumb_items.clear()
            self.thumb_canvas.yview_moveto(0.0)
            for widget in self.thumb_inner.winfo_children():
                widget.destroy()

            if not stl_files:
                ttk.Label(self.thumb_inner, text=self._t("preview.none_files")).pack(
                    anchor="w", padx=10, pady=10
                )
                self._set_status(self._t("preview.none_files_status"))
                self._clear_progress()
                self._clear_activity()
                return

            thumb_w, thumb_h = 320, 220
            columns = self._compute_thumb_columns()
            self.thumb_columns = columns
            for c in range(12):
                self.thumb_inner.columnconfigure(c, weight=0)
            for c in range(columns):
                self.thumb_inner.columnconfigure(c, weight=1)
            if update_progress and not self.render_running:
                self._set_progress(0, len(stl_files))
                self._set_activity(self._t("preview.building", done=0, total=len(stl_files)))

            def build_chunk(start_index: int):
                if token != self.thumb_job_token:
                    return
                end_index = min(start_index + 8, len(stl_files))
                for idx in range(start_index, end_index):
                    stl = stl_files[idx]
                    model_path = stl.resolve()
                    out_path = target_image_path(stl, self.source, self.index_dir, self.image_ext)
                    row = idx // columns
                    col = idx % columns
                    tile = ttk.Frame(self.thumb_inner, padding=(4, 4), relief="ridge")
                    tile.grid(row=row, column=col, sticky="n", padx=4, pady=(2, 4))
                    image_holder = ttk.Frame(tile, width=thumb_w, height=thumb_h)
                    image_holder.pack(anchor="n")
                    image_holder.pack_propagate(False)
                    self.thumb_items[model_path] = {
                        "holder": image_holder,
                        "out_path": out_path,
                        "thumb_w": thumb_w,
                        "thumb_h": thumb_h,
                    }
                    if self.render_running and model_path in self.render_inflight_paths:
                        self._update_thumbnail_for_model(model_path, in_progress=True)
                    else:
                        self._update_thumbnail_for_model(model_path, in_progress=False)

                    ttk.Label(tile, text=stl.name, wraplength=thumb_w, anchor="center").pack(
                        fill="x", pady=(3, 0)
                    )

                self.ui_queue.put(("thumb_progress", end_index, len(stl_files)))
                if end_index < len(stl_files):
                    self.root.after(1, lambda: build_chunk(end_index))
                else:
                    self.ui_queue.put(("thumb_done",))

            self.root.after(1, lambda: build_chunk(0))

        def _update_thumbnail_for_model(self, model_path: Path, in_progress: bool):
            item = self.thumb_items.get(model_path)
            if not item:
                return
            holder = item["holder"]
            out_path = item["out_path"]
            thumb_w = int(item["thumb_w"])
            thumb_h = int(item["thumb_h"])

            for child in holder.winfo_children():
                child.destroy()

            if in_progress:
                ttk.Label(holder, text=self._t("preview.generating")).place(relx=0.5, rely=0.5, anchor="center")
                return

            if out_path.exists():
                try:
                    image = tk.PhotoImage(file=str(out_path))
                    factor = max(
                        1,
                        math.ceil(image.width() / thumb_w),
                        math.ceil(image.height() / thumb_h),
                    )
                    thumb = image.subsample(factor, factor)
                    self.thumbnail_images[model_path] = thumb
                    self.thumbnail_cache.append(thumb)
                    lbl = ttk.Label(holder, image=thumb, anchor="center")
                    lbl.place(relx=0.5, rely=0.5, anchor="center")
                    self._bind_thumbnail_hover(lbl, model_path)
                    return
                except tk.TclError:
                    ttk.Label(holder, text=self._t("preview.unavailable")).place(
                        relx=0.5, rely=0.5, anchor="center"
                    )
                    return
            ttk.Label(holder, text=self._t("preview.no_image")).place(relx=0.5, rely=0.5, anchor="center")

        def start_background_render(self, scope: str, overwrite: bool):
            if self.render_running:
                messagebox.showinfo(self._t("dialog.info"), self._t("render.already_running"))
                return
            self.render_running = True
            self.render_cancel_event.clear()
            self.current_render_scope = scope
            self.current_render_overwrite = overwrite
            self.current_render_dir = self.selected_directory
            self.render_inflight_paths.clear()
            self._clear_progress()
            scope_text = self._t("render.scope.current") if scope == "current" else self._t("render.scope.all")
            self._set_status(self._t("render.preparing_status", scope=scope_text))
            self._set_activity(self._t("render.preparing_simple"))
            self._append_log(
                self._t(
                    "render.request_log",
                    scope=scope_text,
                    overwrite=overwrite,
                    renderer=self.renderer,
                    preset=self.blender_preset,
                    threads=self.render_threads,
                    margin=self.render_margin,
                    width=self.render_width,
                    height=self.render_height,
                    ext=self.image_ext,
                )
            )
            self._refresh_table_statuses()
            current_dir = self.selected_directory
            thread = threading.Thread(
                target=self._render_worker,
                args=(scope, current_dir, overwrite),
                daemon=True,
            )
            thread.start()

        def abort_background_render(self):
            if not self.render_running:
                messagebox.showinfo(self._t("dialog.info"), self._t("render.none_running"))
                return
            self.render_cancel_event.set()
            self._set_status(self._t("render.abort_requested"))
            self._set_activity(self._t("render.abort_activity"))

        def _render_worker(self, scope: str, current_dir: Path, overwrite: bool):
            try:
                effective_blender_path = self.blender_path
                if self.renderer == "blender":
                    blender_exe = detect_blender_executable(self.blender_path)
                    if blender_exe is None:
                        self.ui_queue.put(
                            ("render_error", self._t("render.blender_not_found"))
                        )
                        return
                    effective_blender_path = blender_exe
                    self.ui_queue.put(("log", self._t("render.blender_found", path=blender_exe)))

                if scope == "current":
                    stl_files = list(iter_render_sources_in_directory(current_dir))
                    scope_label = self._t("render.scope.current_with_dir", directory=current_dir)
                else:
                    stl_files = list(iter_render_sources(self.source, self.index_dir))
                    scope_label = self._t("render.scope.all_plain")
                tasks = []
                for idx, stl in enumerate(stl_files, start=1):
                    if self.render_cancel_event.is_set():
                        self.ui_queue.put(("render_cancelled", 0, 0, 0))
                        return
                    out_path = target_image_path(stl, self.source, self.index_dir, self.image_ext)
                    if needs_render(stl, out_path, overwrite=overwrite):
                        tasks.append((stl, out_path, not out_path.exists()))
                    if idx % 150 == 0:
                        self.ui_queue.put(("render_collecting", idx, len(tasks), scope_label))

                self.ui_queue.put(("render_started", len(tasks), scope_label))

                success_count = 0
                failed_count = 0
                total = len(tasks)
                if total == 0:
                    self.ui_queue.put(("render_done", 0, 0))
                    return

                max_workers = max(1, self.render_threads)
                self.ui_queue.put(("log", self._t("render.parallel_log", threads=max_workers)))

                def worker_task(stl: Path, out_path: Path, missing_preview: bool):
                    try:
                        render_stl(
                            stl,
                            out_path,
                            width=self.render_width,
                            height=self.render_height,
                            renderer=self.renderer,
                            blender_path=effective_blender_path,
                            blender_preset=self.blender_preset,
                            framing_margin=self.render_margin,
                        )
                        return True, missing_preview, stl.name, str(out_path), "", str(stl.resolve())
                    except Exception as exc:
                        return False, missing_preview, stl.name, str(out_path), str(exc), str(stl.resolve())

                processed = 0
                next_index = 0
                futures: dict[concurrent.futures.Future, int] = {}

                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    while next_index < total and len(futures) < max_workers:
                        stl, out_path, missing_preview = tasks[next_index]
                        self.ui_queue.put(("render_task_start", str(stl.resolve())))
                        fut = executor.submit(worker_task, stl, out_path, missing_preview)
                        futures[fut] = next_index
                        next_index += 1

                    while futures:
                        done, _ = concurrent.futures.wait(
                            list(futures.keys()),
                            timeout=0.2,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )

                        if self.render_cancel_event.is_set():
                            for fut in list(futures.keys()):
                                fut.cancel()

                        if not done:
                            continue

                        for fut in done:
                            futures.pop(fut, None)
                            if fut.cancelled():
                                continue
                            ok, missing_preview, filename, out_path_str, err_msg, model_path = fut.result()
                            if ok:
                                success_count += 1
                            else:
                                failed_count += 1
                            processed += 1
                            self.ui_queue.put(
                                ("render_progress", ok, missing_preview, filename, out_path_str, err_msg, model_path)
                            )

                        if not self.render_cancel_event.is_set():
                            while next_index < total and len(futures) < max_workers:
                                stl, out_path, missing_preview = tasks[next_index]
                                self.ui_queue.put(("render_task_start", str(stl.resolve())))
                                fut = executor.submit(worker_task, stl, out_path, missing_preview)
                                futures[fut] = next_index
                                next_index += 1

                remaining = total - processed
                if self.render_cancel_event.is_set() and remaining > 0:
                    self.ui_queue.put(("render_cancelled", success_count, failed_count, remaining))
                else:
                    self.ui_queue.put(("render_done", success_count, failed_count))
            except Exception as exc:
                self.ui_queue.put(("render_error", str(exc)))

        def change_start_dir(self):
            if self.render_running:
                messagebox.showinfo(
                    self._t("dialog.info"),
                    self._t("change_dir.blocked"),
                )
                return
            new_dir = filedialog.askdirectory(
                title=self._t("dialog.start_dir"), initialdir=str(self.source)
            )
            if not new_dir:
                return
            path = Path(new_dir)
            if not path.exists() or not path.is_dir():
                messagebox.showerror(self._t("dialog.error"), self._t("dialog.invalid_dir"))
                return

            self.source = path.resolve()
            self.selected_directory = self.source
            self.index_dir = self._default_index_for_source()
            save_last_start_dir(self.source)
            self._save_gui_config()
            self._set_path_text()
            self._start_initial_scan()

        def open_config_dialog(self):
            if self.render_running:
                messagebox.showinfo(
                    self._t("dialog.info"),
                    self._t("settings.blocked"),
                )
                return

            dialog = tk.Toplevel(self.root)
            dialog.title(self._t("settings.title"))
            dialog.transient(self.root)
            dialog.grab_set()
            dialog.resizable(False, False)

            frame = ttk.Frame(dialog, padding=12)
            frame.pack(fill="both", expand=True)

            index_var = tk.StringVar(value=str(self.index_dir))
            width_var = tk.StringVar(value=str(self.render_width))
            height_var = tk.StringVar(value=str(self.render_height))
            threads_var = tk.StringVar(value=str(self.render_threads))
            margin_var = tk.StringVar(value=f"{self.render_margin:.2f}")
            ext_var = tk.StringVar(value=self.image_ext)
            renderer_var = tk.StringVar(value=self.renderer)
            preset_var = tk.StringVar(value=self.blender_preset)
            blender_var = tk.StringVar(value=str(self.blender_path) if self.blender_path else "")
            bambu_var = tk.StringVar(value=str(self.bambu_studio_path) if self.bambu_studio_path else "")

            ttk.Label(frame, text=self._t("settings.language")).grid(row=0, column=0, sticky="w", pady=4)
            language_labels = {
                "de": self._t("settings.lang_de"),
                "en": self._t("settings.lang_en"),
            }
            language_values = [language_labels[code] for code in LANGUAGE_CHOICES]
            language_combo = ttk.Combobox(frame, values=language_values, state="readonly", width=18)
            language_combo.grid(row=1, column=0, sticky="w", pady=(0, 6))
            language_combo.set(language_labels.get(self.language, language_labels["de"]))

            ttk.Label(frame, text=self._t("settings.index_dir")).grid(row=2, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=index_var, width=56).grid(row=3, column=0, sticky="we", pady=(0, 6))

            def browse_index_dir():
                selected = filedialog.askdirectory(
                    title=self._t("settings.index_dir"), initialdir=str(self.source)
                )
                if selected:
                    index_var.set(selected)

            ttk.Button(frame, text=self._t("settings.select_dir"), command=browse_index_dir).grid(
                row=3, column=1, sticky="w", padx=(8, 0), pady=(0, 6)
            )

            ttk.Label(frame, text=self._t("settings.width")).grid(row=4, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=width_var, width=16).grid(row=5, column=0, sticky="w", pady=(0, 6))

            ttk.Label(frame, text=self._t("settings.height")).grid(row=6, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=height_var, width=16).grid(row=7, column=0, sticky="w", pady=(0, 6))

            ttk.Label(frame, text=self._t("settings.threads")).grid(row=8, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=threads_var, width=16).grid(
                row=9, column=0, sticky="w", pady=(0, 6)
            )

            ttk.Label(frame, text=self._t("settings.margin")).grid(row=10, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=margin_var, width=16).grid(
                row=11, column=0, sticky="w", pady=(0, 6)
            )

            ttk.Label(frame, text=self._t("settings.image_ext")).grid(row=12, column=0, sticky="w", pady=4)
            ttk.Combobox(
                frame, textvariable=ext_var, values=GUI_EXT_CHOICES, state="readonly", width=12
            ).grid(row=13, column=0, sticky="w", pady=(0, 10))

            ttk.Label(frame, text=self._t("settings.renderer")).grid(row=14, column=0, sticky="w", pady=4)
            ttk.Combobox(
                frame, textvariable=renderer_var, values=RENDERER_CHOICES, state="readonly", width=14
            ).grid(row=15, column=0, sticky="w", pady=(0, 6))

            ttk.Label(frame, text=self._t("settings.blender_preset")).grid(row=16, column=0, sticky="w", pady=4)
            ttk.Combobox(
                frame,
                textvariable=preset_var,
                values=BLENDER_PRESET_CHOICES,
                state="readonly",
                width=14,
            ).grid(row=17, column=0, sticky="w", pady=(0, 6))

            ttk.Separator(frame, orient="horizontal").grid(row=18, column=0, columnspan=2, sticky="we", pady=(8, 8))
            ttk.Label(frame, text=self._t("settings.program_paths"), style="SectionTitle.TLabel").grid(row=19, column=0, sticky="w", pady=(0, 4))
            ttk.Button(
                frame,
                text=self._t("settings.autodetect"),
                command=lambda: autodetect_program_paths(),
            ).grid(row=19, column=1, sticky="e", pady=(0, 4))

            ttk.Label(frame, text=self._t("settings.blender_path")).grid(row=20, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=blender_var, width=56).grid(
                row=21, column=0, sticky="we", pady=(0, 6)
            )

            def browse_blender_path():
                selected = filedialog.askopenfilename(
                    title=self._t("settings.select_blender_exe"),
                    filetypes=[(self._t("settings.filetype_exe"), "*.exe"), (self._t("settings.filetype_all"), "*.*")],
                )
                if selected:
                    blender_var.set(selected)

            ttk.Button(frame, text=self._t("settings.select_file"), command=browse_blender_path).grid(
                row=21, column=1, sticky="w", padx=(8, 0), pady=(0, 6)
            )

            ttk.Label(frame, text=self._t("settings.bambu_path")).grid(row=22, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=bambu_var, width=56).grid(
                row=23, column=0, sticky="we", pady=(0, 6)
            )

            def browse_bambu_path():
                selected = filedialog.askopenfilename(
                    title=self._t("settings.select_bambu_exe"),
                    filetypes=[(self._t("settings.filetype_exe"), "*.exe"), (self._t("settings.filetype_all"), "*.*")],
                )
                if selected:
                    bambu_var.set(selected)

            ttk.Button(frame, text=self._t("settings.select_file"), command=browse_bambu_path).grid(
                row=23, column=1, sticky="w", padx=(8, 0), pady=(0, 6)
            )

            def autodetect_program_paths():
                blender_exe = detect_blender_executable(self.blender_path)
                bambu_exe = self._detect_bambu_studio_executable()
                if blender_exe:
                    blender_var.set(str(blender_exe))
                if bambu_exe:
                    bambu_var.set(str(bambu_exe))
                found_bits = []
                if blender_exe:
                    found_bits.append("Blender")
                if bambu_exe:
                    found_bits.append("Bambu Studio")
                if found_bits:
                    self._append_log(self._t("settings.autodetect_found", items=", ".join(found_bits)))
                else:
                    self._append_log(self._t("settings.autodetect_none"))

            button_row = ttk.Frame(frame)
            button_row.grid(row=24, column=0, columnspan=2, sticky="e")

            def apply_settings():
                try:
                    width = int(width_var.get().strip())
                    height = int(height_var.get().strip())
                    threads = int(threads_var.get().strip())
                    margin = float(margin_var.get().strip().replace(",", "."))
                except ValueError:
                    messagebox.showerror(self._t("dialog.error"), self._t("settings.err_numeric"))
                    return
                if width <= 0 or height <= 0:
                    messagebox.showerror(self._t("dialog.error"), self._t("settings.err_dimensions"))
                    return
                if threads <= 0:
                    messagebox.showerror(self._t("dialog.error"), self._t("settings.err_threads"))
                    return
                if margin < 0.0 or margin > 1.0:
                    messagebox.showerror(self._t("dialog.error"), self._t("settings.err_margin"))
                    return
                ext = ext_var.get().strip().lower()
                if ext not in GUI_EXT_CHOICES:
                    messagebox.showerror(self._t("dialog.error"), self._t("settings.err_image_ext"))
                    return
                renderer = renderer_var.get().strip().lower()
                if renderer not in RENDERER_CHOICES:
                    messagebox.showerror(self._t("dialog.error"), self._t("settings.err_renderer"))
                    return
                blender_preset = preset_var.get().strip().lower()
                if blender_preset not in BLENDER_PRESET_CHOICES:
                    messagebox.showerror(self._t("dialog.error"), self._t("settings.err_preset"))
                    return
                blender_path = self._resolve_optional_path(blender_var.get())
                bambu_path = self._resolve_optional_path(bambu_var.get())
                selected_lang_label = language_combo.get().strip()
                selected_lang = "de"
                for code, label in language_labels.items():
                    if label == selected_lang_label:
                        selected_lang = code
                        break

                self.render_width = width
                self.render_height = height
                self.render_threads = threads
                self.render_margin = margin
                self.image_ext = ext
                self.renderer = renderer
                self.blender_preset = blender_preset
                self.language = normalize_language(selected_lang)
                self.blender_path = blender_path
                self.bambu_studio_path = bambu_path
                self.index_dir = self._resolve_index_dir(index_var.get(), self._default_index_for_source())
                self._save_gui_config()
                self._apply_language_to_ui()
                self._set_path_text()
                self._set_status(self._t("settings.saved"))
                dialog.destroy()
                self._start_initial_scan()

            ttk.Button(button_row, text=self._t("settings.cancel"), command=dialog.destroy).pack(
                side="right", padx=(8, 0)
            )
            ttk.Button(button_row, text=self._t("settings.save"), command=apply_settings).pack(side="right")

            dialog.wait_window(dialog)

        def delete_index_directory(self):
            if self.render_running:
                messagebox.showinfo(
                    self._t("dialog.info"),
                    self._t("delete_index.blocked"),
                )
                return

            if not self.index_dir.exists():
                messagebox.showinfo(self._t("dialog.info"), self._t("delete_index.not_exists", path=self.index_dir))
                self._append_log(self._t("delete_index.skip_log", path=self.index_dir))
                return

            confirm = messagebox.askyesno(
                self._t("delete_index.confirm_title"),
                self._t("delete_index.confirm_text", path=self.index_dir),
                icon="warning",
            )
            if not confirm:
                self._append_log(self._t("delete_index.cancel_log"))
                return

            try:
                shutil.rmtree(self.index_dir)
                self.summary.images_available = 0
                self.summary.images_to_generate = self.summary.total_models
                self._set_summary_text()
                self._set_status(self._t("delete_index.done_status"))
                self._set_activity("")
                self._refresh_current_view()
                self._append_log(self._t("delete_index.done_log", path=self.index_dir))
                self._start_initial_scan()
            except Exception as exc:
                messagebox.showerror(self._t("dialog.error"), self._t("delete_index.failed", error=exc))
                self._set_status(self._t("delete_index.failed_status"))
                self._append_log(self._t("delete_index.failed_log", error=exc))

        def on_close(self):
            print(self._t("close.terminating"), flush=True)
            if self.hover_preview_win is not None and self.hover_preview_win.winfo_exists():
                self.hover_preview_win.destroy()
            save_last_start_dir(self.source)
            self._save_gui_config()
            self.root.destroy()

    root = tk.Tk()
    root.withdraw()
    boot_cfg = load_app_config()
    boot_gui_cfg = boot_cfg.get("gui", {}) if isinstance(boot_cfg.get("gui"), dict) else {}
    boot_lang = normalize_language(boot_gui_cfg.get("language", detect_default_language()))

    start_dir = load_last_start_dir()
    if start_dir is None:
        selected = filedialog.askdirectory(title=tr(boot_lang, "dialog.start_dir"))
        if not selected:
            root.destroy()
            return 0
        start_dir = Path(selected)

    if not start_dir.exists() or not start_dir.is_dir():
        messagebox.showerror(
            tr(boot_lang, "dialog.error"),
            tr(boot_lang, "dialog.start_dir_not_exists", path=start_dir),
        )
        root.destroy()
        return 2

    save_last_start_dir(start_dir)
    root.deiconify()
    STLPreviewApp(root, start_dir)
    root.mainloop()
    return 0
