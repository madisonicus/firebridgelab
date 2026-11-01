"""Persistence backbone for Saved Scenarios (sv1).

One JSON file per saved scenario, in the app-root ``scenarios/`` directory
(which may be a symlink to a synced folder). This module owns the *store* mechanics
only — slugify, path-traversal confinement, atomic writes, provenance stamping,
caps, and the conflict-file filter. The FastAPI routes in ``main.py`` validate
the blob *shape* and translate exceptions to HTTP status codes.

SECURITY (load-bearing): user-supplied names become filenames. Defense in depth:
slugify to ``[a-z0-9-]`` only, validate any ``{id}`` path param against a strict
pattern, and resolve-and-assert the final path stays inside ``SCENARIOS_DIR``. A
save named ``../../etc/cron.d/x`` must be impossible to write outside the dir.
"""

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Canonical store dir, resolved like ``BOARD_FILE`` in main.py. Overridable at
# the MODULE level (assign ``scenario_store.SCENARIOS_DIR = tmp``) so tests can
# point it at a tmp dir without touching the real store. Every access goes
# through ``_store_dir()`` which mkdirs on demand, so reassigning this variable
# is sufficient.
SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"

# App dir for the best-effort ``git rev-parse`` provenance stamp.
_APP_DIR = Path(__file__).resolve().parent.parent

# Caps (bound the store + each blob).
MAX_BLOB_BYTES = 64 * 1024  # 64 KB
MAX_SCENARIOS = 200

# A valid scenario id: lowercase alnum, optional internal dashes, 1..64 chars,
# must start with an alnum. This is the SAME shape slugify() produces, and the
# pattern the {id} path param is validated against before it ever builds a path.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class ScenarioStoreError(Exception):
    """Base class for store-level errors the API translates to HTTP codes."""


class InvalidScenarioId(ScenarioStoreError):
    """Slug/id is empty, malformed, or escapes the store dir (-> 400)."""


class ScenarioNotFound(ScenarioStoreError):
    """No saved scenario for the given id (-> 404)."""


class ScenarioTooLarge(ScenarioStoreError):
    """Serialized blob exceeds MAX_BLOB_BYTES (-> 413)."""


class ScenarioStoreFull(ScenarioStoreError):
    """Store is at MAX_SCENARIOS and this would be a new create (-> 400)."""


def _store_dir() -> Path:
    """Return SCENARIOS_DIR, creating it on demand. Read live each call so a
    test reassigning the module-level SCENARIOS_DIR takes effect immediately."""
    d = SCENARIOS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def slugify(name: str) -> str:
    """Lowercase, collapse runs of non-``[a-z0-9]`` to a single ``-``, strip
    leading/trailing ``-``, cap at 64 chars. Raises InvalidScenarioId if the
    result is empty (e.g. a name that's all punctuation or whitespace)."""
    if not isinstance(name, str):
        raise InvalidScenarioId("name must be a string")
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = slug[:64]
    # A trailing dash can only appear if the 64th char was a dash; strip again.
    slug = slug.strip("-")
    if not slug:
        raise InvalidScenarioId(f"name {name!r} slugifies to empty")
    return slug


def _validate_id(scenario_id: str) -> str:
    """Validate a {id} path param against the strict id pattern. Returns the id
    unchanged on success; raises InvalidScenarioId otherwise. This rejects
    ``../``, absolute paths, ``..%2f`` (after the framework un-escapes it),
    uppercase, dots, slashes, and over-length ids before any path is built."""
    if not isinstance(scenario_id, str) or not _ID_RE.match(scenario_id):
        raise InvalidScenarioId(f"invalid scenario id: {scenario_id!r}")
    return scenario_id


def _path_for(scenario_id: str) -> Path:
    """Build the confined on-disk path for a (validated) id. Resolve-and-assert:
    the resolved target MUST stay inside the resolved store dir. Defense in depth
    on top of _validate_id — even if the pattern were bypassed, a path that
    escaped the dir is rejected here."""
    _validate_id(scenario_id)
    store = _store_dir().resolve()
    target = (store / f"{scenario_id}.json").resolve()
    if not _is_relative_to(target, store):
        raise InvalidScenarioId(
            f"path for id {scenario_id!r} escapes the store dir"
        )
    return target


def _is_relative_to(path: Path, base: Path) -> bool:
    """``Path.is_relative_to`` shim (added in 3.9; we target 3.12 but keep the
    shim explicit so the confinement assert is obvious and self-contained)."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _app_revision() -> Optional[str]:
    """Best-effort git short SHA of the app dir at save time (provenance). Any
    failure (no git, detached/odd state, timeout) -> None, never raises."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_APP_DIR),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:
        return None


