"""Build a NetworkX graph from the model and export it (HTML, GraphML, JSON)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import networkx as nx

from .model import AcordModel

# orange is reserved for custom / company components, so no domain gets an orange tone
CUSTOM_COLOR = "#f97316"   # reserved for custom / company components

PALETTE = ["#2563eb", "#16a34a", "#9333ea", "#dc2626", "#0891b2", "#db2777", "#65a30d",
           "#4f46e5", "#0d9488", "#be123c", "#7c3aed", "#0369a1", "#a21caf", "#15803d"]
EDGE_STYLE = {
    "composition": {"color": "#475569", "dashes": False, "arrows": "to"},
    "association": {"color": "#2563eb", "dashes": False, "arrows": "to"},
    "inheritance": {"color": "#9333ea", "dashes": [8, 4], "arrows": "to"},
    "reference": {"color": "#0891b2", "dashes": [2, 4], "arrows": "to"},
    "codelist": {"color": "#a3a3a3", "dashes": [3, 3], "arrows": "to"},
    "story": {"color": "#f59e0b", "dashes": [6, 3], "arrows": "to"},
}


def domain_colors(model: AcordModel) -> dict[str, str]:
    doms = [d for d in model.domains() if d not in ("Shared", "Unassigned")]
    out = {d: PALETTE[i % len(PALETTE)] for i, d in enumerate(doms)}
    out["Shared"], out["Unassigned"] = "#475569", "#94a3b8"
    return out


def build_graph(model: AcordModel, include_codelists: bool = True, include_stories: bool = True,
                domains: list[str] | None = None) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    keep = set(domains) if domains else None
    for e in model.entities.values():
        if keep and e.domain not in keep:
            continue
        g.add_node(f"E:{e.name}", kind="entity", label=e.name, domain=e.domain, deprecated=e.deprecated,
                   description=e.description, n_attributes=len(e.attributes), origin=e.origin,
                   used_in=",".join(sorted(e.used_in_domains)))
    for r in model.relationships():
        s, t = f"E:{r.source}", f"E:{r.target}"
        if s not in g:
            continue
        if t not in g:
            if keep and r.target in model.entities:
                # keep cross-domain neighbour visible as a ghost node
                tgt = model.entities[r.target]
                g.add_node(t, kind="entity", label=r.target, domain=tgt.domain, ghost=True,
                           description=tgt.description, n_attributes=len(tgt.attributes))
            else:
                g.add_node(t, kind="missing", label=r.target, domain="(undefined)", description="Referenced but not defined")
        src_attr = model.entities[r.source].attributes.get(r.name) if r.source in model.entities else None
        g.add_edge(s, t, kind=r.rel_type, label=r.name, cardinality=r.cardinality,
                   deprecated=bool(src_attr and src_attr.deprecated))
    if keep:  # an entity outside the selected domains but used by them stays visible
        for e in model.entities.values():
            if f"E:{e.name}" not in g and e.used_in_domains & keep:
                g.add_node(f"E:{e.name}", kind="entity", label=e.name, domain=e.domain, ghost=True,
                           description=e.description, n_attributes=len(e.attributes), origin=e.origin,
                           used_in=",".join(sorted(e.used_in_domains)))
    if include_codelists:
        for e in model.entities.values():
            if f"E:{e.name}" not in g:
                continue
            for a in e.attributes.values():
                if a.codelist:
                    cn = f"C:{a.codelist}"
                    if cn not in g:
                        cl = model.codelists.get(a.codelist)
                        g.add_node(cn, kind="codelist", label=a.codelist,
                                   domain=(cl.domain if cl and cl.domain and cl.domain != "Unassigned" else e.domain),
                                   n_values=len(cl.values) if cl else 0, missing=cl is None,
                                   deprecated=bool(cl and cl.deprecated))
                    g.add_edge(f"E:{e.name}", cn, kind="codelist", label=a.name, cardinality="")
    if include_stories:
        for s in model.stories.values():
            ents = s.used_entities or set(s.declared_entities)
            ents = [x for x in ents if f"E:{x}" in g]
            if not ents:
                continue
            sn = f"S:{s.id}"
            g.add_node(sn, kind="story", label=s.id, domain=s.domain, title=s.title)
            for x in sorted(ents):
                g.add_edge(sn, f"E:{x}", kind="story", label="uses", cardinality="")
    return g


def _tint(hex_color: str, amount: float) -> str:
    h = hex_color.lstrip("#")
    r, gg, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: round(c + (255 - c) * amount)
    return f"#{mix(r):02x}{mix(gg):02x}{mix(b):02x}"


def _cluster_of(g: nx.MultiDiGraph, nid: str, d: dict) -> str:
    if d["kind"] == "missing":
        preds = [g.nodes[u].get("domain") for u, _ in g.in_edges(nid)]
        return preds[0] if preds else "(undefined)"
    return d.get("domain") or "Unassigned"


def _separate(pos: dict, widths: dict, height: float = 58, gap: float = 26, rounds: int = 60) -> dict:
    """Push overlapping entity boxes apart (keeps the overall shape of the layout)."""
    pos = {k: [x, y] for k, (x, y) in pos.items()}
    keys = list(pos)
    for _ in range(rounds):
        moved = False
        for i in range(len(keys)):
            a = keys[i]
            for j in range(i + 1, len(keys)):
                b = keys[j]
                dx, dy = pos[b][0] - pos[a][0], pos[b][1] - pos[a][1]
                ox = (widths[a] + widths[b]) / 2 + gap - abs(dx)
                oy = height + gap - abs(dy)
                if ox > 0 and oy > 0:
                    moved = True
                    if ox / (widths[a] + widths[b]) < oy / (2 * height):   # cheaper to separate sideways
                        s_ = (1 if dx >= 0 else -1) * ox / 2
                        pos[a][0] -= s_; pos[b][0] += s_
                    else:
                        s_ = (1 if dy >= 0 else -1) * oy / 2
                        pos[a][1] -= s_; pos[b][1] += s_
        if not moved:
            break
    return {k: (v[0], v[1]) for k, v in pos.items()}


def layout(g: nx.MultiDiGraph) -> dict[str, tuple[float, float]]:
    """Domain-clustered layout. Entities of each domain are laid out on their own and the
    domains are packed in rows; code lists and user stories are placed as small satellites
    around the entity they belong to, so they never crowd the entities."""
    import math
    sat_kinds = ("codelist", "story")
    core = [n for n, d in g.nodes(data=True) if d["kind"] not in sat_kinds]
    clusters: dict[str, list[str]] = {}
    for nid in core:
        clusters.setdefault(_cluster_of(g, nid, g.nodes[nid]), []).append(nid)
    und = nx.Graph(g.subgraph(core).to_undirected())
    sats: dict[str, list[str]] = {}
    for n, d in g.nodes(data=True):
        if d["kind"] in sat_kinds:
            owners = [v for _, v in g.out_edges(n)] if d["kind"] == "story" else [u for u, _ in g.in_edges(n)]
            if owners:
                sats.setdefault(sorted(owners)[0], []).append(n)
    blocks = []
    for dom, members in clusters.items():
        sub = und.subgraph(members)
        n = len(members)
        r = 60 + 105 * n ** 0.5
        if n == 1:
            pos = {members[0]: (0.0, 0.0)}
        else:
            init = nx.circular_layout(sub)
            pos = nx.spring_layout(sub, pos=init, seed=7, k=2.6 / n ** 0.5, iterations=120 if n > 150 else 250)
            xs = [p[0] for p in pos.values()]; ys = [p[1] for p in pos.values()]
            cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
            span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6) / 2
            pos = {k: ((p[0] - cx) / span * r, (p[1] - cy) / span * r * 0.8) for k, p in pos.items()}
            pos = _separate(pos, {k: 9 * len(str(g.nodes[k].get("label", k))) + 60 for k in pos})
            xs = [p[0] for p in pos.values()]; ys = [p[1] for p in pos.values()]
            r = max(r, (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 1.6)
        blocks.append((dom, pos, 2 * r + 330, 1.6 * r + 300))
    blocks.sort(key=lambda b: -b[2] * b[3])
    total = sum(w * h for _, _, w, h in blocks)
    row_w = max(max((b[2] for b in blocks), default=0), (total ** 0.5) * 1.5)
    out: dict[str, tuple[float, float]] = {}
    centers: dict[str, tuple[float, float]] = {}
    x = y = row_h = 0.0
    for dom, pos, w, h in blocks:
        if x > 0 and x + w > row_w:
            x, y, row_h = 0.0, y + row_h, 0.0
        cx, cy = x + w / 2, y + h / 2
        centers[dom] = (cx, cy)
        for k, (px, py) in pos.items():
            out[k] = (cx + px, cy + py)
        x += w
        row_h = max(row_h, h)
    # satellites: an arc facing away from the domain centre
    for owner, items in sats.items():
        if owner not in out:
            continue
        ox, oy = out[owner]
        ccx, ccy = centers.get(_cluster_of(g, owner, g.nodes[owner]), (ox, oy + 1))
        base = math.atan2(oy - ccy, ox - ccx) if (ox, oy) != (ccx, ccy) else -math.pi / 2
        k = len(items)
        spread = min(2 * math.pi * 0.9, 0.42 * k)
        ring = 70 + 6 * max(0, k - 6)
        for i, nid in enumerate(sorted(items)):
            a = base + (i - (k - 1) / 2) * (spread / max(k - 1, 1)) if k > 1 else base
            out[nid] = (ox + ring * math.cos(a), oy + ring * math.sin(a))
    return out


def _payload(model: AcordModel, g: nx.MultiDiGraph) -> dict:
    colors = domain_colors(model)
    colors.setdefault("Shared", "#475569")
    nodes, edges = [], []
    pos = layout(g)
    for nid, d in g.nodes(data=True):
        kind = d["kind"]
        color = colors.get(d.get("domain"), "#64748b")
        x, y = pos.get(nid, (0, 0))
        used_in = [u for u in (d.get("used_in") or "").split(",") if u]
        node = {"id": nid, "label": d["label"], "kind": kind, "domain": d.get("domain", ""),
                "cluster": _cluster_of(g, nid, d), "usedIn": used_in, "origin": d.get("origin", "standard"),
                "x": round(x, 1), "y": round(y, 1)}
        font = {"face": "Inter, ui-sans-serif, system-ui, sans-serif", "multi": "html"}
        if kind == "entity":
            sub = []
            if d.get("deprecated"):
                sub.append("deprecated")
            if node["origin"] == "custom":
                sub.append("custom")
            elif node["origin"] == "extended":
                sub.append("+ custom")
            if len(used_in) > 1:
                sub.append(f"shared · {len(used_in)} domains")
            node["label"] = f"<b>{d['label']}</b>" + (f"\n<i>{' · '.join(sub)}</i>" if sub else "")
            if d.get("deprecated"):
                node.update(shape="box", color={"background": "#f3f4f6", "border": "#9ca3af"},
                            font={**font, "color": "#6b7280", "size": 13, "ital": {"size": 10, "color": "#9ca3af"}},
                            shapeProperties={"borderDashes": [4, 3], "borderRadius": 8}, margin=10, borderWidth=1)
            else:
                if node["origin"] == "custom":
                    color = CUSTOM_COLOR          # company / custom components always in the reserved colour
                border = CUSTOM_COLOR if node["origin"] in ("custom", "extended") else color
                node.update(shape="box", margin={"top": 9, "bottom": 9, "left": 13, "right": 13},
                            borderWidth=2.5 if node["origin"] == "extended" else (2 if len(used_in) > 1 else 1.5),
                            color={"background": _tint(color, 0.85 if node["origin"] == "custom" else 0.88), "border": border,
                                   "highlight": {"background": _tint(color, 0.7), "border": border},
                                   "hover": {"background": _tint(color, 0.8), "border": border}},
                            font={**font, "color": "#0f172a", "size": 14,
                                  "ital": {"size": 10, "color": color, "face": font["face"]},
                                  "bold": {"color": "#0f172a", "size": 14, "face": font["face"]}},
                            shapeProperties={"borderRadius": 8,
                                             "borderDashes": [6, 3] if node["origin"] == "custom" else False},
                            shadow={"enabled": True, "color": "rgba(15,23,42,0.08)", "size": 8, "x": 0, "y": 2},
                            opacity=0.5 if d.get("ghost") else 1)
        elif kind == "codelist":
            node.update(shape="diamond", size=7, borderWidth=1,
                        color={"background": "#fff" if not d.get("missing") else "#fee2e2",
                               "border": "#94a3b8" if not d.get("missing") else "#dc2626"},
                        font={**font, "size": 10, "color": "#64748b", "vadjust": -2})
        elif kind == "story":
            node.update(shape="dot", size=7, color={"background": "#f59e0b", "border": "#d97706"},
                        font={**font, "size": 10, "color": "#92400e"})
        else:
            node.update(shape="box", color={"background": "#fff", "border": "#dc2626"},
                        font={"color": "#dc2626"}, shapeProperties={"borderDashes": [4, 3]})
        nodes.append(node)
    for i, (u, v, d) in enumerate(g.edges(data=True)):
        st = EDGE_STYLE.get(d["kind"], EDGE_STYLE["association"])
        if d.get("deprecated"):
            st = {**st, "color": "#cbd5e1", "dashes": [2, 3]}
        lbl = d["label"] if d["kind"] not in ("codelist", "story") else ""
        if d.get("cardinality") and d["kind"] in ("composition", "association", "reference"):
            lbl = f"{lbl} [{d['cardinality']}]"
        faint = d["kind"] in ("codelist", "story")
        edges.append({"id": i, "from": u, "to": v, "label": lbl, "kind": d["kind"],
                      "title": f"{d['kind']}: {d['label']} {d.get('cardinality', '')}",
                      "color": {"color": st["color"], "opacity": 0.22 if faint else 0.75,
                                "highlight": st["color"], "hover": st["color"]},
                      "width": 1 if faint else 1.6, "dashes": st["dashes"],
                      "arrows": {"to": {"enabled": True, "scaleFactor": 0.35 if faint else 0.55}},
                      "font": {"size": 10, "color": "#475569", "strokeWidth": 4, "strokeColor": "#ffffff",
                               "face": "Inter, ui-sans-serif, system-ui, sans-serif"}})

    # details for the side panel
    usage: dict[tuple[str, str], list[str]] = {}
    for s in model.stories.values():
        for ea in s.used_attributes:
            usage.setdefault(ea, []).append(s.id)
    details = {}
    for e in model.entities.values():
        inherited = {k: v for k, v in e.all_attributes(model).items() if k not in e.attributes}
        details[f"E:{e.name}"] = {
            "type": "entity", "name": e.name, "domain": e.domain, "description": e.description,
            "parents": e.parents, "source": e.source, "dep": e.deprecated,
            "origin": e.origin, "domainSource": e.domain_source, "usedIn": sorted(e.used_in_domains),
            "notes": e.notes,
            "attributes": [{
                "name": a.name, "type": a.type + ("[]" if a.is_array else ""), "req": a.required,
                "card": a.cardinality, "ref": a.ref_entity, "codelist": a.codelist,
                "enum": a.enum[:20], "desc": a.description,
                "inherited": a.name in inherited, "dep": a.deprecated,
                "origin": a.extra.get("origin", ""), "changes": a.extra.get("changes", []),
                "stories": sorted(usage.get((e.name, a.name), [])),
            } for a in list(inherited.values()) + list(e.attributes.values())],
            "incoming": sorted({f"{r.source}.{r.name}" for r in model.relationships() if r.target == e.name}),
        }
    for c in model.codelists.values():
        details[f"C:{c.name}"] = {"type": "codelist", "name": c.name, "description": c.description,
                                  "source": c.source,
                                  "dep": c.deprecated,
                                  "values": [[v.code, v.description, v.deprecated] for v in c.values[:500]],
                                  "usedBy": sorted(f"{e.name}.{a.name}" for e in model.entities.values()
                                                   for a in e.attributes.values() if a.codelist == c.name)}
    for s in model.stories.values():
        details[f"S:{s.id}"] = {"type": "story", "name": s.id, "title": s.title, "domain": s.domain,
                                "description": s.description, "source": s.source,
                                "criteria": s.acceptance_criteria, "entities": sorted(s.used_entities),
                                "declared": s.declared_entities, "samples": len(s.samples),
                                "findings": [[f.level, f.path, f.message] for f in s.findings if f.level != "ok"][:200]}
    return {"nodes": nodes, "edges": edges, "details": details,
            "domains": [[d, colors.get(d, "#64748b"), sum(1 for e in model.entities.values() if e.domain == d)]
                        for d in model.domains()],
            "removed": {"entities": len(model.removed_entities), "attributes": len(model.removed_attributes),
                        "codelists": len(model.removed_codelists),
                        "codes": sum(len(x) for x in model.removed_codes.values())},
            "big": sum(1 for n in nodes if n["kind"] == "entity") > 80 or sum(1 for n in nodes if n["kind"] == "codelist") > 60,
            "stats": {"entities": len(model.entities), "codelists": len(model.codelists),
                      "stories": len(model.stories), "relationships": len(model.relationships())}}


def _vis_js() -> str:
    local = Path(__file__).parent / "static" / "vis-network.min.js"
    if local.exists():
        return f"<script>{local.read_text(encoding='utf-8')}</script>"
    return '<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>'


def to_html(model: AcordModel, g: nx.MultiDiGraph, title: str = "ACORD Canonical Model Graph") -> str:
    tpl = (Path(__file__).parent / "static" / "template.html").read_text(encoding="utf-8")
    data = json.dumps(_payload(model, g)).replace("</", "<\\/")
    return (tpl.replace("{{TITLE}}", title).replace("{{VIS_JS}}", _vis_js())
               .replace("{{DATA}}", data))


def to_graphml(g: nx.MultiDiGraph, path: str | Path) -> None:
    h = nx.MultiDiGraph()
    for n, d in g.nodes(data=True):
        h.add_node(n, **{k: (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in d.items()})
    for u, v, d in g.edges(data=True):
        h.add_edge(u, v, **{k: str(x) for k, x in d.items()})
    nx.write_graphml(h, path)


def to_json(g: nx.MultiDiGraph) -> dict:
    try:
        return nx.node_link_data(g, edges="links")
    except TypeError:  # older networkx
        return nx.node_link_data(g)
