"""Cross-reference the loaded model.

1. Resolve each attribute to its code list (explicit link first, then name matching).
2. Flag dangling references (entity or code list that is referenced but not defined).
3. Walk every user-story sample payload against the entity model:
   which entities/attributes the story touches, unknown fields, missing required
   fields, code values that are not in the code list, and basic type mismatches.
"""
from __future__ import annotations

import re
from typing import Any

from .loader import norm
from .model import AcordModel, Attribute, Entity, SampleFinding, UserStory

ROOT_HINT_KEYS = {"entity", "entityType", "objectType"}
CODE_SUFFIXES =("cd", "code", "typecd", "type", "status", "statuscd", "ind")


def _codelist_index(model: AcordModel) -> dict[str, str]:
    idx = {}
    for name in model.codelists:
        n = norm(name)
        idx[n] = name
        for suf in ("codelist", "list", "codes", "enum"):
            if n.endswith(suf) and len(n) > len(suf):
                idx.setdefault(n[: -len(suf)], name)
    return idx


def resolve_codelists(model: AcordModel) -> list[str]:
    """Link attributes to code lists. Returns human-readable notes."""
    notes = []
    idx = _codelist_index(model)
    for ent in model.entities.values():
        for attr in ent.attributes.values():
            if attr.ref_entity and attr.ref_entity not in model.entities and norm(attr.ref_entity) in idx:
                attr.codelist, attr.ref_entity, attr.type = idx[norm(attr.ref_entity)], "", "code"
                attr.extra["codelist_match"] = "$ref"
            if attr.ref_entity:
                continue
            if attr.codelist:
                hit = idx.get(norm(attr.codelist))
                if hit:
                    attr.codelist = hit
                else:
                    notes.append(f"{ent.name}.{attr.name}: code list '{attr.codelist}' is not in the loaded code-list files")
                continue
            n = norm(attr.name)
            candidates = [n, norm(ent.name) + n]
            if any(n.endswith(s) for s in CODE_SUFFIXES):
                candidates += [n + "cd", re.sub(r"(code)$", "cd", n), re.sub(r"cd$", "code", n)]
            for c in candidates:
                if c in idx:
                    attr.codelist = idx[c]
                    attr.extra["codelist_match"] = "by-name"
                    break
    return notes


def apply_deprecations(model: AcordModel, include_deprecated: bool = False) -> list[str]:
    """Mark items named in the deprecation list, then (unless include_deprecated)
    take every deprecated entity / attribute / code list / code value out of the model.
    Returns a list of what was excluded (or flagged)."""
    dl = {norm(x) for x in model.deprecation_list}
    for e in model.entities.values():
        if norm(e.name) in dl:
            e.deprecated = True
        for a in e.attributes.values():
            if norm(f"{e.name}.{a.name}") in dl:
                a.deprecated = True
    for c in model.codelists.values():
        if norm(c.name) in dl:
            c.deprecated = True
        for v in c.values:
            if norm(f"{c.name}={v.code}") in dl:
                v.deprecated = True

    dep_ents = {n for n, e in model.entities.items() if e.deprecated}
    dep_cls = {n for n, c in model.codelists.items() if c.deprecated}
    out: list[str] = [f"Entity {n}" for n in sorted(dep_ents)]
    for e in model.entities.values():
        for a in e.attributes.values():
            if a.deprecated:
                out.append(f"Attribute {e.name}.{a.name}")
            elif a.ref_entity in dep_ents:
                a.deprecated = True
                out.append(f"Attribute {e.name}.{a.name} (points to deprecated {a.ref_entity})")
    out += [f"Code list {n}" for n in sorted(dep_cls)]
    for c in model.codelists.values():
        out += [f"Code {c.name}={v.code}" for v in c.values if v.deprecated and c.name not in dep_cls]
    if include_deprecated:
        return out

    for n in dep_ents:
        model.removed_entities[n] = model.entities.pop(n)
    for e in list(model.entities.values()) + list(model.removed_entities.values()):
        for an in [k for k, a in e.attributes.items() if a.deprecated]:
            model.removed_attributes[(e.name, an)] = e.attributes.pop(an)
        for a in e.attributes.values():
            if a.codelist in dep_cls:
                a.extra["deprecated_codelist"] = a.codelist
                a.codelist = ""
    for n in dep_cls:
        model.removed_codelists[n] = model.codelists.pop(n)
    for c in model.codelists.values():
        gone = {v.code for v in c.values if v.deprecated}
        if gone:
            model.removed_codes[c.name] = gone
            c.values = [v for v in c.values if not v.deprecated]
    model.extra_relationships = [r for r in model.extra_relationships
                                 if r.source not in dep_ents and r.target not in dep_ents]
    return out


