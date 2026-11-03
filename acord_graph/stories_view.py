"""User-story view (stories.html): which domain each story belongs to, which ACORD
entities it uses, and what is inside each story's sample payload."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .analyzer import ROOT_HINT_KEYS, guess_root
from .graph import _tint, _vis_js, domain_colors
from .loader import norm
from .model import AcordModel, Entity, UserStory

HOW = {"declared": "declared in the story", "folder": "folder name", "entities": "from the entities its samples use",
       "": "not determined"}
FONT = "Inter, ui-sans-serif, system-ui, sans-serif"
MAX_ASSET_BYTES = 4 * 1024 * 1024


def _preview(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= 60 else s[:57] + "…"


def _tree(model: AcordModel, ent: Entity, data: dict, path: str, fmap: dict, depth: int = 0) -> dict:
    node = {"entity": ent.name, "domain": ent.domain, "path": path, "rows": [], "kids": []}
    if depth > 12:
        return node
    attrs = ent.all_attributes(model)
    by_norm = {norm(k): a for k, a in attrs.items()}
    eidx = {norm(n): n for n in model.entities}
    for key, value in data.items():
        if key.startswith(("@", "$", "_")) or (depth == 0 and key in ROOT_HINT_KEYS):
            continue
        attr = attrs.get(key) or by_norm.get(norm(key))
        p = f"{path}.{key}"
        fs = fmap.get(p, [])
        target = attr.ref_entity if attr and attr.ref_entity in model.entities else \
            (eidx.get(norm(key)) if attr is None and isinstance(value, (dict, list)) else None)
        if target and isinstance(value, (dict, list)):
            items = value if isinstance(value, list) else [value]
            node["rows"].append({"k": key, "v": f"→ {target}" + (f" ×{len(items)}" if isinstance(value, list) else ""),
                                 "t": "object", "st": "warn" if fs else "ok", "msg": [f["m"] for f in fs], "ref": target})
            for i, it in enumerate(items):
                if isinstance(it, dict):
                    cp = f"{p}[{i}]" if isinstance(value, list) else p
                    child = _tree(model, model.entities[target], it, cp, fmap, depth + 1)
                    child["via"] = key + (f"[{i}]" if isinstance(value, list) else "")
                    node["kids"].append(child)
            continue
        levels = {f["l"] for f in fs}
        st = "error" if "error" in levels else "warn" if "warning" in levels else ("ok" if attr else "error")
        msg = [f["m"] for f in fs] or ([] if attr else [f"not an attribute of {ent.name}"])
        if any("deprecated" in m for m in msg):
            st = "dep"
        node["rows"].append({"k": key, "v": _preview(value), "t": attr.type if attr else "", "st": st, "msg": msg,
                             "cl": attr.codelist if attr else "", "req": bool(attr and attr.required)})
    for a in attrs.values():
        fs = [f for f in fmap.get(f"{path}.{a.name}", []) if "required" in f["m"]]
        if fs and a.name not in data:
            node["rows"].append({"k": a.name, "v": "", "t": a.type, "st": "missing", "msg": [fs[0]["m"]], "req": True})
    return node


def story_trees(model: AcordModel, s: UserStory) -> list[dict]:
    fmap: dict[str, list] = {}
    for f in s.findings:
        fmap.setdefault(f.path, []).append({"l": f.level, "m": f.message})
    out = []
    for i, sample in enumerate(s.samples):
        root, body = guess_root(model, sample, s)
        if not root:
            out.append({"entity": "?", "domain": "", "path": f"sample[{i}]", "rows": [], "kids": [],
                        "error": "could not match this sample to any ACORD entity"})
            continue
        out.append(_tree(model, model.entities[root], body, f"sample[{i}]:{root}", fmap))
    return out


def _assets(data_dir: Path | None, s: UserStory) -> list[dict]:
    out, total = [], 0
    for rel in s.assets:
        p = (data_dir / rel) if data_dir else Path(rel)
        if not p.exists():
            continue
        raw = p.read_bytes()
        total += len(raw)
        if total > MAX_ASSET_BYTES:
            out.append({"name": p.name, "src": ""})
            continue
        mime = {"gif": "image/gif", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "svg": "image/svg+xml"}.get(p.suffix.lower().lstrip("."), "application/octet-stream")
        out.append({"name": p.name, "src": f"data:{mime};base64,{base64.b64encode(raw).decode()}"})
    return out


def stories_payload(model: AcordModel, data_dir: Path | None = None) -> dict:
    colors = domain_colors(model)
    stories = sorted(model.stories.values(), key=lambda x: (x.domain, x.id))
    doms = sorted({s.domain or "Unassigned" for s in stories})
    for d in doms:
        colors.setdefault(d, "#64748b")
    nodes, edges = [], []
    for d in doms:
        n = sum(1 for s in stories if (s.domain or "Unassigned") == d)
        c = colors[d]
        nodes.append({"id": f"D:{d}", "kind": "domain", "label": f"<b>{d}</b>\n<i>{n} stories</i>", "level": 0, "domain": d,
                      "shape": "box", "margin": 12, "borderWidth": 2,
                      "color": {"background": _tint(c, 0.82), "border": c, "highlight": {"background": _tint(c, 0.7), "border": c}},
                      "font": {"face": FONT, "multi": "html", "size": 15, "color": "#0f172a",
                               "bold": {"face": FONT, "size": 15}, "ital": {"face": FONT, "size": 11, "color": c}},
                      "shapeProperties": {"borderRadius": 10}})
    used_ents: dict[str, int] = {}
    out_stories = []
    for s in stories:
        d = s.domain or "Unassigned"
        errs = sum(1 for f in s.findings if f.level == "error")
        nodes.append({"id": f"S:{s.id}", "kind": "story", "label": s.id + (" ⚠" if errs else ""), "level": 1, "domain": d,
                      "shape": "box", "margin": {"top": 4, "bottom": 4, "left": 9, "right": 9}, "title": s.title,
                      "color": {"background": "#fff" if not errs else "#fef2f2", "border": colors[d],
                                "highlight": {"background": _tint(colors[d], 0.8), "border": colors[d]}},
                      "borderWidth": 1.5, "shapeProperties": {"borderRadius": 99},
                      "font": {"face": FONT, "size": 12, "color": "#0f172a" if not errs else "#b91c1c"}})
        edges.append({"from": f"D:{d}", "to": f"S:{s.id}", "kind": "dom", "color": {"color": colors[d], "opacity": 0.5}, "width": 1})
        counts: dict[str, int] = {}
        for e, _a in s.used_attributes:
            counts[e] = counts.get(e, 0) + 1
        for e in s.used_entities | set(x for x in s.declared_entities if x in model.entities):
            used_ents[e] = used_ents.get(e, 0) + 1
            root = e in s.roots
            edges.append({"from": f"S:{s.id}", "to": f"E:{e}", "kind": "root" if root else "uses",
                          "width": 3 if root else 1, "dashes": not root and e not in s.used_entities,
                          "color": {"color": "#0f172a" if root else "#94a3b8", "opacity": 0.85 if root else 0.45},
                          "title": f"{s.id} uses {counts.get(e, 0)} attribute(s) of {e}" + (" (sample root)" if root else "")})
        out_stories.append({
            "id": s.id, "title": s.title, "domain": d, "how": HOW.get(s.domain_source, s.domain_source),
            "desc": s.description, "criteria": s.acceptance_criteria, "source": s.source, "roots": s.roots,
            "entities": sorted(s.used_entities), "declared": s.declared_entities,
            "errors": errs, "warnings": sum(1 for f in s.findings if f.level == "warning"),
            "trees": story_trees(model, s), "assets": _assets(data_dir, s), "nSamples": len(s.samples)})
    for e, n in used_ents.items():
        ent = model.entities[e]
        c = colors.get(ent.domain, "#64748b")
        nodes.append({"id": f"E:{e}", "kind": "entity", "level": 2, "domain": ent.domain,
                      "label": f"<b>{e}</b>\n<i>{ent.domain} · {n} stories</i>", "shape": "box",
                      "margin": {"top": 7, "bottom": 7, "left": 11, "right": 11}, "borderWidth": 1.5,
                      "color": {"background": _tint(c, 0.88), "border": c, "highlight": {"background": _tint(c, 0.72), "border": c}},
                      "font": {"face": FONT, "multi": "html", "size": 13, "color": "#0f172a",
                               "bold": {"face": FONT, "size": 13}, "ital": {"face": FONT, "size": 10, "color": c}},
                      "shapeProperties": {"borderRadius": 7}})
    for i, e in enumerate(edges):
        e["id"] = i
        e.setdefault("arrows", {"to": {"enabled": e["kind"] != "dom", "scaleFactor": 0.5}})
    ents_by_domain: dict[str, list[str]] = {}
    for e in used_ents:
        ents_by_domain.setdefault(model.entities[e].domain, []).append(e)
    return {"nodes": nodes, "edges": edges, "stories": out_stories, "domains": [[d, colors[d]] for d in doms],
            "colors": colors, "entityDomains": {e: model.entities[e].domain for e in used_ents},
            "counts": {"stories": len(stories), "domains": len(doms), "entities": len(used_ents)}}


def to_stories_html(model: AcordModel, data_dir: Path | None = None, title: str = "User stories ↔ ACORD") -> str:
    tpl = (Path(__file__).parent / "static" / "stories.html").read_text(encoding="utf-8")
    data = json.dumps(stories_payload(model, data_dir)).replace("</", "<\\/")
    return tpl.replace("{{TITLE}}", title).replace("{{VIS_JS}}", _vis_js()).replace("{{DATA}}", data)
