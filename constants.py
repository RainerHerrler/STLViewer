from pathlib import Path

CONFIG_PATH = Path.home() / ".stl_preview_gui.json"
GUI_IMAGE_EXT = ".png"
GUI_EXT_CHOICES = (".png", ".jpg", ".jpeg", ".webp")
RENDERER_CHOICES = ("blender", "pyvista", "matplotlib")
BLENDER_PRESET_CHOICES = ("neutral", "kontrast", "dunkelblau")
