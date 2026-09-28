import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

with open(ROOT / "config.json") as f:
    CONFIG = json.load(f)
