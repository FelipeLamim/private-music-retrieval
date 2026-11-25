"""Sanitize exported diagnostics without changing protocol inputs or measured values.

Only use this on report copies, never on live ready files, keys or requests.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_QUERY_SECRET = re.compile(r"([?&](?:t|token|access_token|api_key)=)[^&#\s\"']+", re.I)
_HEADER_SECRET = re.compile(r"((?:X-PMR-Token|Authorization)\s*:\s*)(?:Bearer\s+|Basic\s+)?[^\s\"']+", re.I)
_CREDENTIAL_URL = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.I)
_LOCAL_PATH = re.compile(r"(?:/(?:home|Users|root|workspace|workspaces|mnt/[a-zA-Z]|tmp)/|(?<![A-Za-z0-9])[A-Za-z]:[\\/])[^\s\"'<>;,\]\)]+")
_ANCHORS = frozenset({'src', 'scripts', 'tests', 'experiments', 'bench', 'results', 'data', 'third_party', '.venv', '.venv-browser', '.tools', '.tmp'})
_SECRET_FIELDS = frozenset({'token', 'access_token', 'refresh_token', 'api_key', 'password', 'client_secret', 'authorization', 'x-pmr-token', 'temp_clone_token'})
_IDENTITY_FIELDS = frozenset({'hostname', 'username', 'user_name', 'machine_name'})
_HANDLES = re.compile(r"(/(?:v1/pir/(?:queries|sessions)|api/jobs)/)[A-Za-z0-9_-]{12,}")


def _portable_path(match: re.Match[str]) -> str:
    parts = match.group().replace('\\', '/').split('/')
    for i, part in enumerate(parts):
        if part in _ANCHORS:
            return '/'.join(parts[i:])
    return '<local-path>'


def public_report(value: Any, *, root: str | Path | None = None, hide_handles: bool = True) -> Any:
    """Return a sanitized copy; keep numeric measurements and input objects unchanged."""
    if isinstance(value, dict):
        return {
            key: ('<redacted>' if str(key).lower() in _SECRET_FIELDS | _IDENTITY_FIELDS
                  and isinstance(item, str) and item else
                  public_report(item, root=root, hide_handles=hide_handles))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [public_report(item, root=root, hide_handles=hide_handles) for item in value]
    if not isinstance(value, str):
        return value
    text = _QUERY_SECRET.sub(r'\1<redacted>', value)
    text = _HEADER_SECRET.sub(r'\1<redacted>', text)
    text = _CREDENTIAL_URL.sub(r'\1<redacted>@', text)
    if root is not None:
        prefix = str(root).rstrip('/\\')
        text = text.replace(prefix + '/', '').replace(prefix + '\\', '')
        if text == prefix:
            text = '.'
    text = _LOCAL_PATH.sub(_portable_path, text)
    if hide_handles:
        text = _HANDLES.sub(r'\1<redacted>', text)
    return text
