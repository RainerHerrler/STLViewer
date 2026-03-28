from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

from constants import BLENDER_PRESET_CHOICES, RENDERER_CHOICES


def get_blender_preset_values(preset: str) -> dict[str, float]:
    values = {
        "neutral": {
            "bg_r": 0.78,
            "bg_g": 0.80,
            "bg_b": 0.84,
            "bg_strength": 0.9,
            "mat_r": 0.24,
            "mat_g": 0.41,
            "mat_b": 0.80,
            "roughness": 0.46,
            "key_energy": 2.6,
            "fill_energy": 0.9,
            "exposure": -0.05,
            "look": "None",
        },
        "kontrast": {
            "bg_r": 0.72,
            "bg_g": 0.74,
            "bg_b": 0.78,
            "bg_strength": 0.8,
            "mat_r": 0.12,
            "mat_g": 0.29,
            "mat_b": 0.75,
            "roughness": 0.42,
            "key_energy": 2.4,
            "fill_energy": 0.7,
            "exposure": -0.15,
            "look": "Medium High Contrast",
        },
        "dunkelblau": {
            "bg_r": 0.66,
            "bg_g": 0.68,
            "bg_b": 0.72,
            "bg_strength": 0.7,
            "mat_r": 0.06,
            "mat_g": 0.18,
            "mat_b": 0.60,
            "roughness": 0.38,
            "key_energy": 2.2,
            "fill_energy": 0.55,
            "exposure": -0.2,
            "look": "High Contrast",
        },
    }
    if preset not in BLENDER_PRESET_CHOICES:
        raise ValueError(f"Ungültiges Blender-Preset: {preset}")
    return values[preset]


def set_equal_3d_axes(ax, mins, maxs):
    center = (mins + maxs) / 2.0
    radius = (maxs - mins).max() / 2.0
    if radius == 0:
        radius = 1.0
    ax.set_xlim(center[0] - radius, center[0] + radius)
    ax.set_ylim(center[1] - radius, center[1] + radius)
    ax.set_zlim(center[2] - radius, center[2] + radius)


def detect_blender_executable(configured_path: Path | str | None = None) -> Path | None:
    if configured_path:
        candidate = Path(configured_path).expanduser().resolve()
        if candidate.exists() and candidate.is_file():
            return candidate

    found = shutil.which("blender")
    if found:
        return Path(found).resolve()

    for base in [
        Path("C:/Program Files/Blender Foundation"),
        Path("C:/Program Files (x86)/Blender Foundation"),
    ]:
        if not base.exists():
            continue
        for exe in sorted(base.glob("Blender */blender.exe"), reverse=True):
            if exe.exists():
                return exe.resolve()
    return None


def render_stl(
    stl_path: Path,
    out_path: Path,
    width: int,
    height: int,
    renderer: str = "blender",
    blender_path: Path | str | None = None,
    blender_preset: str = "kontrast",
    framing_margin: float = 0.18,
):
    source_ext = stl_path.suffix.lower()
    if source_ext not in (".stl", ".blend"):
        raise ValueError(f"Nicht unterstützter Quelltyp: {source_ext}")

    requested = renderer.lower()
    if requested not in RENDERER_CHOICES:
        raise ValueError(f"Ungültiger Renderer: {renderer}")

    if requested == "blender":
        blender_exe = detect_blender_executable(blender_path)
        if blender_exe:
            if source_ext == ".blend":
                render_blend_blender(
                    stl_path,
                    out_path,
                    width,
                    height,
                    blender_exe,
                    blender_preset,
                    framing_margin,
                )
            else:
                render_stl_blender(
                    stl_path,
                    out_path,
                    width,
                    height,
                    blender_exe,
                    blender_preset,
                    framing_margin,
                )
            _verify_output(out_path)
            return
        raise RuntimeError("Blender nicht gefunden.")

    if source_ext == ".blend":
        raise RuntimeError("BLEND-Dateien können nur mit Renderer 'blender' verarbeitet werden.")

    if requested == "pyvista":
        render_stl_pyvista(stl_path, out_path, width, height, framing_margin)
        _verify_output(out_path)
        return

    render_stl_matplotlib(stl_path, out_path, width, height, framing_margin)
    _verify_output(out_path)


def _verify_output(out_path: Path):
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"Keine Ausgabedatei erzeugt: {out_path}")


