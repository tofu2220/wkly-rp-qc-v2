import hashlib
import json
import tempfile
from pathlib import Path

from .core import JsonDict
from .importer import inspect_workbook, normalized_payload
from .replay import RESPONSE_FILES, read_replay_provenance
from .store import get_original_workbook_path, get_template_mapping, list_snapshots


def normalized_input_for_workbook(database_path: Path, storage_dir: Path, workbook_id: int) -> Path:
    original = get_original_workbook_path(database_path, storage_dir, workbook_id)
    snapshots = [item for item in list_snapshots(database_path) if item["workbook_id"] == workbook_id]
    overrides = {}
    for snapshot in snapshots:
        if snapshot["mapping_version"] is None:
            continue
        mapping = get_template_mapping(database_path, snapshot["template_id"], snapshot["mapping_version"])
        if mapping is None:
            raise ValueError(f"Missing stored template mapping {snapshot['template_id']}@{snapshot['mapping_version']}")
        overrides[snapshot["sheet_name"]] = mapping["mapping"]
    filename = snapshots[0]["original_filename"] if snapshots else original.name
    payload = normalized_payload(inspect_workbook(original, mapping_overrides=overrides), workbook_filename=filename)
    content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    output_dir = Path(storage_dir) / "normalized-inputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{digest}.json"
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"Normalized input hash collision: {digest}")
        return path
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_dir, prefix=f".{digest}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
        temporary.rename(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def eligible_replays(
    normalized_input_path: Path,
    scope: JsonDict,
    role: str,
    aliases: list[str],
    replay_roots: tuple[Path, ...],
) -> list[JsonDict]:
    bundles = []
    for root in replay_roots:
        root = Path(root).resolve()
        if not root.is_dir():
            continue
        for notes_path in sorted(root.rglob("run_notes.md")):
            response_dir = notes_path.parent.resolve()
            if root not in response_dir.parents:
                continue
            response_paths = [response_dir / filename for filename in RESPONSE_FILES]
            if any(not path.is_file() or path.is_symlink() or root not in path.resolve().parents for path in response_paths):
                continue
            try:
                provenance = read_replay_provenance(normalized_input_path, response_dir, scope, role, aliases)
            except (OSError, ValueError):
                continue
            relative_path = response_dir.relative_to(root).as_posix()
            bundle_id = hashlib.sha256(f"{root}\0{relative_path}".encode()).hexdigest()[:24]
            bundles.append({
                "bundle_id": bundle_id,
                "run_name": response_dir.name,
                "response_dir": response_dir,
                "provenance": provenance,
            })
    return sorted(bundles, key=lambda bundle: (bundle["run_name"], bundle["bundle_id"]))
