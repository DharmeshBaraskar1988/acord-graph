"""Company model ↔ ACORD mapping page (mapping.html)."""
from __future__ import annotations

import json
from pathlib import Path

from .graph import _tint, _vis_js, domain_colors
from .matcher import EntityMapping
from .model import AcordModel

CUSTOM_COLOR = "#f97316"          # reserved colour for company / custom components
STATUS_COLOR = {"MATCHED": "#16a34a", "PARTIAL": "#d97706", "UNMATCHED": "#dc2626"}
FONT = "Inter, ui-sans-serif, system-ui, sans-serif"


def mapping_payload(model: AcordModel, maps: list[EntityMapping]) -> dict:
    colors = domain_colors(model)
    company = model.company_entities
    max_lvl = max((m.level for m in maps), default=0)
    L_ALIGN, L_REL, L_UC = max_lvl + 1, max_lvl + 2, max_lvl + 3
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def acord_node(name: str, level: int, role: str) -> None:
        nid = f"A:{name}"
        if nid in nodes:
            if level < nodes[nid]["level"]:
                nodes[nid]["level"], nodes[nid]["role"] = level, role
            return
        e = model.entities.get(name)
        dom = e.domain if e else "Unassigned"
        c = colors.get(dom, "#64748b")
        nodes[nid] = {"id": nid, "label": name, "level": level, "kind": "acord", "role": role, "domain": dom,
                      "shape": "box", "margin": {"top": 6, "bottom": 6, "left": 10, "right": 10},
                      "color": {"background": _tint(c, 0.9), "border": c,
                                "highlight": {"background": _tint(c, 0.75), "border": c}},
                      "font": {"face": FONT, "size": 12, "color": "#0f172a"},
                      "shapeProperties": {"borderRadius": 6}, "borderWidth": 1.2}

    for m in maps:
        nid = f"K:{m.company}"
        sc = STATUS_COLOR[m.status]
        unmatched = m.n_attrs - m.n_matched
        nodes[nid] = {
            "id": nid, "kind": "company", "level": m.level, "status": m.status,
            "label": f"<b>{m.company}</b>\n<i>{m.n_matched}/{m.n_attrs} attr · {m.status.lower()}</i>",
            "shape": "box", "margin": {"top": 8, "bottom": 8, "left": 12, "right": 12},
            "color": {"background": _tint(CUSTOM_COLOR, 0.86), "border": sc,
                      "highlight": {"background": _tint(CUSTOM_COLOR, 0.7), "border": sc}},
            "borderWidth": 2, "shapeProperties": {"borderRadius": 8},
            "font": {"face": FONT, "multi": "html", "size": 13, "color": "#431407",
                     "bold": {"face": FONT, "size": 13, "color": "#431407"},
                     "ital": {"face": FONT, "size": 10, "color": sc}},
            "shadow": {"enabled": True, "color": "rgba(15,23,42,.08)", "size": 6, "y": 2},
            "notFull": unmatched > 0}
        if m.aligned:
            acord_node(m.aligned, L_ALIGN, "aligned")
            edges.append({"from": nid, "to": f"A:{m.aligned}", "kind": "align",
                          "label": f"{m.similarity * 100:.0f}%", "dashes": [6, 4], "width": 2,
                          "color": {"color": CUSTOM_COLOR}})
        for alt, s in m.alternatives:
            acord_node(alt, L_ALIGN, "alternative")
            edges.append({"from": nid, "to": f"A:{alt}", "kind": "alt", "label": f"{s * 100:.0f}%",
                          "dashes": [2, 5], "width": 1, "color": {"color": "#94a3b8"}})
        for tgt, n in m.targets().items():
            acord_node(tgt, L_ALIGN, "target")
            edges.append({"from": nid, "to": f"A:{tgt}", "kind": "attr", "label": f"{n} attr",
                          "width": 1.5 + min(n, 6) * 0.5, "color": {"color": "#7c3aed"}})
        ent = company.get(m.company)
        for a in (ent.attributes.values() if ent else []):
            if a.ref_entity and a.ref_entity in company:
                edges.append({"from": nid, "to": f"K:{a.ref_entity}", "kind": "company",
                              "label": a.name + (" []" if a.is_array else ""), "width": 1.4,
                              "color": {"color": "#475569"}})

    # ACORD relations around the aligned / target entities
    core = {n["id"][2:] for n in nodes.values() if n["kind"] == "acord"}
    for r in model.relationships():
        if r.rel_type == "inheritance":
            continue
        if r.source in core or r.target in core:
            for x in (r.source, r.target):
                if x in model.entities and model.entities[x].origin != "custom":
                    acord_node(x, L_REL, "related")
            if f"A:{r.source}" in nodes and f"A:{r.target}" in nodes:
                edges.append({"from": f"A:{r.source}", "to": f"A:{r.target}", "kind": "acord",
                              "label": r.name, "width": 1, "color": {"color": "#16a34a"}})
    # use cases touching the shown ACORD entities
    for s in model.stories.values():
        used = [e for e in (s.used_entities or set(s.declared_entities)) if f"A:{e}" in nodes]
        if not used:
            continue
        sid = f"S:{s.id}"
        nodes[sid] = {"id": sid, "label": s.id, "level": L_UC, "kind": "usecase", "title": s.title,
                      "shape": "dot", "size": 7, "color": {"background": "#eab308", "border": "#a16207"},
                      "font": {"face": FONT, "size": 11, "color": "#713f12"}}
        for e in used:
            edges.append({"from": sid, "to": f"A:{e}", "kind": "usecase", "label": "", "dashes": [4, 4],
                          "width": 1, "color": {"color": "#eab308"}})

    for i, e in enumerate(edges):
        e.update(id=i, arrows={"to": {"enabled": True, "scaleFactor": 0.45}},
                 font={"face": FONT, "size": 10, "color": "#475569", "strokeWidth": 4, "strokeColor": "#fff"})
        e["_label"] = e["label"]

    summary = [{"company": m.company, "aligned": m.aligned, "status": m.status,
                "similarity": round(m.similarity * 100, 1), "matched": m.n_matched, "total": m.n_attrs,
                "targets": m.targets(), "alternatives": m.alternatives, "level": m.level,
                "attrs": [{"c": a.company_attr, "ct": a.company_type, "e": a.acord_entity, "a": a.acord_attr,
                           "at": a.acord_type, "s": round(a.score * 100), "via": a.via, "how": a.how,
                           "p": a.path, "cd": a.company_desc, "ad": a.acord_desc, "r": a.reason,
                           "ne": a.near_entity, "na": a.near_attr, "ns": round(a.near_score * 100)}
                          for a in m.attrs]} for m in maps]
    acord_attrs = {}
    for m in maps:
        for n in [m.aligned] + list(m.targets()):
            if n and n in model.entities and n not in acord_attrs:
                acord_attrs[n] = [{"n": a.name, "t": a.type, "d": a.description, "req": a.required}
                                  for a in model.entities[n].all_attributes(model).values() if not a.ref_entity]
    columns = [[lvl, "Company model" if lvl == 0 else f"Company level {lvl}"] for lvl in range(max_lvl + 1)]
    columns += [[L_ALIGN, "ACORD aligned / attribute targets"], [L_REL, "ACORD related"], [L_UC, "Use cases"]]
    return {"nodes": list(nodes.values()), "edges": edges, "summary": summary, "columns": columns,
            "acordAttrs": acord_attrs,
            "customColor": CUSTOM_COLOR, "statusColor": STATUS_COLOR,
            "counts": {"company": len(maps), "acord": sum(1 for n in nodes.values() if n["kind"] == "acord"),
                       "usecases": sum(1 for n in nodes.values() if n["kind"] == "usecase")}}


def to_mapping_html(model: AcordModel, maps: list[EntityMapping], title: str = "Company model ↔ ACORD") -> str:
    tpl = (Path(__file__).parent / "static" / "mapping.html").read_text(encoding="utf-8")
    data = json.dumps(mapping_payload(model, maps)).replace("</", "<\\/")
    return tpl.replace("{{TITLE}}", title).replace("{{VIS_JS}}", _vis_js()).replace("{{DATA}}", data)
