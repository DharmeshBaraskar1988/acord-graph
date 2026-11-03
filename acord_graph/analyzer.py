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
        return e.domain_source in ("declared", "file", "tag", "inherited", "inferred", "cluster", "mapped",
                                   "stories", "propagated") \
            and e.domain != "Unassigned"

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


def _name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name)]


def cluster_domains(model: AcordModel, min_share: float = 0.4, min_count: int = 12, force: bool = False) -> list[str]:
    """When most entities have no domain, group them by how they are connected (Louvain communities)
    and name each group after its most common leading word (Claim, Policy, Party ...)."""
    import networkx as nx
    ents = model.entities
    loose = [n for n, e in ents.items() if e.domain in ("Unassigned", "Shared") and e.domain_source in ("", "shared")]
    if len(loose) < (6 if force else min_count) or (not force and len(loose) / max(len(ents), 1) < min_share):
        return []
    g = nx.Graph()
    g.add_nodes_from(loose)
    for r in model.relationships():
        if r.rel_type != "inheritance" and r.source in g and r.target in g and r.source != r.target:
            g.add_edge(r.source, r.target)
    # very generic hubs (referenced from everywhere) would glue all groups together
    deg = dict(g.degree())
    cutoff = max(12, sorted(deg.values())[int(len(deg) * 0.97)] if deg else 12)
    core = g.copy()
    core.remove_nodes_from([n for n, d in deg.items() if d > cutoff])
    comms = [set(c) for c in nx.community.louvain_communities(core, seed=7, resolution=1.0)]
    # attach tiny groups / hubs to the group they connect to most
    big = [c for c in comms if len(c) >= 3]
    rest = set(loose) - set().union(*big) if big else set(loose)
    for n in rest:
        best = max(big, key=lambda c: sum(1 for m in g.neighbors(n) if m in c), default=None)
        if best is not None and any(m in best for m in g.neighbors(n)):
            best.add(n)
    groups = big
    leftovers = set(loose) - set().union(*groups) if groups else set(loose)
    if leftovers:
        groups.append(leftovers)
    used, notes = set(), []
    for c in sorted(groups, key=len, reverse=True):
        words = [(_name_words(n) or [n])[0] for n in c]
        indeg = {n: sum(1 for r in model.relationships() if r.target == n and r.source in c) for n in c}
        # most shared leading word; ties go to the word whose entity is referenced most
        top = max(set(words), key=lambda w: (words.count(w), indeg.get(w, 0), -len(w)))
        hub = max(c, key=lambda n: (indeg[n], -len(n)))
        name = top if words.count(top) >= 2 else hub
        if c is leftovers and len(groups) > 1:
            name = "Other"
        base, i = name, 2
        while name in used:
            name, i = f"{base} {i}", i + 1
        used.add(name)
        for n in c:
            ents[n].domain, ents[n].domain_source = name, "cluster"
        notes.append(f"{name}: {len(c)} entities grouped by their relationships (no domain in the files)")
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


def _dom_key(name: str) -> str:
    n = norm(name)
    return n[:-1] if n.endswith("s") and len(n) > 3 else n


def walk_stories(model: AcordModel) -> None:
    """Walk every sample payload through the model: which entities / attributes each story uses."""
    for story in model.stories.values():
        story.used_entities.clear(); story.used_attributes.clear(); story.findings.clear(); story.roots.clear()
        for e in story.declared_entities:
            if e not in model.entities:
                story.findings.append(SampleFinding("(declared)", "error", f"story lists entity '{e}' which is not defined", e))
        for i, sample in enumerate(story.samples):
            root, body = guess_root(model, sample, story)
            if not root:
                story.findings.append(SampleFinding(f"sample[{i}]", "error", "could not match this sample to any entity"))
                continue
            story.roots.append(root)
            walk(model, model.entities[root], body, f"sample[{i}]:{root}", story)


def _story_domain_names(model: AcordModel) -> dict[str, str]:
    """One spelling per story domain (Claims / claims / Claim -> the model's or the most common one)."""
    model_doms = {_dom_key(d): d for d in model.domains() if d not in ("Unassigned", "Shared")}
    seen: dict[str, list[str]] = {}
    for s in model.stories.values():
        if s.domain and s.domain_source in ("declared", "folder"):
            seen.setdefault(_dom_key(s.domain), []).append(s.domain)
    return {k: model_doms.get(k) or max(set(v), key=v.count) for k, v in seen.items()}


