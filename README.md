# STL Preview Index Renderer (Python)

A tool with two modes:

- GUI mode (no arguments): directory browser with model list (`.stl` / `.blend`) and thumbnail preview
- CLI mode (with arguments): renders preview images recursively into an index directory

## Screenshot

<img src="screenshot.png" alt="STL Preview Screenshot" width="1400" />

## Project Structure

- `stl_index_renderer.py`: entry point (CLI + GUI start)
- `gui_app.py`: compatibility wrapper for GUI start
- `gui/app.py`: slim GUI entry point
- `gui/window.py`: main Tkinter UI logic
- `gui/models.py`: GUI data classes
- `gui/utils.py`: GUI helper functions
- `renderers.py`: Blender/PyVista/Matplotlib renderers
- `scanner.py`: model scan, summary, path logic
- `config_store.py`: load/save configuration
- `constants.py`: shared constants

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pyvista matplotlib numpy-stl
```

Notes:
- Default renderer is `blender`.
- Alternatives are `pyvista` and `matplotlib` (via settings or CLI).
- Blender detection order:
  - configured Blender path in GUI settings, or
  - `blender` in `PATH`, or
  - common Windows installation paths.

## GUI Mode

Start without arguments:

```bash
python3 stl_index_renderer.py
```

Behavior:

- First start asks for a start directory.
- Later starts use the last directory automatically.
- Default index directory is `Index` inside the start directory.
- Initial scan runs in background with status/busy feedback.
- Scan result is cached in the index directory (`.stlpreview_scan_cache.json`).
- Auto re-scan runs only if cache is missing or older than 3 days.
- `Rescan` always forces a fresh scan.
- `File -> Settings...` lets you configure index path, resolution and output format.
- `File -> Settings...` also supports renderer selection and optional Blender path.
- `File -> Settings...` also supports Blender look presets (`neutral`, `kontrast`, `dunkelblau`).
- `File -> Settings...` also supports render thread count (default `4`).
- `File -> Settings...` also supports framing margin (`0.00` to `1.00`).
- Header shows:
  - STL file count
  - Blender files without matching STL
  - total renderable models
  - existing images
  - images to generate
- Separate activity line with progress bar.
- Footer activity log shows ongoing steps and errors.
- Left: folder navigation below start directory.
- Right:
  - model list (`.stl` and `.blend`; name, size, date)
  - thumbnail preview from index directory
- Search field in toolbar:
  - searches across all directories
  - uses fuzzy matching when no exact match exists
  - model list is filtered to matches
- `File` menu:
  - `Change start directory...`
  - `Settings...`
  - `Rescan`
  - `Delete index` (deletes configured index directory after confirmation)
- `Render` menu:
  - `Start (whole start directory)`
  - `Start (current directory)`
  - `Abort`

## CLI Mode

Example:

```bash
python3 stl_index_renderer.py \
  --source . \
  --index-dir ./index \
  --width 500 \
  --height 300 \
  --ext .png \
  --verbose
```

Options:

- `--source`: source directory (default: current directory)
- `--index-dir`: target image directory (default: `./index`)
- `--width`: output width in px (default: `500`)
- `--height`: output height in px (default: `300`)
- `--ext`: output format (`.png`, `.jpg`, `.jpeg`, `.webp`)
- `--renderer`: `blender`, `pyvista`, `matplotlib`
- `--blender-path`: optional path to local Blender executable
- `--blender-preset`: `neutral`, `kontrast`, `dunkelblau`
- `--framing-margin`: extra margin around object (`0.0` to `1.0`, default `0.18`)
- GUI rendering uses parallel workers with configurable thread count (default `4`).
- `--overwrite`: force re-rendering of all images
- `--verbose`: print every processed file

## Note

- For each model basename (`folder + filename without extension`), `*.stl` is preferred.
- If no STL exists, `*.blend` is rendered.

## Donations

If this project helps you and you want to support it, donations are welcome:

- Contact: `herrler@buschtrommel.net`