CUSTOM_AFFIX = re.compile(r"^(custom|cust|ext|extended|my|client|carrier|x)[_-]?|[_-]?(custom|ext|extension|extended|ext\d+|v\d+)$", re.I)


def link_custom_entities(model: AcordModel) -> list[str]:
    """A custom entity with no parent whose name is a standard name plus an affix
    (CustomAddress, Address_Ext, AddressExtension) is linked to it as 'extends'."""
    notes = []
    std = {norm(n): n for n, e in model.entities.items() if e.origin != "custom"}
    for e in model.entities.values():
        if e.origin != "custom" or e.parents:
            continue
        base = CUSTOM_AFFIX.sub("", e.name)
        base = CUSTOM_AFFIX.sub("", base)
        if base != e.name and norm(base) in std:
            e.parents.append(std[norm(base)])
            e.notes.append(f"matched to standard {std[norm(base)]} by name")
            notes.append(f"{e.name} → extends {std[norm(base)]} (matched by name)")
    return notes


def resolve_domains(model: AcordModel) -> list[str]:
    """Home domain for every entity + the set of domains it is used in.
    Order: declared → file/folder → parent (inherited) → referencing entities (inferred) → Shared."""
    notes = []
    incoming: dict[str, set[str]] = {}
    rels = model.relationships()

    def known(e) -> bool:
        return e.domain_source in ("declared", "file", "inherited", "inferred") and e.domain != "Unassigned"

    # inherit from parents (repeat until stable, parents may resolve late)
    for _ in range(5):
        changed = False
        for e in model.entities.values():
            if known(e):
                continue
            for p in e.parents:
                pe = model.entities.get(p) or model.removed_entities.get(p)
                if pe and known(pe):
                    e.domain, e.domain_source, changed = pe.domain, "inherited", True
                    break
        if not changed:
            break
    for _ in range(5):
        changed = False
        for r in rels:
            if r.rel_type == "inheritance" or r.source not in model.entities:
                continue
            incoming.setdefault(r.target, set())
            src = model.entities[r.source]
            if known(src):
                incoming[r.target].add(src.domain)
        for e in model.entities.values():
            if known(e) or e.domain_source == "shared" or not incoming.get(e.name):
                continue
            doms = incoming[e.name]
            if len(doms) == 1:
                e.domain, e.domain_source = next(iter(doms)), "inferred"
            else:
                e.domain, e.domain_source = "Shared", "shared"
                notes.append(f"{e.name}: no domain declared and used by {len(doms)} domains ({', '.join(sorted(doms))}) → Shared")
            changed = True
        if not changed:
            break
    for e in model.entities.values():
        if e.domain_source == "" and e.domain == "Unassigned":
            notes.append(f"{e.name}: no domain declared and nothing points to it → Unassigned")
        e.used_in_domains = ({e.domain} - {"Shared"}) | {d for d in incoming.get(e.name, set()) if d != "Unassigned"}
    for c in model.codelists.values():
        users = [en.domain for en in model.entities.values() for a in en.attributes.values() if a.codelist == c.name]
        if users:
            c.domain = max(set(users), key=users.count)
    return notes


def dangling_refs(model: AcordModel) -> list[str]:
    out = []
    for r in model.relationships():
        if r.target not in model.entities:
            out.append(f"{r.source}.{r.name} -> '{r.target}' is referenced but not defined")
    return out


# ---------------------------------------------------------------------------
# Sample mapping
# ---------------------------------------------------------------------------
JSON_TYPES = {
    "string": (str,), "code": (str,), "date": (str,), "datetime": (str,),
    "integer": (int,), "number": (int, float), "decimal": (int, float), "amount": (int, float, str),
    "boolean": (bool,), "object": (dict,), "array": (list,),
}


def _entity_index(model: AcordModel) -> dict[str, str]:
    return {norm(n): n for n in model.entities}


