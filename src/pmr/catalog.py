"""Server-side catalog loading and the public catalog description.

Only the database server reads audio files. The public description contains metadata that a
streaming service would show anyway (titles, durations, licenses); it never depends on what a
client is listening to.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from .audio.dataset import load_manifest
from .audio.wavio import read_wav_pcm16_mono

PUBLIC_TRACK_FIELDS = ("index", "id", "title", "artist", "duration_s", "pcm_bytes", "license",
                       "license_url", "attribution", "origin", "source_url")


def load_catalog(catalog_dir: str, *, max_tracks: int | None = None, verify: bool = True) -> tuple[dict, list[bytes]]:
    manifest = load_manifest(catalog_dir)
    tracks = manifest["tracks"][: max_tracks] if max_tracks else manifest["tracks"]
    records = []
    for t in tracks:
        pcm = read_wav_pcm16_mono(os.path.join(catalog_dir, t["file"]), manifest["sample_rate"]).pcm
        if verify and hashlib.sha256(pcm).hexdigest() != t["sha256_pcm"]:
            raise ValueError(f"track {t['id']}: PCM hash does not match the manifest")
        records.append(pcm)
    if max_tracks:
        manifest = dict(manifest, tracks=tracks)
    return manifest, records


def public_catalog(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "catalog_id": manifest["catalog_id"],
        "audio": {"sample_rate": manifest["sample_rate"], "channels": manifest["channels"],
                  "sample_width": manifest["sample_width"], "encoding": "pcm_s16le"},
        "tracks": [{k: t.get(k) for k in PUBLIC_TRACK_FIELDS} for t in manifest["tracks"]],
    }
