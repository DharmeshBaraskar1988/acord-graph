"""Project data store: uploaded files are kept inside the project, under data/.

    data/
      standard/     ACORD files: master resource definition, master code list, deprecation list ...
      custom/       company / custom model files (applied as an overlay)
      stories/      user stories: JSON files and unpacked ZIPs (folders kept; images/GIFs kept too)

Everything in data/ is loaded automatically the next time the app or the CLI starts.
ZIP files are unpacked safely (no paths outside the target folder, size limits).
"""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
KINDS = ("standard", "custom", "stories")
ASSET_EXT = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".svg"}
TEXT_EXT = {".json"}
MAX_FILE = 50 * 1024 * 1024        # per unpacked file
MAX_TOTAL = 500 * 1024 * 1024      # per ZIP

# folder names that are not domains
GENERIC_FOLDER = re.compile(r"^(custom|ext|extensions?|overlay|user[ _-]?stor(y|ies)|stories|stor(y|ies)|"
                            r"code[ _-]?lists?|codes?|samples?|examples?|deprecat\w*|json|schemas?|data|"
                            r"standard|acord|ngds|files?|input|output|src|resources?|v?\d[\d._-]*|__macosx|misc|other|common_?files|"
                            r"assets?|images?|gifs?|docs?|attachments?|tmp|temp|new|old|archive|backup|ok)$", re.I)


def folder_domain(parts: list[str]) -> str:
    """Deepest folder name that looks like a domain (Claims/, policy/ ...), else ''."""
    for p in reversed(parts):
        p = p.strip()
        if re.search(r"(^|[^A-Za-z])[A-Za-z]{0,6}[-_ ]?\d{2,}", p):   # US-301, story_12, 2026-10 ...
            continue
        if p and not GENERIC_FOLDER.match(p) and not p.startswith("."):
            return p[:1].upper() + p[1:]
    return ""


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name).strip(" .")
    return name or "file"


def _safe_rel(member: str) -> PurePosixPath | None:
    p = PurePosixPath(member.replace("\\", "/"))
    if p.is_absolute() or any(x in ("..", "") for x in p.parts) or not p.parts:
        return None
    if p.parts[0] == "__MACOSX" or p.name.startswith("._"):
        return None
    return PurePosixPath(*[_safe_name(x) for x in p.parts])


@dataclass
class Saved:
    path: Path
    kind: str
    note: str = ""


def save_upload(kind: str, name: str, raw: bytes, data_dir: Path = DATA_DIR) -> list[Saved]:
    """Store one uploaded file. A .zip is unpacked into a folder named after it."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    target = data_dir / kind
    target.mkdir(parents=True, exist_ok=True)
    out: list[Saved] = []
    if name.lower().endswith(".zip"):
        root = target / _safe_name(Path(name).stem)
        if root.exists():
            shutil.rmtree(root)               # re-upload replaces the previous copy
        total = 0
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = _safe_rel(info.filename)
                if rel is None or Path(rel.name).suffix.lower() not in TEXT_EXT | ASSET_EXT:
                    continue
                if info.file_size > MAX_FILE or total + info.file_size > MAX_TOTAL:
                    out.append(Saved(root / str(rel), kind, "skipped: too large"))
                    continue
                total += info.file_size
                dest = (root / str(rel)).resolve()
                if not str(dest).startswith(str(root.resolve())):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append(Saved(dest, kind))
    else:
        dest = target / _safe_name(name)
        dest.write_bytes(raw)
        out.append(Saved(dest, kind))
    return out


def stored_files(data_dir: Path = DATA_DIR) -> list[tuple[str, Path]]:
    """(kind, path) for every stored JSON file."""
    out = []
    for kind in KINDS:
        base = data_dir / kind
        if base.exists():
            out += [(kind, p) for p in sorted(base.rglob("*.json")) if p.is_file()]
    return out


def stored_assets(data_dir: Path = DATA_DIR) -> list[Path]:
    base = data_dir / "stories"
    return sorted(p for p in base.rglob("*") if p.suffix.lower() in ASSET_EXT) if base.exists() else []


def stored_items(data_dir: Path = DATA_DIR) -> list[tuple[str, Path, int]]:
    """Top-level items per kind (a file, or a folder from a ZIP) with their JSON file count."""
    out = []
    for kind in KINDS:
        base = data_dir / kind
        if not base.exists():
            continue
        for p in sorted(base.iterdir()):
            if p.name.startswith("."):
                continue
            n = sum(1 for _ in p.rglob("*.json")) if p.is_dir() else 1
            out.append((kind, p, n))
    return out


def delete_item(path: Path, data_dir: Path = DATA_DIR) -> None:
    path = path.resolve()
    if not str(path).startswith(str(data_dir.resolve())):
        raise ValueError("refusing to delete outside the data folder")
    shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)


def clear_all(data_dir: Path = DATA_DIR) -> None:
    for kind in KINDS:
        if (data_dir / kind).exists():
            shutil.rmtree(data_dir / kind)


def domain_hint_for(path: Path, kind: str, data_dir: Path = DATA_DIR) -> str:
    rel = path.resolve().relative_to((data_dir / kind).resolve())
    return folder_domain(list(rel.parts[:-1]))


def fingerprint(data_dir: Path = DATA_DIR) -> tuple:
    """Changes whenever a stored file is added, removed or modified (used as a cache key)."""
    return tuple((k, str(p), p.stat().st_mtime_ns, p.stat().st_size) for k, p in stored_files(data_dir))


def load_store(data_dir: Path = DATA_DIR):
    """Load everything in data/: standard first, then user stories, then the custom overlay."""
    import json as _json
    from .loader import load_json_bytes
    from .model import AcordModel
    model = AcordModel()
    order = {"standard": 0, "stories": 1, "custom": 2}
    for kind, path in sorted(stored_files(data_dir), key=lambda kp: order[kp[0]]):
        rel = path.resolve().relative_to(data_dir.resolve()).as_posix()
        try:
            part = load_json_bytes(path.read_bytes(), rel, domain_hint_for(path, kind, data_dir),
                                   custom=(kind == "custom"), kind=kind)
            model.merge(part)
        except (_json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            model.load_log.append(f"{rel}: skipped ({exc})")
    # images / GIFs: attach to the story whose id is in the file name, or to the only story in that folder
    assets = stored_assets(data_dir)
    by_dir: dict[Path, list] = {}
    for s in model.stories.values():
        by_dir.setdefault((data_dir / s.source).parent.resolve(), []).append(s)
    for a in assets:
        rel = a.resolve().relative_to(data_dir.resolve()).as_posix()
        stem = a.stem.lower()
        hit = [s for s in model.stories.values() if s.id.lower() in stem or stem in s.id.lower()]
        if not hit:
            here = by_dir.get(a.parent.resolve(), [])
            hit = here if len(here) == 1 else []
        for s in hit:
            s.assets.append(rel)
    return model