def guess_root(model: AcordModel, sample: dict, story: UserStory) -> tuple[str | None, dict]:
    """Find the entity a sample payload represents and the dict to walk."""
    eidx = _entity_index(model)
    # explicit hints inside the sample
    for k in ("entity", "entityType", "objectType", "@type", "_type", "$schema"):
        v = sample.get(k)
        if isinstance(v, str) and norm(v.split("/")[-1].replace(".json", "")) in eidx:
            return eidx[norm(v.split("/")[-1].replace(".json", ""))], sample
    # wrapper: {"Policy": {...}}
    if len(sample) == 1:
        (k, v), = sample.items()
        if norm(k) in eidx and isinstance(v, (dict, list)):
            return eidx[norm(k)], (v[0] if isinstance(v, list) and v and isinstance(v[0], dict) else v)
    # best overlap among declared entities first, then all entities
    keys = {norm(k) for k in sample}
    pool = [e for e in story.declared_entities if e in model.entities] or list(model.entities)
    best, score = None, 0.0
    for name in pool:
        attrs = {norm(a) for a in model.entities[name].all_attributes(model)}
        if not attrs:
            continue
        s = len(keys & attrs) / max(len(keys), 1)
        if s > score:
            best, score = name, s
    return (best, sample) if score >= 0.3 else (None, sample)


def _check_value(path: str, ent: Entity, attr: Attribute, value: Any, model: AcordModel,
                 story: UserStory, depth: int) -> None:
    f = story.findings
    if attr.is_array and not isinstance(value, list):
        f.append(SampleFinding(path, "warning", f"expected an array ({attr.cardinality})", ent.name, attr.name))
    values = value if isinstance(value, list) else [value]
    for i, v in enumerate(values):
        p = f"{path}[{i}]" if isinstance(value, list) else path
        if attr.ref_entity:
            if isinstance(v, dict) and attr.ref_entity in model.entities:
                walk(model, model.entities[attr.ref_entity], v, p, story, depth + 1)
            elif isinstance(v, str):
                f.append(SampleFinding(p, "ok", f"reference by id to {attr.ref_entity}", ent.name, attr.name))
            continue
        dep_codes = model.removed_codes.get(attr.codelist, set())
        if v is not None and str(v) in dep_codes:
            f.append(SampleFinding(p, "warning", f"code '{v}' is deprecated in {attr.codelist}", ent.name, attr.name))
            continue
        allowed = set(attr.enum)
        if attr.codelist and attr.codelist in model.codelists:
            allowed |= model.codelists[attr.codelist].codes
        if allowed and v is not None and str(v) not in allowed:
            src = f"code list {attr.codelist}" if attr.codelist else "enum"
            f.append(SampleFinding(p, "error", f"value '{v}' not in {src}", ent.name, attr.name))
        t = attr.type.lower().split("/")[0]
        if t in JSON_TYPES and v is not None and not isinstance(v, JSON_TYPES[t]):
            numeric_string = t in ("integer", "number", "decimal") and isinstance(v, str) \
                and re.fullmatch(r"-?\d+(\.\d+)?", v)
            if not numeric_string:
                f.append(SampleFinding(p, "warning", f"type {type(v).__name__} but model says {attr.type}", ent.name, attr.name))
        if attr.max_length and isinstance(v, str) and len(v) > int(attr.max_length):
            f.append(SampleFinding(p, "warning", f"length {len(v)} > maxLength {attr.max_length}", ent.name, attr.name))


def walk(model: AcordModel, ent: Entity, data: dict, path: str, story: UserStory, depth: int = 0) -> None:
    if depth > 25:
        return
    story.used_entities.add(ent.name)
    attrs = ent.all_attributes(model)
    by_norm = {norm(k): a for k, a in attrs.items()}
    eidx = _entity_index(model)
    present = set()
    for key, value in data.items():
        if key.startswith(("@", "$", "_")) or (depth == 0 and key in ROOT_HINT_KEYS):
            continue
        attr = attrs.get(key) or by_norm.get(norm(key))
        p = f"{path}.{key}"
        if attr is None:
            owners = [ent.name] + ent.parents
            if any((o, key) in model.removed_attributes for o in owners):
                story.findings.append(SampleFinding(p, "warning", f"'{key}' is deprecated on {ent.name}; remove it from the sample", ent.name, key))
                continue
            # nested object whose key is an entity name -> implicit relationship
            if norm(key) in eidx and isinstance(value, (dict, list)):
                tgt = model.entities[eidx[norm(key)]]
                story.findings.append(SampleFinding(p, "warning", f"'{key}' is an entity but {ent.name} has no attribute for it (implicit association)", ent.name, key))
                for i, v in enumerate(value if isinstance(value, list) else [value]):
                    if isinstance(v, dict):
                        walk(model, tgt, v, p if isinstance(value, dict) else f"{p}[{i}]", story, depth + 1)
            else:
                story.findings.append(SampleFinding(p, "error", f"'{key}' is not an attribute of {ent.name}", ent.name, key))
            continue
        present.add(attr.name)
        story.used_attributes.add((ent.name, attr.name))
        _check_value(p, ent, attr, value, model, story, depth)
    for a in attrs.values():
        if a.required and a.name not in present:
            story.findings.append(SampleFinding(f"{path}.{a.name}", "warning", f"required attribute missing in sample", ent.name, a.name))