def story_vote_domains(model: AcordModel) -> list[str]:
    """Entities without a domain of their own get the domain of the user stories that use them.
    Each story votes for its domain: 3 for the entity its sample is about (the root), 1 for every other
    entity the sample touches. The story domain comes from the story itself or its folder."""
    names = _story_domain_names(model)
    for e in model.entities.values():
        e.story_votes = {}
    for s in model.stories.values():
        if not (s.domain and s.domain_source in ("declared", "folder")):
            continue
        dom = names.get(_dom_key(s.domain), s.domain)
        s.domain = dom
        for e in s.used_entities | set(s.declared_entities):
            if e in model.entities:
                w = 3.0 if e in s.roots or e in s.declared_entities else 1.0
                v = model.entities[e].story_votes
                v[dom] = v.get(dom, 0) + w
    notes = []
    for e in model.entities.values():
        if not e.story_votes or (e.domain_source in ("declared", "file", "tag", "inherited") and e.domain != "Unassigned"):
            continue
        top = max(e.story_votes, key=lambda d: (e.story_votes[d], d))
        e.domain, e.domain_source = top, "stories"
        others = [d for d in e.story_votes if d != top]
        notes.append(f"{e.name} → {top} (used by {top} stories" + (f"; also by {', '.join(sorted(others))}" if others else "") + ")")
    return notes


def propagate_domains(model: AcordModel, rounds: int = 6) -> int:
    """Entities still without a domain take the majority domain of the entities they are connected to
    (only if those neighbours got their domain from a reliable source)."""
    reliable = ("declared", "tag", "inherited", "stories", "file", "propagated")
    nbrs: dict[str, list[str]] = {n: [] for n in model.entities}
    for r in model.relationships():
        if r.rel_type != "inheritance" and r.source in nbrs and r.target in nbrs:
            nbrs[r.source].append(r.target); nbrs[r.target].append(r.source)
    total = 0
    for _ in range(rounds):
        changes = {}
        for n, e in model.entities.items():
            if e.domain not in ("Unassigned", "Shared") or e.domain_source in reliable:
                continue
            doms = [model.entities[m].domain for m in nbrs[n]
                    if model.entities[m].domain_source in reliable and model.entities[m].domain not in ("Unassigned", "Shared")]
            if doms:
                top = max(set(doms), key=lambda d: (doms.count(d), d))
                # join only when at least half of ALL its connections are in that domain
                if doms.count(top) * 2 >= len(set(nbrs[n])):
                    changes[n] = top
        if not changes:
            break
        for n, d in changes.items():
            model.entities[n].domain, model.entities[n].domain_source = d, "propagated"
        total += len(changes)
    return total


def finalize_story_domains(model: AcordModel) -> None:
    known = {_dom_key(d): d for d in model.domains()}
    for story in model.stories.values():
        pool = story.roots or story.declared_entities[:1] or list(story.used_entities)
        doms = [model.entities[e].domain for e in pool if e in model.entities]
        ent_dom = max(doms, key=doms.count) if doms else ""
        if story.domain_source in ("folder", "declared") and story.domain:
            hit = known.get(_dom_key(story.domain))
            if hit:
                story.domain = hit
            elif story.domain_source == "folder" and ent_dom and ent_dom not in ("Unassigned",):
                story.domain, story.domain_source = ent_dom, "entities"
        if not story.domain:
            story.domain = ent_dom or "Unassigned"
            story.domain_source = "entities" if ent_dom else ""
    # stories also count as 'used in' for every entity they touch
    for story in model.stories.values():
        for e in story.used_entities:
            if e in model.entities and story.domain not in ("Unassigned", ""):
                model.entities[e].used_in_domains.add(story.domain)


def map_stories(model: AcordModel) -> None:
    walk_stories(model)
    finalize_story_domains(model)


def analyze(model: AcordModel, include_deprecated: bool = False) -> dict[str, list[str]]:
    notes = resolve_codelists(model)
    deprecated = apply_deprecations(model, include_deprecated)
    custom_notes = link_custom_entities(model)
    domain_notes = resolve_domains(model)
    walk_stories(model)                                   # what each user story uses
    story_notes = story_vote_domains(model)               # domains from the user stories
    n_prop = propagate_domains(model) if story_notes else 0
    cluster_notes = cluster_domains(model, force=bool(story_notes))   # whatever is still without a domain
    resolve_domains(model)                                # refresh 'used in'
    domain_notes = [n for n in domain_notes if "nothing points to it" not in n or
                    model.entities.get(n.split(":")[0], None) is None or
                    model.entities[n.split(":")[0]].domain == "Unassigned"]
    if story_notes:
        domain_notes = ([f"Domains taken from user stories for {len(story_notes)} entities"
                         + (f" and from their connections for {n_prop} more" if n_prop else "") + ":"]
                        + story_notes + domain_notes)
    if cluster_notes:
        domain_notes += ["Entities with no domain in the files or the stories were grouped by how they connect:"] + cluster_notes
    for e in model.entities.values():
        for a in e.attributes.values():
            if a.extra.get("deprecated_codelist"):
                notes.append(f"{e.name}.{a.name}: code list '{a.extra['deprecated_codelist']}' is deprecated (link removed)")
    finalize_story_domains(model)
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
