"""
Every filesystem location the package derives from its own position, in one place.

    <app>/                        APP_DIR      (config.yaml, static/, the app.py launcher)
    <app>/dataviz/                PKG_DIR
    <project root>/               PROJECT_ROOT (config paths such as dataset.path and storage.allowed_roots '.' are
                                               relative to it)

The app may sit inside a bigger project as <project>/scripts/image-tools/ (the Docker image: /workspace/scripts/
image-tools), where the project root is two levels up; cloned on its own, the clone is the project root. Taking two
levels up for a plain clone made allowed_roots '.' mean e.g. /home (every user's folders). DATAVIZ_PROJECT_ROOT
overrides both. Moving a module never changes these; moving the package means editing only this file.
"""

import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
APP_DIR = PKG_DIR.parent
LEGACY_ROOT = APP_DIR.parents[1]        # where a plain clone's project root used to be


def _project_root():
    if os.environ.get('DATAVIZ_PROJECT_ROOT'):
        return Path(os.environ['DATAVIZ_PROJECT_ROOT']).expanduser().resolve()
    if APP_DIR.parent.name == 'scripts':
        return LEGACY_ROOT
    # an install from before this rule keeps its root: its workspace holds secret.key, without which the stored
    # connection secrets cannot be decrypted (cli.py warns, with how to move)
    if (LEGACY_ROOT / '.dataviz_cache' / 'secret.key').is_file() and not (APP_DIR / '.dataviz_cache' / 'secret.key').is_file():
        return LEGACY_ROOT
    return APP_DIR


PROJECT_ROOT = _project_root()
STATIC_DIR = APP_DIR / 'static'
DEFAULT_CONFIG = APP_DIR / 'config.yaml'
DATA_PREP_DIR = APP_DIR.parent / 'data_prep'      # scripts/data_prep (shared session-name rules)