def map_stories(model: AcordModel) -> None:
    for story in model.stories.values():
        story.used_entities.clear(); story.used_attributes.clear(); story.findings.clear()
        for e in story.declared_entities:
            if e not in model.entities:
                story.findings.append(SampleFinding("(declared)", "error", f"story lists entity '{e}' which is not defined", e))
        roots = []
        for i, sample in enumerate(story.samples):
            root, body = guess_root(model, sample, story)
            if not root:
                story.findings.append(SampleFinding(f"sample[{i}]", "error", "could not match this sample to any entity"))
                continue
            roots.append(root)
            walk(model, model.entities[root], body, f"sample[{i}]:{root}", story)
        if not story.domain:
            # the domain of the sample's root entity wins; otherwise the declared/used majority
            pool = roots or story.declared_entities[:1] or list(story.used_entities)
            doms = [model.entities[e].domain for e in pool if e in model.entities]
            story.domain = max(doms, key=doms.count) if doms else "Unassigned"


def analyze(model: AcordModel, include_deprecated: bool = False) -> dict[str, list[str]]:
    notes = resolve_codelists(model)
    deprecated = apply_deprecations(model, include_deprecated)
    custom_notes = link_custom_entities(model)
    domain_notes = resolve_domains(model)
    for e in model.entities.values():
        for a in e.attributes.values():
            if a.extra.get("deprecated_codelist"):
                notes.append(f"{e.name}.{a.name}: code list '{a.extra['deprecated_codelist']}' is deprecated (link removed)")
    map_stories(model)
    return {"deprecated": deprecated, "custom": custom_notes + custom_changes(model),
            "domains": domain_notes, "codelist_notes": notes, "dangling": dangling_refs(model)}


def custom_changes(model: AcordModel) -> list[str]:
    out = []
    for e in sorted(model.entities.values(), key=lambda x: x.name):
        if e.origin == "custom" and not any("matched" in n for n in e.notes):
            out.append(f"{e.name}: new custom entity in {e.domain} ({e.domain_source or 'unassigned'})")
        for a in e.attributes.values():
            o = a.extra.get("origin")
            if e.origin == "extended" and o == "custom":
                out.append(f"{e.name}.{a.name}: custom attribute added")
            elif o == "changed":
                out.append(f"{e.name}.{a.name}: changed ({'; '.join(a.extra['changes'])})")
        out += [f"{e.name}: {n}" for n in e.notes if "matched" not in n]
    return out


def attribute_rows(model: AcordModel) -> list[dict]:
    """Flat mapping sheet: one row per entity attribute (incl. story usage)."""
    usage: dict[tuple[str, str], list[str]] = {}
    for s in model.stories.values():
        for ea in s.used_attributes:
            usage.setdefault(ea, []).append(s.id)
    rows = []
    for ent in sorted(model.entities.values(), key=lambda e: (e.domain, e.name)):
        for a in ent.attributes.values():
            cl = model.codelists.get(a.codelist)
            rows.append({
                "Domain": ent.domain, "Entity": ent.name, "Attribute": a.name, "Kind": a.kind,
                "Type": a.type, "Required": "Y" if a.required else "N", "Cardinality": a.cardinality,
                "Related Entity": a.ref_entity, "Code List": a.codelist,
                "Code List Match": a.extra.get("codelist_match", "explicit" if a.codelist else ""),
                "Code Values": ", ".join(sorted(cl.codes))[:300] if cl else ", ".join(a.enum),
                "Deprecated": "Y" if (a.deprecated or ent.deprecated) else "",
                "Origin": a.extra.get("origin", "custom" if ent.origin == "custom" else "standard"),
                "Change": "; ".join(a.extra.get("changes", [])),
                "Domain Source": ent.domain_source, "Used In Domains": ", ".join(sorted(ent.used_in_domains)),
                "Description": a.description, "Used In Stories": ", ".join(sorted(usage.get((ent.name, a.name), []))),
            })
    return rows
