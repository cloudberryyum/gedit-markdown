"""Build the fixed preview shell; document text enters only through JSON."""

from __future__ import annotations

import html
import json
import secrets
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
MAX_DOCUMENT_BYTES = 4_000_000


def document_script(source: str, options: dict[str, object]) -> str:
    """Serialize a render request without interpolating executable source."""
    if len(source.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError("Preview supports documents up to 4 MB. Close preview to edit normally.")
    return "window.mdPreview.render(" + json.dumps(source) + "," + json.dumps(options) + ");void 0;"


def preview_html(theme: str = "light") -> str:
    """Use a fixed, local shell with a restrictive resource policy."""
    if theme not in {"light", "dark"}:
        raise ValueError("Unknown preview theme")
    asset_uri = ASSETS.as_uri() + "/"
    nonce = secrets.token_urlsafe(24)
    policy = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'unsafe-inline' file:; img-src file: data:; "
        "font-src data:; connect-src 'none'; frame-src 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    return f"""<!doctype html>
<html lang="en" data-theme="{theme}">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{html.escape(policy, quote=True)}">
<title>Markdown Preview</title>
<link rel="stylesheet" href="{asset_uri}preview.css">
<script defer nonce="{nonce}" src="{asset_uri}vendor.js"></script>
<script defer nonce="{nonce}" src="{asset_uri}preview.js"></script>
</head><body>
<div id="layout"><aside id="outline" aria-label="Document outline" hidden>
<h2>On this page</h2><nav id="outline-links"></nav></aside>
<main id="document" tabindex="-1"><p class="empty">Loading preview…</p></main></div>
<div id="error" role="alert" hidden></div>
</body></html>"""