def _iter_scenario_files() -> list[Path]:
    """All real saved-scenario files: ``*.json`` only, EXCLUDING Syncthing
    conflict copies (``*.sync-conflict-*``). A conflict copy is named like
    ``my-plan.sync-conflict-20260530-...-XXXX.json`` — it ends in ``.json`` but
    carries ``.sync-conflict-`` in the stem, so filter on that marker."""
    store = _store_dir()
    out = []
    for p in store.glob("*.json"):
        if not p.is_file():
            continue
        if ".sync-conflict-" in p.name:
            continue
        out.append(p)
    return out


def list_scenarios() -> list[dict]:
    """Metadata for every saved scenario, sorted by ``updated_at`` descending
    (most-recent first). Cheap: reads id/name/created_at/updated_at only. A file
    that fails to parse or whose id doesn't match the strict pattern is skipped
    (it never corrupts the list)."""
    metas = []
    for p in _iter_scenario_files():
        stem = p.stem
        if not _ID_RE.match(stem):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                blob = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(blob, dict):
            continue
        metas.append(
            {
                "id": blob.get("id", stem),
                "name": blob.get("name", stem),
                "created_at": blob.get("created_at"),
                "updated_at": blob.get("updated_at"),
            }
        )
    # Most-recent first; None updated_at sorts last (treated as empty string).
    metas.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
    return metas


def read_scenario(scenario_id: str) -> dict:
    """Return the full blob for one save. Raises ScenarioNotFound if absent,
    InvalidScenarioId if the id is malformed/escaping."""
    target = _path_for(scenario_id)
    if not target.is_file():
        raise ScenarioNotFound(scenario_id)
    with open(target, encoding="utf-8") as f:
        return json.load(f)


def write_scenario(blob: dict) -> dict:
    """Upsert a scenario by slug derived from ``blob['name']``.

    Stamps server-owned fields: ``id`` (the slug), ``created_at`` (only on first
    write — stable across overwrites), ``updated_at`` (always), and
    ``app_revision`` (best-effort git short SHA). The write is ATOMIC: a temp
    file in the same dir + ``os.replace()`` — never a partial JSON for Syncthing
    to propagate.

    Caps: rejects a serialized blob > MAX_BLOB_BYTES (ScenarioTooLarge), and
    rejects a NEW create beyond MAX_SCENARIOS (ScenarioStoreFull); an overwrite
    of an existing id is always allowed (it doesn't grow the store).

    Returns the stamped blob actually written.
    """
    if not isinstance(blob, dict):
        raise InvalidScenarioId("blob must be an object")
    name = blob.get("name")
    if not isinstance(name, str):
        raise InvalidScenarioId("blob.name must be a string")
    slug = slugify(name)  # raises InvalidScenarioId on empty
    target = _path_for(slug)

    existing = None
    if target.is_file():
        try:
            with open(target, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = None

    is_new = existing is None
    # Count cap: only a NEW create counts against the store size. Count existing
    # *real* scenario files (excludes conflict copies).
    if is_new and len(_iter_scenario_files()) >= MAX_SCENARIOS:
        raise ScenarioStoreFull(
            f"store is at the {MAX_SCENARIOS}-scenario cap"
        )

    now = _now_iso()
    created_at = (
        existing.get("created_at")
        if isinstance(existing, dict) and existing.get("created_at")
        else now
    )

    stamped = dict(blob)
    stamped["id"] = slug
    stamped["created_at"] = created_at
    stamped["updated_at"] = now
    stamped["app_revision"] = _app_revision()

    data = json.dumps(stamped, ensure_ascii=False, indent=2)
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_BLOB_BYTES:
        raise ScenarioTooLarge(
            f"scenario blob is {len(encoded)} bytes (cap {MAX_BLOB_BYTES})"
        )

    store = _store_dir()
    # Atomic write: temp file in the SAME dir (so os.replace is a rename, not a
    # cross-device copy), then atomic rename into place.
    fd, tmp_path = tempfile.mkstemp(dir=str(store), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        # Clean up the temp file on any failure so we never leave litter.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return stamped


def delete_scenario(scenario_id: str) -> None:
    """Delete one save. Raises ScenarioNotFound if absent, InvalidScenarioId if
    the id is malformed/escaping."""
    target = _path_for(scenario_id)
    if not target.is_file():
        raise ScenarioNotFound(scenario_id)
    target.unlink()
