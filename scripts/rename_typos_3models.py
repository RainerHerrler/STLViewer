from __future__ import annotations

import argparse
from pathlib import Path


REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("Fahhard", "Fahrrad"),
    ("Abeckung", "Abdeckung"),
    ("unterleil", "unterteil"),
    ("Pzzle", "Puzzle"),
    ("Briullen", "Brillen"),
    ("hörrer", "hörer"),
    ("BadSchlänkchen", "BadSchränkchen"),
    ("Breadboarwire", "Breadboardwire"),
    ("Wheelchar", "Wheelchair"),
    ("nurufen", "nurofen"),
    ("Sonenschirm", "Sonnenschirm"),
)


def renamed_name(name: str) -> str:
    new_name = name
    for old, new in REPLACEMENTS:
        new_name = new_name.replace(old, new)
    return new_name


def collect_ops(root: Path) -> list[tuple[Path, Path]]:
    ops: list[tuple[Path, Path]] = []
    all_paths = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for src in all_paths:
        new_name = renamed_name(src.name)
        if new_name == src.name:
            continue
        dst = src.with_name(new_name)
        ops.append((src, dst))
    return ops


def main() -> int:
    parser = argparse.ArgumentParser(description="Rename obvious typo names in a 3models tree.")
    parser.add_argument("root", type=Path, help="Root directory to process, e.g. /mnt/c/Temp/3models")
    parser.add_argument("--apply", action="store_true", help="Execute renames. Without this flag only preview.")
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.exists() or not root.is_dir():
        print(f"ERROR: Invalid root directory: {root}")
        return 2

    ops = collect_ops(root)
    if not ops:
        print("No rename candidates found.")
        return 0

    print(f"Found {len(ops)} rename candidates under {root}")
    for src, dst in ops:
        print(f"{src} -> {dst}")

    if not args.apply:
        print("Preview only. Re-run with --apply to execute.")
        return 0

    done = 0
    skipped = 0
    for src, dst in ops:
        if not src.exists():
            skipped += 1
            print(f"SKIP (missing): {src}")
            continue
        if dst.exists():
            skipped += 1
            print(f"SKIP (target exists): {dst}")
            continue
        src.rename(dst)
        done += 1
        print(f"OK: {src} -> {dst}")

    print(f"Finished. Renamed={done}, Skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
