#!/usr/bin/env python3
"""STL preview tool: CLI renderer and GUI launcher."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from constants import BLENDER_PRESET_CHOICES, GUI_EXT_CHOICES, RENDERER_CHOICES
from gui_app import launch_gui
from renderers import render_stl
from scanner import iter_render_sources, needs_render, target_image_path


@dataclass
class Stats:
    scanned: int = 0
    rendered: int = 0
    skipped: int = 0
    failed: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render preview images for STL files into an index directory"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("."),
        help="Quellverzeichnis für STL-Dateien (Default: aktuelles Verzeichnis)",
    )
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=Path("index"),
        help="Zielverzeichnis für gerenderte Bilder (Default: ./index)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=500,
        help="Bildbreite in Pixel (Default: 500)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=300,
        help="Bildhöhe in Pixel (Default: 300)",
    )
    parser.add_argument(
        "--ext",
        default=".png",
        choices=GUI_EXT_CHOICES,
        help="Zielformat der Vorschaubilder (Default: .png)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Erzwingt Neurendern aller Bilder, ignoriert Zeitstempel",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Ausführliche Ausgabe",
    )
    parser.add_argument(
        "--renderer",
        default="blender",
        choices=RENDERER_CHOICES,
        help="Renderer wählen: blender|pyvista|matplotlib (Default: blender)",
    )
    parser.add_argument(
        "--blender-path",
        type=Path,
        default=None,
        help="Optionaler Pfad zur blender-Executable",
    )
    parser.add_argument(
        "--blender-preset",
        default="kontrast",
        choices=BLENDER_PRESET_CHOICES,
        help="Blender-Look-Preset: neutral|kontrast|dunkelblau (Default: kontrast)",
    )
    parser.add_argument(
        "--framing-margin",
        type=float,
        default=0.18,
        help="Zusätzlicher Bildrand um das Objekt (0.0 bis 1.0, Default: 0.18)",
    )
    return parser.parse_args()


def run_cli(args: argparse.Namespace) -> int:
    if args.width <= 0 or args.height <= 0:
        print("Fehler: --width und --height müssen > 0 sein.", file=sys.stderr)
        return 2

    source = args.source.resolve()
    index_dir = args.index_dir.resolve()

    if not source.exists() or not source.is_dir():
        print(f"Fehler: Quellverzeichnis nicht gefunden: {source}", file=sys.stderr)
        return 2

    stats = Stats()

    for stl_path in iter_render_sources(source, index_dir):
        stats.scanned += 1
        out_path = target_image_path(stl_path, source, index_dir, args.ext)

        if not needs_render(stl_path, out_path, args.overwrite):
            stats.skipped += 1
            if args.verbose:
                print(f"SKIP   {stl_path} -> {out_path}")
            continue

        try:
            render_stl(
                stl_path,
                out_path,
                args.width,
                args.height,
                renderer=args.renderer,
                blender_path=args.blender_path,
                blender_preset=args.blender_preset,
                framing_margin=args.framing_margin,
            )
            stats.rendered += 1
            if args.verbose:
                print(f"OK     {stl_path} -> {out_path}")
        except Exception as exc:
            stats.failed += 1
            print(f"FEHLER {stl_path}: {exc}", file=sys.stderr)

    print(
        f"Fertig. STL gefunden: {stats.scanned}, gerendert: {stats.rendered}, "
        f"übersprungen: {stats.skipped}, Fehler: {stats.failed}"
    )

    return 1 if stats.failed else 0


def main() -> int:
    if len(sys.argv) == 1:
        return launch_gui()
    return run_cli(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
