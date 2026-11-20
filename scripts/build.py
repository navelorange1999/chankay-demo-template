"""Build the example site with versioned URLs using only the Python standard library."""

import os
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
slug = os.environ["DEMO_SLUG"]
sha = os.environ["DEMO_RELEASE"]
if slug == "api" or len(slug) > 63 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
    raise ValueError("Invalid demo slug")
if not re.fullmatch(r"[a-f0-9]{40}", sha):
    raise ValueError("Invalid release SHA")
if (root / "dist").is_symlink():
    raise ValueError("Invalid build directory")
output = root / "dist"
output.mkdir(exist_ok=True)
for name in ("index.html", "app.css", "app.js"):
    dest = output / name
    if dest.is_symlink():
        raise ValueError("Invalid build destination")
    text = (root / "site" / name).read_text()
    if name == "index.html":
        text = text.replace("{{slug}}", slug).replace("{{asset_base}}", f"/{slug}/_r/{sha}/")
    dest.write_text(text)
