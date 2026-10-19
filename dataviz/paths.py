"""
Every filesystem location the package derives from its own position, in one place.

    scripts/image-tools/          APP_DIR      (config.yaml, static/, the app.py launcher)
    scripts/image-tools/dataviz/  PKG_DIR
    <project root>/               PROJECT_ROOT (config paths such as dataset.path are relative to it)

Moving a module never changes these; moving the package means editing only this file.
"""

from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
APP_DIR = PKG_DIR.parent
PROJECT_ROOT = APP_DIR.parents[1]
STATIC_DIR = APP_DIR / 'static'
DEFAULT_CONFIG = APP_DIR / 'config.yaml'
DATA_PREP_DIR = APP_DIR.parent / 'data_prep'      # scripts/data_prep (shared session-name rules)