def _collect_blender_output_candidates(out_path: Path, started_at: float) -> list[Path]:
    parent = out_path.parent
    stem = out_path.stem
    suffix = out_path.suffix.lower()
    candidates: list[Path] = []
    for p in parent.glob(f"{stem}*"):
        if not p.is_file():
            continue
        name_l = p.name.lower()
        if suffix and not (name_l.endswith(suffix) or name_l.endswith(suffix + suffix)):
            continue
        if p.stat().st_mtime < started_at - 2.0:
            continue
        candidates.append(p)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates


def _finalize_blender_output(out_path: Path, started_at: float, detail: str = ""):
    if out_path.exists() and out_path.stat().st_size > 0:
        return
    candidates = _collect_blender_output_candidates(out_path, started_at)
    if not candidates:
        suffix = f" | Blender-Ausgabe: {detail}" if detail else ""
        raise RuntimeError(f"Blender hat keine Ausgabedatei erzeugt: {out_path}{suffix}")
    best = candidates[0]
    if best.resolve() != out_path.resolve():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(best), str(out_path))
    _verify_output(out_path)


def render_stl_blender(
    stl_path: Path,
    out_path: Path,
    width: int,
    height: int,
    blender_exe: Path,
    blender_preset: str,
    framing_margin: float,
):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = out_path.suffix.lower()
    blender_format_map = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
    }
    file_format = blender_format_map.get(suffix)
    if file_format is None:
        raise ValueError(f"Blender unterstützt dieses Dateiformat nicht: {suffix}")
    preset = get_blender_preset_values(blender_preset)
    margin = max(0.0, min(1.0, float(framing_margin)))

    script_content = textwrap.dedent(
        """
        import addon_utils
        import bpy
        import mathutils
        from pathlib import Path
        import sys
        from math import radians

        argv = sys.argv
        if "--" not in argv:
            raise SystemExit("missing args")
        args = argv[argv.index("--") + 1:]
        (
            stl_path,
            out_path,
            width,
            height,
            file_format,
            bg_r,
            bg_g,
            bg_b,
            bg_strength,
            mat_r,
            mat_g,
            mat_b,
            roughness,
            key_energy,
            fill_energy,
            exposure,
            look,
            margin,
        ) = args[0], args[1], int(args[2]), int(args[3]), args[4], float(args[5]), float(args[6]), float(args[7]), float(args[8]), float(args[9]), float(args[10]), float(args[11]), float(args[12]), float(args[13]), float(args[14]), float(args[15]), args[16], float(args[17])
        out_path = str(Path(out_path))

        def fit_camera_to_points(cam_obj, cam_data, points, margin):
            if not points:
                return
            fit = 1.0 + margin
            cam_data.type = "ORTHO"
            cam_data.shift_x = 0.0
            cam_data.shift_y = 0.0
            aspect = max(1e-6, float(width) / max(1.0, float(height)))
            # Build camera matrix directly from location/rotation (no view_layer.update needed)
            rot_mat = cam_obj.rotation_euler.to_matrix().to_4x4()
            cam_quat = cam_obj.rotation_euler.to_quaternion()
            inv = (mathutils.Matrix.Translation(cam_obj.location) @ rot_mat).inverted()
            cam_pts = [inv @ p for p in points]

            # Push camera back if objects are too close or behind the camera
            max_z = max(p.z for p in cam_pts)
            if max_z > -0.05:
                shift_z = max_z + 0.5
                cam_obj.location += cam_quat @ mathutils.Vector((0.0, 0.0, shift_z))
                inv = (mathutils.Matrix.Translation(cam_obj.location) @ rot_mat).inverted()
                cam_pts = [inv @ p for p in points]

            min_x = min(p.x for p in cam_pts)
            max_x = max(p.x for p in cam_pts)
            min_y = min(p.y for p in cam_pts)
            max_y = max(p.y for p in cam_pts)
            min_z = min(p.z for p in cam_pts)
            max_z = max(p.z for p in cam_pts)
            cx = (min_x + max_x) * 0.5
            cy = (min_y + max_y) * 0.5
            span_x = max(1e-6, max_x - min_x)
            span_y = max(1e-6, max_y - min_y)

            if aspect >= 1.0:
                required_ortho = max(span_x, span_y * aspect)
            else:
                required_ortho = max(span_x / aspect, span_y)
            S = max(0.01, required_ortho * fit * 1.04)
            cam_data.ortho_scale = S

            # Use shift_x/shift_y to center the view — more reliable than moving the camera
            # Blender shift is in proportion of ortho_scale (1.0 = one full canvas width)
            cam_data.shift_x = cx / S
            cam_data.shift_y = cy / S

            depth = abs(min_z) + abs(max_z) + 1.0
            cam_data.clip_start = max(0.001, depth / 100000.0)
            cam_data.clip_end = max(1000.0, depth * 20.0)
            bpy.context.view_layer.update()

        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
        scene.render.engine = "BLENDER_EEVEE"
        scene.render.image_settings.file_format = file_format
        scene.render.use_file_extension = False
        scene.render.use_overwrite = True
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        scene.render.pixel_aspect_x = 1.0
        scene.render.pixel_aspect_y = 1.0
        scene.render.use_border = False
        scene.render.use_crop_to_border = False
        scene.render.filepath = out_path
        scene.render.film_transparent = False
        scene.view_settings.view_transform = "Filmic"
        scene.view_settings.look = look
        scene.view_settings.exposure = exposure

        if scene.world is None:
            scene.world = bpy.data.worlds.new("PreviewWorld")
        scene.world.use_nodes = True
        bg = scene.world.node_tree.nodes.get("Background")
        if bg is not None:
            bg.inputs["Color"].default_value = (bg_r, bg_g, bg_b, 1.0)
            bg.inputs["Strength"].default_value = bg_strength

        addon_utils.enable("io_mesh_stl", default_set=False, persistent=False)
        imported = None
        if hasattr(bpy.ops.wm, "stl_import"):
            imported = bpy.ops.wm.stl_import(filepath=stl_path)
        elif hasattr(bpy.ops.import_mesh, "stl"):
            imported = bpy.ops.import_mesh.stl(filepath=stl_path)
        if imported is None or "FINISHED" not in imported:
            raise RuntimeError("STL-Import in Blender fehlgeschlagen.")

        if not bpy.context.selected_objects:
            raise RuntimeError("Nach STL-Import wurde kein Objekt selektiert.")
        obj = bpy.context.selected_objects[0]
        bpy.context.view_layer.objects.active = obj

        mat = bpy.data.materials.new(name="PreviewMaterial")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        bsdf.inputs["Base Color"].default_value = (mat_r, mat_g, mat_b, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
        if "Specular IOR Level" in bsdf.inputs:
            bsdf.inputs["Specular IOR Level"].default_value = 0.35
        elif "Specular" in bsdf.inputs:
            bsdf.inputs["Specular"].default_value = 0.35
        obj.data.materials.append(mat)

        world_pts = [obj.matrix_world @ v.co for v in obj.data.vertices]
        if not world_pts:
            raise RuntimeError("Keine Geometriepunkte gefunden.")
        min_x = min(v.x for v in world_pts); max_x = max(v.x for v in world_pts)
        min_y = min(v.y for v in world_pts); max_y = max(v.y for v in world_pts)
        min_z = min(v.z for v in world_pts); max_z = max(v.z for v in world_pts)
        center = mathutils.Vector(((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5))
        radius = max(max_x - min_x, max_y - min_y, max_z - min_z) * 0.5 + 1e-6

        cam_data = bpy.data.cameras.new("PreviewCamera")
        cam_obj = bpy.data.objects.new("PreviewCamera", cam_data)
        scene.collection.objects.link(cam_obj)
        scene.camera = cam_obj
        direction = mathutils.Vector((1.0, -1.0, 0.75)).normalized()
        cam_obj.location = center + direction * max(1.0, radius * 4.0)
        cam_obj.rotation_euler = (center - cam_obj.location).to_track_quat("-Z", "Y").to_euler()

        fit_camera_to_points(cam_obj, cam_data, world_pts, margin)

        key_light = bpy.data.lights.new(name="Key", type='SUN')
        key_light.energy = key_energy
        key_obj = bpy.data.objects.new(name="Key", object_data=key_light)
        scene.collection.objects.link(key_obj)
        key_obj.rotation_euler = (radians(50), radians(10), radians(40))

        fill_light = bpy.data.lights.new(name="Fill", type='SUN')
        fill_light.energy = fill_energy
        fill_obj = bpy.data.objects.new(name="Fill", object_data=fill_light)
        scene.collection.objects.link(fill_obj)
        fill_obj.rotation_euler = (radians(45), radians(-20), radians(-120))

        result_op = bpy.ops.render.render(write_still=True)
        if "FINISHED" not in result_op:
            raise RuntimeError(f"Render-Operator abgebrochen: {result_op}")
        """
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as script_file:
        script_file.write(script_content)
        script_path = Path(script_file.name)

    cmd = [
        str(blender_exe),
        "--background",
        "--factory-startup",
        "--python",
        str(script_path),
        "--",
        str(stl_path),
        str(out_path),
        str(width),
        str(height),
        file_format,
        str(preset["bg_r"]),
        str(preset["bg_g"]),
        str(preset["bg_b"]),
        str(preset["bg_strength"]),
        str(preset["mat_r"]),
        str(preset["mat_g"]),
        str(preset["mat_b"]),
        str(preset["roughness"]),
        str(preset["key_energy"]),
        str(preset["fill_energy"]),
        str(preset["exposure"]),
        str(preset["look"]),
        str(margin),
    ]
    started_at = time.time()
    detail = ""
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        std_tail = (completed.stdout or "").strip().splitlines()[-4:]
        err_tail = (completed.stderr or "").strip().splitlines()[-4:]
        detail = " || ".join(std_tail + err_tail)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        raise RuntimeError(f"Blender-Rendering fehlgeschlagen: {detail}") from exc
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass

    _finalize_blender_output(out_path, started_at, detail)


def render_blend_blender(
    blend_path: Path,
    out_path: Path,
    width: int,
    height: int,
    blender_exe: Path,
    blender_preset: str,
    framing_margin: float,
):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = out_path.suffix.lower()
    blender_format_map = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
    }
    file_format = blender_format_map.get(suffix)
    if file_format is None:
        raise ValueError(f"Blender unterstützt dieses Dateiformat nicht: {suffix}")
    preset = get_blender_preset_values(blender_preset)
    margin = max(0.0, min(1.0, float(framing_margin)))

    script_content = textwrap.dedent(
        """
        import bpy
        import mathutils
        from pathlib import Path
        import sys
        from math import radians

        argv = sys.argv
        if "--" not in argv:
            raise SystemExit("missing args")
        args = argv[argv.index("--") + 1:]
        (
            out_path,
            width,
            height,
            file_format,
            bg_r,
            bg_g,
            bg_b,
            bg_strength,
            key_energy,
            fill_energy,
            exposure,
            look,
            margin,
        ) = args[0], int(args[1]), int(args[2]), args[3], float(args[4]), float(args[5]), float(args[6]), float(args[7]), float(args[8]), float(args[9]), float(args[10]), args[11], float(args[12])
        out_path = str(Path(out_path))

        def fit_camera_to_points(cam_obj, cam_data, points, margin):
            if not points:
                return
            fit = 1.0 + margin
            cam_data.type = "ORTHO"
            cam_data.shift_x = 0.0
            cam_data.shift_y = 0.0
            aspect = max(1e-6, float(width) / max(1.0, float(height)))
            # Build camera matrix directly from location/rotation (no view_layer.update needed)
            rot_mat = cam_obj.rotation_euler.to_matrix().to_4x4()
            cam_quat = cam_obj.rotation_euler.to_quaternion()
            inv = (mathutils.Matrix.Translation(cam_obj.location) @ rot_mat).inverted()
            cam_pts = [inv @ p for p in points]

            # Push camera back if objects are too close or behind the camera
            max_z = max(p.z for p in cam_pts)
            if max_z > -0.05:
                shift_z = max_z + 0.5
                cam_obj.location += cam_quat @ mathutils.Vector((0.0, 0.0, shift_z))
                inv = (mathutils.Matrix.Translation(cam_obj.location) @ rot_mat).inverted()
                cam_pts = [inv @ p for p in points]

            min_x = min(p.x for p in cam_pts)
            max_x = max(p.x for p in cam_pts)
            min_y = min(p.y for p in cam_pts)
            max_y = max(p.y for p in cam_pts)
            min_z = min(p.z for p in cam_pts)
            max_z = max(p.z for p in cam_pts)
            cx = (min_x + max_x) * 0.5
            cy = (min_y + max_y) * 0.5
            span_x = max(1e-6, max_x - min_x)
            span_y = max(1e-6, max_y - min_y)

            if aspect >= 1.0:
                required_ortho = max(span_x, span_y * aspect)
            else:
                required_ortho = max(span_x / aspect, span_y)
            S = max(0.01, required_ortho * fit * 1.04)
            cam_data.ortho_scale = S

            # Use shift_x/shift_y to center the view — more reliable than moving the camera
            # Blender shift is in proportion of ortho_scale (1.0 = one full canvas width)
            cam_data.shift_x = cx / S
            cam_data.shift_y = cy / S

            depth = abs(min_z) + abs(max_z) + 1.0
            cam_data.clip_start = max(0.001, depth / 100000.0)
            cam_data.clip_end = max(1000.0, depth * 20.0)
            bpy.context.view_layer.update()

        scene = bpy.context.scene
        scene.render.engine = "BLENDER_EEVEE"
        scene.render.image_settings.file_format = file_format
        scene.render.use_file_extension = False
        scene.render.use_overwrite = True
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        scene.render.pixel_aspect_x = 1.0
        scene.render.pixel_aspect_y = 1.0
        scene.render.use_border = False
        scene.render.use_crop_to_border = False
        scene.render.filepath = out_path
        scene.render.film_transparent = False
        scene.render.use_compositing = False
        scene.render.use_sequencer = False
        scene.view_settings.view_transform = "Filmic"
        scene.view_settings.look = look
        scene.view_settings.exposure = exposure
        scene.frame_set(1)

        if scene.world is None:
            scene.world = bpy.data.worlds.new("PreviewWorld")
        scene.world.use_nodes = True
        bg = scene.world.node_tree.nodes.get("Background")
        if bg is not None:
            bg.inputs["Color"].default_value = (bg_r, bg_g, bg_b, 1.0)
            bg.inputs["Strength"].default_value = bg_strength

        objs = [o for o in scene.objects if o.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}]
        if not objs:
            raise RuntimeError("BLEND enthält keine renderbaren Objekte.")

        points = []
        for obj in objs:
            for corner in obj.bound_box:
                points.append(obj.matrix_world @ mathutils.Vector(corner))
        min_x = min(v.x for v in points); max_x = max(v.x for v in points)
        min_y = min(v.y for v in points); max_y = max(v.y for v in points)
        min_z = min(v.z for v in points); max_z = max(v.z for v in points)
        center = mathutils.Vector(((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5))
        radius = max(max_x - min_x, max_y - min_y, max_z - min_z) * 0.5 + 1e-6
        # Always use dedicated preview camera for consistent framing.
        cam_data = bpy.data.cameras.new("PreviewCamera")
        cam_obj = bpy.data.objects.new("PreviewCamera", cam_data)
        scene.collection.objects.link(cam_obj)
        scene.camera = cam_obj
        direction = mathutils.Vector((1.0, -1.0, 0.75)).normalized()
        cam_obj.location = center + direction * max(1.0, radius * 4.0)
        cam_obj.rotation_euler = (center - cam_obj.location).to_track_quat("-Z", "Y").to_euler()
        fit_camera_to_points(cam_obj, cam_data, points, margin)

        if not [o for o in scene.objects if o.type == "LIGHT"]:
            key_light = bpy.data.lights.new(name="Key", type='SUN')
            key_light.energy = key_energy
            key_obj = bpy.data.objects.new(name="Key", object_data=key_light)
            scene.collection.objects.link(key_obj)
            key_obj.rotation_euler = (radians(50), radians(10), radians(40))
            fill_light = bpy.data.lights.new(name="Fill", type='SUN')
            fill_light.energy = fill_energy
            fill_obj = bpy.data.objects.new(name="Fill", object_data=fill_light)
            scene.collection.objects.link(fill_obj)
            fill_obj.rotation_euler = (radians(45), radians(-20), radians(-120))

        # Apply a consistent preview material override to mesh objects
        # so BLEND thumbnails match STL thumbnail style.
        mesh_objs = [o for o in scene.objects if o.type == "MESH"]
        if mesh_objs:
            mat = bpy.data.materials.new(name="PreviewMaterialBlend")
            mat.use_nodes = True
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            if bsdf is not None:
                bsdf.inputs["Base Color"].default_value = (0.12, 0.29, 0.75, 1.0)
                bsdf.inputs["Roughness"].default_value = 0.42
                if "Specular IOR Level" in bsdf.inputs:
                    bsdf.inputs["Specular IOR Level"].default_value = 0.35
                elif "Specular" in bsdf.inputs:
                    bsdf.inputs["Specular"].default_value = 0.35
            for obj in mesh_objs:
                obj.data.materials.clear()
                obj.data.materials.append(mat)

        result_op = bpy.ops.render.render(write_still=True)
        if "FINISHED" not in result_op:
            raise RuntimeError(f"Render-Operator abgebrochen: {result_op}")
        """
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as script_file:
        script_file.write(script_content)
        script_path = Path(script_file.name)

    cmd = [
        str(blender_exe),
        "--background",
        str(blend_path),
        "--python",
        str(script_path),
        "--",
        str(out_path),
        str(width),
        str(height),
        file_format,
        str(preset["bg_r"]),
        str(preset["bg_g"]),
        str(preset["bg_b"]),
        str(preset["bg_strength"]),
        str(preset["key_energy"]),
        str(preset["fill_energy"]),
        str(preset["exposure"]),
        str(preset["look"]),
        str(margin),
    ]
    started_at = time.time()
    detail = ""
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        std_tail = (completed.stdout or "").strip().splitlines()[-4:]
        err_tail = (completed.stderr or "").strip().splitlines()[-4:]
        detail = " || ".join(std_tail + err_tail)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        raise RuntimeError(f"Blender-Rendering fehlgeschlagen: {detail}") from exc
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass

    _finalize_blender_output(out_path, started_at, detail)


def render_stl_pyvista(
    stl_path: Path, out_path: Path, width: int, height: int, framing_margin: float = 0.18
):
    import pyvista as pv

    mesh = pv.read(str(stl_path))
    if mesh is None or mesh.n_points == 0:
        raise ValueError("STL enthält keine renderbaren Geometriedaten.")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    plotter = pv.Plotter(off_screen=True, window_size=(width, height))
    plotter.set_background("white")
    plotter.add_mesh(
        mesh,
        color="#A9B8CC",
        smooth_shading=True,
        specular=0.25,
        diffuse=0.85,
        ambient=0.2,
        show_edges=True,
        edge_color="#2f3642",
        line_width=0.6,
    )
    plotter.camera_position = "iso"
    margin = max(0.0, min(1.0, float(framing_margin)))
    plotter.camera.zoom(max(0.55, 1.25 - 0.85 * margin))
    plotter.add_light(pv.Light(position=(2, 2, 3), focal_point=(0, 0, 0), intensity=1.0))
    plotter.add_light(pv.Light(position=(-2, -1, 2), focal_point=(0, 0, 0), intensity=0.45))
    plotter.show(auto_close=False)
    plotter.screenshot(str(out_path))
    plotter.close()


def render_stl_matplotlib(
    stl_path: Path, out_path: Path, width: int, height: int, framing_margin: float = 0.18
):
    import matplotlib
    import numpy as np

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    from stl import mesh as stl_mesh

    model = stl_mesh.Mesh.from_file(str(stl_path))
    vectors = model.vectors

    dpi = 100
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor="#c7cbd2")
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0], projection="3d")

    poly = Poly3DCollection(vectors, linewidths=0.05)
    poly.set_facecolor((0.72, 0.78, 0.86, 1.0))
    poly.set_edgecolor((0.18, 0.2, 0.24, 0.35))
    ax.add_collection3d(poly)

    points = vectors.reshape(-1, 3)
    mins = points.min(axis=0).astype(float)
    maxs = points.max(axis=0).astype(float)
    margin = max(0.0, min(1.0, float(framing_margin)))
    center = (mins + maxs) * 0.5
    span = np.maximum(maxs - mins, 1e-6)
    half = 0.5 * span * (1.0 + margin)

    ax.set_xlim(center[0] - half[0], center[0] + half[0])
    ax.set_ylim(center[1] - half[1], center[1] + half[1])
    ax.set_zlim(center[2] - half[2], center[2] + half[2])

    # Orthographic view avoids perspective drift and keeps object centered by geometry.
    try:
        ax.set_proj_type("ortho")
    except Exception:
        pass
    try:
        ax.set_box_aspect(span)
    except Exception:
        set_equal_3d_axes(ax, mins, maxs)
    ax.view_init(elev=26, azim=45)
    ax.set_axis_off()
    try:
        ax.set_facecolor((0.78, 0.80, 0.84, 1.0))
    except Exception:
        pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
