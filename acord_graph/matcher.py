"""Map a company (custom) model onto the ACORD model.

For every company entity:
  1. Score every ACORD entity: name similarity (40%) + share of the company
     attributes that match one of its attributes (60%).
  2. The best one is the *aligned* entity; the next ones above a floor are
     *alternative candidates*.
  3. Company attributes that do not match the aligned entity are tried against the
     ACORD entities *connected* to it (its relationships, both directions, and its
     parents). That is the "attributes matched into this ACORD entity" link.
  4. Status: MATCHED (>= 70 % of attributes matched and a good alignment),
     PARTIAL, or UNMATCHED. Attributes that match nowhere stay unmatched;
     nothing is force-fitted.

Name matching understands camelCase/snake_case, ACORD abbreviations
(Cd, Dt, Amt, Ind, Nbr ...), common insurance synonyms and version/noise
suffixes (Model, _v3, Dto, Info ...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

from .model import AcordModel, Attribute, Entity

ABBREV = {
    "cd": "code", "dt": "date", "tm": "time", "amt": "amount", "ind": "indicator", "nbr": "number",
    "num": "number", "no": "number", "desc": "description", "addr": "address", "qty": "quantity",
    "pct": "percent", "id": "identifier", "ident": "identifier", "info": "", "ref": "reference",
    "dob": "birth date", "birthdate": "birth date", "dateofbirth": "birth date", "zip": "postalcode", "zipcode": "postalcode", "tel": "phone", "telephone": "phone",
    "fname": "givenname", "lname": "surname", "nm": "name", "typ": "type", "stat": "status",
    "yr": "year", "mo": "month", "acct": "account", "pmt": "payment", "prem": "premium",
    "lob": "lineofbusiness", "eff": "effective", "exp": "expiration", "st": "state", "prov": "province",
    "vin": "vehicleidentificationnumber", "veh": "vehicle", "insd": "insured", "clm": "claim",
}
# words that mean the same thing in a P&C model -> one canonical token
SYNONYMS = [
    ["claim", "loss", "lossevent", "incident", "fnol"],
    ["payment", "disbursement", "payout", "pay"],
    ["insured", "policyholder", "principal", "namedinsured"],
    ["party", "person", "contact", "individual", "claimant"],
    ["givenname", "firstname", "forename"],
    ["surname", "lastname", "familyname"],
    ["postalcode", "postcode"],
    ["address", "line", "street", "streetaddress"],
    ["city", "town", "cityname"],
    ["state", "stateprov", "province", "region"],
    ["country", "nation"],
    ["phone", "phonenumber", "mobile"],
    ["email", "emailaddress", "mail"],
    ["description", "narrative", "remarks", "comment", "text"],
    ["note", "notes", "memo"],
    ["reserve", "reserves"],
    ["coverage", "cover", "peril"],
    ["deductible", "excess", "retention"],
    ["limit", "maximum", "cap"],
    ["amount", "value", "sum"],
    ["cause", "reason"],
    ["vehicle", "auto", "car"],
    ["location", "site", "premises", "risklocation"],
    ["payee", "recipient", "beneficiary"],
    ["report", "filing"],
    ["policy", "contract", "agreement"],
    ["identifier", "key", "uid", "uuid"],
    ["component", "feature"],
]
_SYN = {w: g[0] for g in SYNONYMS for w in g}   # first word of each group is the canonical one
NOISE = {"model", "dto", "entity", "info", "details", "detail", "data", "record", "obj", "object", "the",
         "of", "custom", "ext", "extension", "type_", "list", "v", "bo", "vo", "resource", "schema"}


def _split(name: str) -> list[str]:
    name = re.sub(r"[_\-\s.]+", " ", name)
    name = re.sub(r"(?<![A-Za-z])[vV]\d+\b|(?<=[a-z])V\d+\b", " ", name)   # Model_v3, ModelV3
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    name = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", name)
    return [t.lower() for t in name.split() if t]


@lru_cache(maxsize=200_000)
def tokens(name: str) -> tuple[str, ...]:
    out = []
    raw = _split(name)
    joined = "".join(raw)
    if joined in _SYN or joined in ABBREV:          # whole name is a known word (e.g. LossEvent, zipCode)
        raw = [joined]
    for t0 in raw:
        for t in ABBREV.get(t0, t0).split():
            if not t or t in NOISE:
                continue
            if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and t[:-1] in _SYN:
                t = t[:-1]
            out.append(_SYN.get(t, t))
    return tuple(out)


# class words: they say what kind of value it is, not what it is about
GENERIC = {"date", "time", "code", "amount", "number", "name", "indicator", "type", "identifier",
           "text", "description", "percent", "quantity", "count", "flag", "value", "status"}


@lru_cache(maxsize=500_000)
def name_sim(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    if sa == sb:
        return 1.0
    jac = len(sa & sb) / len(sa | sb)
    # compare the meaningful words only, so 'receivedDt' and 'reserveDt' are not close because of 'date'
    ca, cb = [t for t in ta if t not in GENERIC] or list(ta), [t for t in tb if t not in GENERIC] or list(tb)
    seq = SequenceMatcher(None, "".join(ca), "".join(cb)).ratio()
    # one name contained in the other: lossCause vs lossCauseCd, reportNumber vs policeReportNumber
    small, big = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    contain = 0.0
    if small <= big and (len(small) >= 2 or (big - small) <= GENERIC):
        contain = 0.75 + 0.25 * jac
    score = max(jac, seq * 0.92, contain)
    ga, gb = sa & GENERIC, sb & GENERIC
    if ga and gb and not (ga & gb):          # amount vs date: different kinds of value
        score *= 0.8
    return score


TYPE_GROUP = {"string": "s", "code": "s", "integer": "n", "number": "n", "decimal": "n", "amount": "n",
              "boolean": "b", "object": "o", "array": "o"}


def attr_sim(a: Attribute, b: Attribute) -> float:
    s = name_sim(a.name, b.name)
    ga = TYPE_GROUP.get(a.type.split("/")[0].lower(), "s")
    gb = TYPE_GROUP.get(b.type.split("/")[0].lower(), "s")
    if ga != gb and not ({ga, gb} <= {"s", "n"}):
        s *= 0.8
    if a.format and a.format == b.format:
        s = min(1.0, s + 0.05)
    return s


ATTR_THRESHOLD = 0.72
ENTITY_FLOOR = 0.22


@dataclass
class AttrMatch:
    company_attr: str
    company_type: str
    acord_entity: str = ""
    acord_attr: str = ""
    acord_type: str = ""
    score: float = 0.0
    via: str = ""          # aligned | related | ""(unmatched)
    how: str = ""          # exact | synonym/abbrev | fuzzy
    path: str = ""         # full ACORD path, e.g. Claim.claimFeatures.deductible.deductibleAmt
    company_desc: str = ""
    acord_desc: str = ""
    reason: str = ""
    near_entity: str = ""  # best candidate below the threshold (for unmatched attributes)
    near_attr: str = ""
    near_score: float = 0.0


@dataclass
class EntityMapping:
    company: str
    aligned: str = ""
    similarity: float = 0.0
    name_score: float = 0.0
    status: str = "UNMATCHED"
    alternatives: list[tuple[str, float]] = field(default_factory=list)
    attrs: list[AttrMatch] = field(default_factory=list)
    level: int = 0
    domain: str = ""

    @property
    def n_attrs(self) -> int:
        return len(self.attrs)

    @property
    def n_matched(self) -> int:
        return sum(1 for a in self.attrs if a.acord_attr)

    @property
    def n_aligned(self) -> int:
        return sum(1 for a in self.attrs if a.via == "aligned")

    def targets(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.attrs:
            if a.via == "related":
                out[a.acord_entity] = out.get(a.acord_entity, 0) + 1
        return out


def _how(a: str, b: str, score: float) -> str:
    if a.lower() == b.lower():
        return "exact"
    if set(tokens(a)) == set(tokens(b)):
        return "synonym/abbrev"
    return "fuzzy"


def _best_assignment(c_attrs: list[Attribute], e_attrs: list[Attribute]) -> dict[str, tuple[Attribute, float]]:
    """Greedy one-to-one attribute assignment above the threshold."""
    pairs = []
    for ca in c_attrs:
        for ea in e_attrs:
            s = attr_sim(ca, ea)
            if s >= ATTR_THRESHOLD:
                pairs.append((s, ca.name, ea))
    pairs.sort(key=lambda x: -x[0])
    used_c, used_e, out = set(), set(), {}
    for s, cn, ea in pairs:
        if cn in used_c or ea.name in used_e:
            continue
        used_c.add(cn); used_e.add(ea.name)
        out[cn] = (ea, s)
    return out


def _neighbours(model: AcordModel, name: str) -> list[str]:
    out = []
    for r in model.relationships():
        if r.source == name and r.target in model.entities:
            out.append(r.target)
        elif r.target == name and r.source in model.entities:
            out.append(r.source)
    return [n for n in dict.fromkeys(out) if n != name]


def _paths_from(model: AcordModel, start: str, max_depth: int = 4) -> dict[str, str]:
    """Shortest navigation path (by relationship attribute names) from an ACORD entity to the others."""
    out = {start: start}
    frontier = [start]
    for _ in range(max_depth):
        nxt = []
        for n in frontier:
            e = model.entities.get(n)
            if not e:
                continue
            for a in e.all_attributes(model).values():
                if a.ref_entity and a.ref_entity in model.entities and a.ref_entity not in out:
                    out[a.ref_entity] = f"{out[n]}.{a.name}"
                    nxt.append(a.ref_entity)
        frontier = nxt
    return out


def _reason(ca: Attribute, ea: Attribute, score: float, how: str, via: str, aligned: str, entity: str,
            hop_desc: str) -> str:
    ta, tb = " · ".join(tokens(ca.name)), " · ".join(tokens(ea.name))
    if how == "exact":
        r = "Same attribute name."
    elif how == "synonym/abbrev":
        r = f"Same meaning once abbreviations and synonyms are expanded ({ta})."
    else:
        r = f"Similar names ({ta}  ~  {tb}), {score * 100:.0f}% — please review."
    r += f" Types: {ca.type} → {ea.type}."
    if via == "related":
        r += f" Not found on {aligned}; found on {entity}, which is connected to it ({hop_desc})."
    return r


def company_levels(company: dict[str, Entity]) -> dict[str, int]:
    """Depth of each company entity under its root(s) (entities nobody else references)."""
    children = {n: [a.ref_entity for a in e.attributes.values() if a.ref_entity in company]
                for n, e in company.items()}
    referenced = {c for cs in children.values() for c in cs}
    roots = [n for n in company if n not in referenced] or list(company)[:1]
    level = {r: 0 for r in roots}
    queue = list(roots)
    while queue:
        n = queue.pop(0)
        for c in children.get(n, []):
            if c not in level:
                level[c] = level[n] + 1
                queue.append(c)
    for n in company:
        level.setdefault(n, 0)
    return level


def map_company_model(model: AcordModel) -> list[EntityMapping]:
    company = model.company_entities
    if not company:
        return []
    acord = {n: e for n, e in model.entities.items() if e.origin != "custom"}
    acord_attrs = {n: [a for a in e.all_attributes(model).values() if not a.ref_entity]
                   for n, e in acord.items()}
    # inverted index on canonical attribute tokens to shortlist candidates quickly
    index: dict[tuple, set[str]] = {}
    for n, attrs in acord_attrs.items():
        for a in attrs:
            index.setdefault(tokens(a.name), set()).add(n)
    levels = company_levels(company)
    results = []
    path_cache: dict[str, dict[str, str]] = {}
    for cname, cent in company.items():
        c_attrs = [a for a in cent.attributes.values() if not (a.ref_entity and a.ref_entity in company)]
        quick: dict[str, float] = {}
        for a in c_attrs:
            for n in index.get(tokens(a.name), ()):
                quick[n] = quick.get(n, 0) + 1
        shortlist = sorted(acord, key=lambda n: -(name_sim(cname, n) * 3 + quick.get(n, 0) / max(len(c_attrs), 1)))[:25]
        scored = []
        for n in shortlist:
            ns = name_sim(cname, n)
            assign = _best_assignment(c_attrs, acord_attrs[n])
            cov = len(assign) / len(c_attrs) if c_attrs else 0.0
            scored.append((0.4 * ns + 0.6 * cov, n, ns, assign))
        scored.sort(key=lambda x: -x[0])
        m = EntityMapping(company=cname, level=levels.get(cname, 0), domain=cent.domain)
        if scored and scored[0][0] >= ENTITY_FLOOR and (scored[0][3] or scored[0][2] >= 0.75):
            sim, aligned, ns, assign = scored[0]
            m.aligned, m.similarity, m.name_score = aligned, round(sim, 3), round(ns, 3)
            m.alternatives = [(n, round(s_, 3)) for s_, n, ns_, as_ in scored[1:6]
                              if s_ >= max(ENTITY_FLOOR, 0.6 * sim) and (len(as_) >= 2 or ns_ >= 0.75)][:3]
        else:
            assign = {}
        hop: dict[str, int] = {}
        if m.aligned:
            first = _neighbours(model, m.aligned) + [p for p in acord[m.aligned].parents if p in acord]
            for n in first:
                hop.setdefault(n, 1)
            for n in first:
                for n2 in _neighbours(model, n):
                    if n2 != m.aligned:
                        hop.setdefault(n2, 2)
        neigh_attrs = {n: acord_attrs[n] for n in hop if n in acord_attrs}
        def hop_desc(n: str) -> str:
            return "direct relationship" if hop.get(n) == 1 else "2 relationships away"

        for ca in c_attrs:
            am = AttrMatch(ca.name, ca.type + ("[]" if ca.is_array else ""), company_desc=ca.description)
            if ca.name in assign:
                ea, s = assign[ca.name]
                am.acord_entity, am.acord_attr, am.acord_type, am.score, am.via = m.aligned, ea.name, ea.type, round(s, 3), "aligned"
            else:
                best = (0.0, "", None)
                for n, attrs in neigh_attrs.items():
                    for ea in attrs:
                        s = attr_sim(ca, ea) * (1.0 if hop[n] == 1 else 0.96)
                        if s > best[0]:
                            best = (s, n, ea)
                if best[0] >= ATTR_THRESHOLD:
                    am.acord_entity, am.acord_attr, am.acord_type = best[1], best[2].name, best[2].type
                    am.score, am.via = round(best[0], 3), "related"
            if am.acord_attr:
                am.how = _how(ca.name, am.acord_attr, am.score)
                ea = next(x for x in acord_attrs[am.acord_entity] if x.name == am.acord_attr)
                am.acord_desc = ea.description
                am.reason = _reason(ca, ea, am.score, am.how, am.via, m.aligned, am.acord_entity, hop_desc(am.acord_entity))
            else:
                best = (0.0, "", None)
                for n in ([m.aligned] if m.aligned else []) + list(neigh_attrs):
                    for ea in acord_attrs.get(n, []):
                        sc = attr_sim(ca, ea)
                        if sc > best[0]:
                            best = (sc, n, ea)
                shared = best[2] is not None and (set(tokens(ca.name)) & set(tokens(best[2].name))) - GENERIC
                if best[2] is not None and best[0] >= 0.55 and shared:
                    am.near_entity, am.near_attr, am.near_score = best[1], best[2].name, round(best[0], 3)
                    am.reason = (f"No ACORD attribute reached the {ATTR_THRESHOLD * 100:.0f}% threshold. Closest: "
                                 f"{best[1]}.{best[2].name} at {best[0] * 100:.0f}% (not used — would be a force-fit).")
                else:
                    am.reason = (f"No similar attribute on {m.aligned or 'any ACORD entity'}"
                                 + (" or the entities connected to it" if m.aligned else "") + ". Candidate for a custom extension.")
            m.attrs.append(am)
        if m.aligned and m.n_attrs:
            related = sum(1 for a in m.attrs if a.via == "related")
            m.similarity = round(0.4 * m.name_score + 0.6 * (m.n_aligned + 0.8 * related) / m.n_attrs, 3)
        elif m.aligned:
            m.similarity = m.name_score       # only relationships, no value attributes to compare
        ratio = m.n_matched / m.n_attrs if m.n_attrs else (1.0 if m.name_score >= 0.8 else 0.0)
        if m.aligned and ratio >= 0.7 and (m.similarity >= 0.4 or m.name_score >= 0.8):
            m.status = "MATCHED"
        elif m.aligned:
            m.status = "PARTIAL"
        results.append(m)
    # full ACORD paths, anchored at the ACORD entity that the company root maps to
    by_name = {r.company: r for r in results}
    parent: dict[str, str] = {}
    for n, e in company.items():
        for a in e.attributes.values():
            if a.ref_entity in company and a.ref_entity != n:
                parent.setdefault(a.ref_entity, n)
    for r in results:
        chain, cur = [r.company], r.company
        while cur in parent and parent[cur] not in chain:
            cur = parent[cur]; chain.append(cur)
        anchors = [by_name[c].aligned for c in reversed(chain) if c in by_name and by_name[c].aligned]
        for am in r.attrs:
            if not am.acord_attr:
                continue
            am.path = f"{am.acord_entity}.{am.acord_attr}"
            for anc in anchors:
                paths = path_cache.setdefault(anc, _paths_from(model, anc))
                if am.acord_entity in paths:
                    am.path = f"{paths[am.acord_entity]}.{am.acord_attr}"
                    break
    results.sort(key=lambda r: (r.level, r.company))
    return results


def mapping_rows(maps: list[EntityMapping]) -> tuple[list[dict], list[dict]]:
    ent_rows = [{"Company Entity": m.company, "Level": m.level, "ACORD Entity": m.aligned or "—",
                 "Status": m.status, "Similarity %": round(m.similarity * 100, 1),
                 "Name Score %": round(m.name_score * 100, 1),
                 "Attributes Matched": f"{m.n_matched}/{m.n_attrs}",
                 "Into Aligned": m.n_aligned,
                 "Into Related": ", ".join(f"{k} ({v})" for k, v in m.targets().items()),
                 "Alternatives": ", ".join(f"{n} ({s * 100:.0f}%)" for n, s in m.alternatives)} for m in maps]
    attr_rows = [{"Company Entity": m.company, "Company Attribute": a.company_attr, "Company Type": a.company_type,
                  "ACORD Entity": a.acord_entity or "—", "ACORD Attribute": a.acord_attr or "— unmatched —",
                  "ACORD Type": a.acord_type, "ACORD Path": a.path, "Score %": round(a.score * 100, 1) if a.acord_attr else None,
                  "Matched Via": a.via or "unmatched", "Match Type": a.how,
                  "Closest (below threshold)": f"{a.near_entity}.{a.near_attr} ({a.near_score * 100:.0f}%)" if a.near_attr else "",
                  "Reason": a.reason} for m in maps for a in m.attrs]
    return ent_rows, attr_rows


def apply_mapped_domains(model: AcordModel, maps: list[EntityMapping]) -> list[str]:
    """Company entities with no domain of their own take the domain of the ACORD entity they map to."""
    notes = []
    for m in maps:
        e = model.entities.get(m.company)
        if not e or not m.aligned or m.aligned not in model.entities:
            continue
        if e.origin == "custom" and e.domain_source in ("", "shared") or e.domain == "Unassigned":
            dom = model.entities[m.aligned].domain
            if dom and dom not in ("Unassigned", "Shared") and dom != e.domain:
                e.domain, e.domain_source = dom, "mapped"
                e.used_in_domains = (e.used_in_domains - {"Unassigned", "Shared"}) | {dom}
                m.domain = dom
                notes.append(f"{m.company}: domain {dom} (from its ACORD match {m.aligned})")
    return notes


def map_and_finalize(model: AcordModel, report: dict) -> list[EntityMapping]:
    """Run the company mapping, give unmapped-domain company entities their ACORD domain,
    and refresh the report notes that depended on the old domain."""
    from .analyzer import custom_changes
    maps = map_company_model(model)
    mapped = apply_mapped_domains(model, maps)
    names = {n.split(":")[0] for n in mapped}
    report["domains"] = mapped + [n for n in report.get("domains", []) if n.split(":")[0] not in names]
    report["custom"] = [n for n in report.get("custom", []) if "matched by name" in n] + custom_changes(model)
    return maps
