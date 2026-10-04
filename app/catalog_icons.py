"""Installed presentation assets; arbitrary URLs cannot be assigned as icons."""
import json
from pathlib import Path

ICON_LIBRARY = json.loads(Path(__file__).with_name('icon_manifest.json').read_text(encoding='utf-8'))
ICON_PATHS = frozenset(icon['path'] for icon in ICON_LIBRARY)
