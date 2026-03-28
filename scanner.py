from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ScanSummary:
    stl_count: int = 0
    blend_only_count: int = 0
    total_models: int = 0
    images_available: int = 0
    images_to_generate: int = 0


def _iter_candidate_model_files(source: Path, index_dir: Path):
    source = source.resolve()
    index_dir = index_dir.resolve()

    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".stl", ".blend"):
            continue
        try:
            path.relative_to(index_dir)
            continue
        except ValueError:
            pass
        yield path


def iter_stl_files(source: Path, index_dir: Path):
    # Backward-compatible helper used by old call sites.
    for path in _iter_candidate_model_files(source, index_dir):
        if path.suffix.lower() == ".stl":
            yield path


def iter_render_sources(source: Path, index_dir: Path):
    source = source.resolve()
    grouped: dict[tuple[Path, str], dict[str, Path]] = {}
    for path in _iter_candidate_model_files(source, index_dir):
        rel = path.resolve().relative_to(source).with_suffix("")
        key = (rel.parent, rel.name.lower())
        bucket = grouped.setdefault(key, {})
        bucket[path.suffix.lower()] = path

    # STL has priority; only count BLEND if no matching STL exists.
    for _, bucket in sorted(grouped.items(), key=lambda item: str(item[0][0] / item[0][1])):
        if ".stl" in bucket:
            yield bucket[".stl"]
        elif ".blend" in bucket:
            yield bucket[".blend"]


def list_display_files(directory: Path):
    return sorted(
        [
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in (".stl", ".blend")
        ],
        key=lambda p: p.name.lower(),
    )


def iter_render_sources_in_directory(directory: Path):
    grouped: dict[str, dict[str, Path]] = {}
    for path in list_display_files(directory):
        bucket = grouped.setdefault(path.stem.lower(), {})
        bucket[path.suffix.lower()] = path
    for stem, bucket in sorted(grouped.items()):
        if ".stl" in bucket:
            yield bucket[".stl"]
        elif ".blend" in bucket:
            yield bucket[".blend"]


def target_image_path(stl_path: Path, source: Path, index_dir: Path, ext: str) -> Path:
    rel = stl_path.resolve().relative_to(source.resolve())
    return (index_dir / rel).with_suffix(ext)


def needs_render(stl_path: Path, out_path: Path, overwrite: bool) -> bool:
    if overwrite or not out_path.exists():
        return True
    return stl_path.stat().st_mtime > out_path.stat().st_mtime


def scan_summary(source: Path, index_dir: Path, ext: str) -> ScanSummary:
    summary = ScanSummary()
    # Global STL count (all STL files)
    for path in _iter_candidate_model_files(source, index_dir):
        if path.suffix.lower() == ".stl":
            summary.stl_count += 1

    # Renderable model count and blend-only count (deduplicated by stem)
    render_sources = list(iter_render_sources(source, index_dir))
    for path in render_sources:
        if path.suffix.lower() == ".blend":
            summary.blend_only_count += 1
    summary.total_models = summary.stl_count + summary.blend_only_count

    for src_path in render_sources:
        out_path = target_image_path(src_path, source, index_dir, ext)
        if out_path.exists():
            summary.images_available += 1
        if needs_render(src_path, out_path, overwrite=False):
            summary.images_to_generate += 1
    return summary


def collect_directories(source: Path, index_dir: Path) -> list[Path]:
    source = source.resolve()
    index_dir = index_dir.resolve()
    directories = [source]
    for root, dirnames, _ in os.walk(source):
        root_path = Path(root).resolve()
        allowed: list[str] = []
        for dirname in dirnames:
            child = (root_path / dirname).resolve()
            try:
                child.relative_to(index_dir)
                continue
            except ValueError:
                pass
            allowed.append(dirname)
            directories.append(child)
        dirnames[:] = allowed
    directories.sort(key=lambda p: str(p).lower())
    return directories
