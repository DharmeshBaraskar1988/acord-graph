"""Load ACORD JSON files of any of the supported shapes into an AcordModel.

Every file is offered to three extractors (entities, code lists, user stories),
so one file may contribute to all three. Supported shapes:

Entities / master object file
  * JSON Schema:  {"definitions": {...}}, {"$defs": {...}},
                  {"components": {"schemas": {...}}}  or a single schema with "properties".
  * Object list:  {"objects"|"entities"|"masterObjects"|"classes": [ {name, domain,
                  attributes:[{name,type,required,description,codeList}], relationships:[...]}, ... ]}

Code lists / master code list file
  * {"codeLists"|"codelists"|"masterCodeList"|"lists": {Name: {description, values:[...]}}}
  * the same as a list: [{"name": ..., "values"|"codes"|"items": [...]}]
  * values may be plain strings or {code|value|key, description|label|name|definition}

User stories
  * {"userStories"|"stories"|"businessStories": [ {...story...} ]}  or one story per file
  * story keys: id|storyId|key, title|name|summary, description|narrative|asA/iWant/soThat,
    domain, entities, sample|samples|example|examples|payload|request|response, acceptanceCriteria
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .model import AcordModel, Attribute, CodeList, CodeValue, Entity, Relationship, UserStory

ENTITY_CONTAINERS = ("definitions", "$defs", "entities", "objects", "masterObjects",
                     "master_objects", "classes", "schemas", "types", "aggregates")
CODELIST_CONTAINERS = ("codeLists", "codelists", "code_lists", "masterCodeList",
                       "masterCodeLists", "lists", "codes", "enumerations", "valueLists")
STORY_CONTAINERS = ("userStories", "user_stories", "stories", "businessStories", "useCases")

DOMAIN_KEYS = ("x-acord-domain", "x-domain", "domain", "subjectArea", "subject_area",
               "category", "x-category", "package", "module")
CODELIST_KEYS = ("x-codelist", "x-codeList", "x-acord-codelist", "codeList", "codelist",
                 "codeListName", "code_list", "codeListRef", "valueList", "x-code-list")
DESC_KEYS = ("description", "definition", "desc", "documentation", "comment", "title")


def _first(d: dict, keys: Iterable[str], default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _tag_domain(d: dict) -> str:
    for k in ("x-tags", "tags", "x-acord-tags"):
        t = d.get(k)
        if isinstance(t, list) and t:
            return str(t[0] if not isinstance(t[0], dict) else _first(t[0], ("name",)))
        if isinstance(t, str) and t:
            return t
    return ""


def is_deprecated(d: Any) -> bool:
    """deprecated: true | x-deprecated | x-acord-deprecated | status/lifecycle = deprecated/retired."""
    if not isinstance(d, dict):
        return False
    for k, v in d.items():
        nk = re.sub(r"[^a-z]", "", k.lower())
        if nk in ("deprecated", "xdeprecated", "xacorddeprecated", "isdeprecated"):
            if v is True or str(v).strip().lower() in ("true", "y", "yes", "1"):
                return True
            if isinstance(v, (dict, str)) and v not in ("", "false", "False", "N", "n", "no"):
                return True   # e.g. {"since": "1.9", "replacedBy": "..."} or "since 1.9"
        if nk in ("status", "lifecycle", "lifecyclestatus", "xstatus", "xacordstatus") and \
                str(v).strip().lower() in ("deprecated", "retired", "obsolete", "removed"):
            return True
    return False


def codelist_ref(d: dict) -> str:
    """Code-list name on a property. Exact known keys first, then any key that
    looks like a code-list pointer (x-acord-codeList, x-codeListName, codeSetRef ...)."""
    if not isinstance(d, dict):
        return ""
    val = _first(d, CODELIST_KEYS, None)
    if val is None:
        for k, v in d.items():
            nk = re.sub(r"[^a-z]", "", k.lower())
            if any(t in nk for t in ("codelist", "codeset", "valuelist", "codetable")):
                val = v
                break
    if isinstance(val, dict):
        val = _first(val, ("$ref", "name", "id", "ref"))
    if isinstance(val, list):
        val = val[0] if val else ""
    return ref_name(str(val)) if val else ""


def norm(name: str) -> str:
    """Normalize names for fuzzy matching: 'State_Prov-Cd' -> 'stateprovcd'."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def ref_name(ref: str) -> str:
    """'#/definitions/Address' or 'Address.json' or 'acord:Address' -> 'Address'."""
    ref = ref.split("#")[-1] if "#/" in ref else ref
    ref = ref.rstrip("/").split("/")[-1]
    ref = re.sub(r"\.(schema\.)?json$", "", ref)
    return ref.split(":")[-1]


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
def _parse_schema_property(name: str, spec: dict, required: bool) -> Attribute:
    attr = Attribute(name=name, required=required)
    if not isinstance(spec, dict):
        return attr
    attr.deprecated = is_deprecated(spec) or is_deprecated(spec.get("items"))
    attr.description = str(_first(spec, DESC_KEYS))
    attr.format = spec.get("format", "")
    attr.pattern = spec.get("pattern", "")
    attr.max_length = spec.get("maxLength")
    attr.codelist = codelist_ref(spec)

    target = spec
    if spec.get("type") == "array" or "items" in spec:
        attr.is_array = True
        target = spec.get("items", {}) or {}
        attr.codelist = attr.codelist or codelist_ref(target)
        attr.description = attr.description or str(_first(target, DESC_KEYS))
    for combo in ("allOf", "oneOf", "anyOf"):
        if combo in target and isinstance(target[combo], list):
            for sub in target[combo]:
                if isinstance(sub, dict) and "$ref" in sub:
                    target = sub
                    break
    if "$ref" in target:
        ref = target["$ref"]
        name_ = ref_name(ref)
        if re.search(r"code[-_]?lists?|enumerations?|codes/", ref, re.I):
            attr.codelist = attr.codelist or name_
            attr.type = "code"
        else:
            attr.ref_entity = name_
            attr.type = "object"
    else:
        t = target.get("type", "string")
        attr.type = "/".join(t) if isinstance(t, list) else str(t)
        if attr.type == "object" and "properties" in target:
            attr.type = "object(inline)"
    if "enum" in target:
        attr.enum = [str(v) for v in target["enum"]]
    for k in ("x-relationship", "x-association", "relationshipType", "associationType"):
        if k in spec:
            attr.extra["rel_type"] = str(spec[k]).lower()
    return attr


def _entity_from_schema(name: str, schema: dict, domain_hint: str, source: str) -> Entity:
    declared = _first(schema, DOMAIN_KEYS) or _tag_domain(schema)
    ent = Entity(name=str(schema.get("title") if name is None else name),
                 domain=str(declared or domain_hint),
                 domain_source="declared" if declared else ("file" if domain_hint != "Unassigned" else ""),
                 description=str(_first(schema, ("description", "definition", "documentation"))),
                 source=source, abstract=bool(schema.get("abstract") or schema.get("x-abstract")),
                 deprecated=is_deprecated(schema))
    parts = [schema]
    for sub in schema.get("allOf", []) or []:
        if isinstance(sub, dict):
            if "$ref" in sub and "properties" not in sub:
                ent.parents.append(ref_name(sub["$ref"]))
            else:
                parts.append(sub)
    for p in ("extends", "x-extends", "baseType", "parent", "superType"):
        if isinstance(schema.get(p), str):
            ent.parents.append(ref_name(schema[p]))
    for part in parts:
        req = set(part.get("required", []) or [])
        for pname, pspec in (part.get("properties") or {}).items():
            at = _parse_schema_property(pname, pspec, pname in req)
            if "required" in part:
                at.extra["req_declared"] = True
            ent.attributes[pname] = at
    return ent


def _attribute_from_record(rec: dict) -> Attribute:
    """Master-object style attribute record (flat dict)."""
    name = str(_first(rec, ("name", "attributeName", "attribute", "propertyName", "field", "id")))
    req = rec.get("required", rec.get("mandatory", rec.get("isRequired", False)))
    if isinstance(req, str):
        req = req.strip().lower() in ("y", "yes", "true", "1", "required", "mandatory", "m")
    card = str(rec.get("cardinality", rec.get("multiplicity", "")))
    attr = Attribute(name=name, required=bool(req) or card.startswith("1"),
                     type=str(_first(rec, ("type", "dataType", "datatype", "data_type"), "string")),
                     description=str(_first(rec, DESC_KEYS)),
                     is_array=bool(rec.get("isArray") or rec.get("repeating") or card.endswith("*")
                                   or card.endswith("n")),
                     codelist=codelist_ref(rec),
                     format=str(rec.get("format", "")),
                     max_length=rec.get("maxLength") or rec.get("length"))
    ref = _first(rec, ("$ref", "ref", "refEntity", "reference", "objectType", "targetEntity"))
    if ref:
        attr.ref_entity = ref_name(str(ref))
        attr.type = "object"
    if isinstance(rec.get("enum"), list):
        attr.enum = [str(v) for v in rec["enum"]]
    attr.deprecated = is_deprecated(rec)
    return attr


def _entity_from_record(rec: dict, domain_hint: str, source: str) -> Entity | None:
    if "properties" in rec or "allOf" in rec:
        name = _first(rec, ("name", "title", "entityName", "objectName", "$id"))
        return _entity_from_schema(ref_name(str(name)), rec, domain_hint, source) if name else None
    name = _first(rec, ("name", "entityName", "objectName", "className", "title", "id"))
    if not name:
        return None
    declared = _first(rec, DOMAIN_KEYS) or _tag_domain(rec)
    ent = Entity(name=str(name), domain=str(declared or domain_hint),
                 domain_source="declared" if declared else ("file" if domain_hint != "Unassigned" else ""),
                 description=str(_first(rec, ("description", "definition", "documentation"))),
                 source=source, deprecated=is_deprecated(rec))
    parent = _first(rec, ("extends", "parent", "baseType", "superType", "inherits"))
    if parent:
        ent.parents = [ref_name(p) for p in (parent if isinstance(parent, list) else [parent])]
    attrs = _first(rec, ("attributes", "fields", "elements", "columns", "props"), [])
    if isinstance(attrs, dict):
        attrs = [{"name": k, **(v if isinstance(v, dict) else {"type": v})} for k, v in attrs.items()]
    for a in attrs or []:
        if isinstance(a, dict):
            at = _attribute_from_record(a)
            if at.name:
                ent.attributes[at.name] = at
    return ent


def _relationships_from_record(rec: dict, ent_name: str) -> list[Relationship]:
    out = []
    for r in rec.get("relationships", rec.get("associations", [])) or []:
        if not isinstance(r, dict):
            continue
        tgt = _first(r, ("target", "targetEntity", "to", "entity", "object", "$ref"))
        if not tgt:
            continue
        out.append(Relationship(
            source=str(_first(r, ("source", "from"), ent_name)), target=ref_name(str(tgt)),
            name=str(_first(r, ("name", "role", "label"), "relatesTo")),
            rel_type=str(_first(r, ("type", "relationshipType", "kind"), "association")).lower(),
            cardinality=str(_first(r, ("cardinality", "multiplicity"), "0..*")),
            description=str(_first(r, DESC_KEYS))))
    return out


def _looks_like_schema(v: Any) -> bool:
    return isinstance(v, dict) and ("properties" in v or "allOf" in v or
                                    v.get("type") == "object")


def _refs_in(obj: Any, out: set[str]) -> set[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "$ref" and isinstance(v, str):
                out.add(ref_name(v))
            else:
                _refs_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _refs_in(v, out)
    return out


def path_tag_domains(data: Any) -> dict[str, str]:
    """OpenAPI: schemas used by an operation get the operation's first tag (or first path segment) as domain."""
    out: dict[str, str] = {}
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        return out
    for path, item in data["paths"].items():
        if not isinstance(item, dict):
            continue
        seg = next((x for x in path.strip("/").split("/")
                    if x and not x.startswith("{") and not re.fullmatch(r"v\d+|api|rest|ngds|acord", x, re.I)), "")
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            dom = str(tags[0]) if tags else seg
            if not dom:
                continue
            dom = dom[:1].upper() + dom[1:]
            for name in _refs_in(op, set()):
                out.setdefault(name, dom)
    return out


def extract_entities(data: Any, source: str, domain_hint: str) -> tuple[dict[str, Entity], list[Relationship]]:
    ents: dict[str, Entity] = {}
    rels: list[Relationship] = []

    def add_container(container: Any, hint: str) -> None:
        if isinstance(container, dict):
            for name, spec in container.items():
                if _looks_like_schema(spec):
                    ents[name] = _entity_from_schema(name, spec, hint, source)
                elif isinstance(spec, dict) and _first(spec, ("attributes", "fields", "elements")):
                    e = _entity_from_record({"name": name, **spec}, hint, source)
                    if e:
                        ents[e.name] = e
                        rels.extend(_relationships_from_record(spec, e.name))
        elif isinstance(container, list):
            for rec in container:
                if isinstance(rec, dict):
                    e = _entity_from_record(rec, hint, source)
                    if e:
                        ents[e.name] = e
                        rels.extend(_relationships_from_record(rec, e.name))

    if not isinstance(data, dict):
        if isinstance(data, list) and data and all(isinstance(r, dict) for r in data) and \
                any(_first(r, ("attributes", "fields", "properties")) for r in data):
            add_container(data, domain_hint)
        return ents, rels

    hint = str(_first(data, DOMAIN_KEYS, domain_hint) or domain_hint)
    found = False
    for key in ENTITY_CONTAINERS:
        if key in data:
            add_container(data[key], hint)
            found = True
    if isinstance(data.get("components"), dict) and "schemas" in data["components"]:
        add_container(data["components"]["schemas"], hint)
        found = True
    # Domain-grouped:  {"domains": {"Party": {"entities": [...]}}} or [{"name":"Party","entities":[...]}]
    doms = data.get("domains") or data.get("subjectAreas")
    if isinstance(doms, dict):
        doms = [{"name": k, **v} for k, v in doms.items() if isinstance(v, dict)]
    for d in doms or []:
        if isinstance(d, dict):
            for key in ENTITY_CONTAINERS:
                if key in d:
                    add_container(d[key], str(d.get("name", hint)))
                    found = True
    # A single top-level schema that is itself an entity
    if not found and "properties" in data and data.get("title"):
        ents[str(data["title"])] = _entity_from_schema(str(data["title"]), data, hint, source)
        for k in ("definitions", "$defs"):
            add_container(data.get(k, {}), hint)
    return ents, rels


# ---------------------------------------------------------------------------
# Code lists
# ---------------------------------------------------------------------------
def _values(raw: Any) -> list[CodeValue]:
    out = []
    if isinstance(raw, dict):   # {"CA": "California", ...}
        for k, v in raw.items():
            out.append(CodeValue(str(k), str(v if not isinstance(v, dict) else _first(v, DESC_KEYS + ("label", "name")))))
        return out
    for v in raw or []:
        if isinstance(v, dict):
            code = _first(v, ("code", "value", "key", "id", "cd", "name"))
            desc = _first(v, ("description", "label", "displayName", "definition", "name", "text"))
            out.append(CodeValue(str(code), str(desc if desc != code else ""), is_deprecated(v)))
        else:
            out.append(CodeValue(str(v)))
    return out


def _codelist_from(name: str, spec: Any, hint: str, source: str) -> CodeList | None:
    if isinstance(spec, list):
        return CodeList(name=name, values=_values(spec), domain=hint, source=source)
    if not isinstance(spec, dict):
        return None
    raw = _first(spec, ("values", "codes", "items", "entries", "enum", "codeValues", "members"), None)
    if raw is None:
        return None
    return CodeList(name=str(_first(spec, ("name", "codeListName", "id"), name)),
                    description=str(_first(spec, DESC_KEYS)),
                    domain=str(_first(spec, DOMAIN_KEYS, hint)), values=_values(raw), source=source,
                    deprecated=is_deprecated(spec))


def _enum_schema_values(spec: dict) -> list[CodeValue] | None:
    """A schema that is really a code list: enum (+ optional descriptions) or oneOf/anyOf of consts."""
    if not isinstance(spec, dict) or "properties" in spec:
        return None
    if isinstance(spec.get("enum"), list):
        descs = _first(spec, ("x-enum-descriptions", "x-enumDescriptions", "x-enum-varnames",
                              "x-enumNames", "x-acord-enum-descriptions"), [])
        out = []
        for i, code in enumerate(spec["enum"]):
            d = descs.get(str(code), "") if isinstance(descs, dict) else (descs[i] if i < len(descs) else "")
            out.append(CodeValue(str(code), str(d)))
        dep = _first(spec, ("x-deprecated-enum", "x-deprecatedValues", "x-deprecated-values",
                            "x-acord-deprecated-codes"), [])
        for cv in out:
            cv.deprecated = cv.code in {str(x) for x in dep}
        return out
    for combo in ("oneOf", "anyOf"):
        items = spec.get(combo)
        if isinstance(items, list) and items and all(isinstance(x, dict) and "const" in x for x in items):
            return [CodeValue(str(x["const"]), str(_first(x, ("description", "title"))), is_deprecated(x))
                    for x in items]
    vals = _first(spec, ("x-codes", "x-code-values", "x-acord-codes", "x-values"), None)
    return _values(vals) if vals else None


def schema_codelists(data: Any, source: str, hint: str) -> dict[str, CodeList]:
    """Enum-style schemas inside components.schemas / $defs / definitions."""
    out: dict[str, CodeList] = {}
    if not isinstance(data, dict):
        return out
    containers = [data.get("$defs"), data.get("definitions")]
    if isinstance(data.get("components"), dict):
        containers.append(data["components"].get("schemas"))
    for c in containers:
        if not isinstance(c, dict):
            continue
        for name, spec in c.items():
            vals = _enum_schema_values(spec)
            if vals is not None:
                out[name] = CodeList(name=name, description=str(_first(spec, DESC_KEYS)),
                                     domain=str(_first(spec, DOMAIN_KEYS, hint)), values=vals, source=source,
                                     deprecated=is_deprecated(spec))
    return out


def extract_codelists(data: Any, source: str, domain_hint: str) -> dict[str, CodeList]:
    lists: dict[str, CodeList] = schema_codelists(data, source, domain_hint)

    def add(container: Any, hint: str) -> None:
        if isinstance(container, dict):
            for name, spec in container.items():
                cl = _codelist_from(name, spec, hint, source)
                if cl:
                    lists[cl.name] = cl
        elif isinstance(container, list):
            for spec in container:
                if isinstance(spec, dict):
                    nm = _first(spec, ("name", "codeListName", "id", "title"))
                    cl = _codelist_from(str(nm), spec, hint, source) if nm else None
                    if cl:
                        lists[cl.name] = cl

    if isinstance(data, dict):
        for key in CODELIST_CONTAINERS:
            if key in data:
                add(data[key], domain_hint)
        # A file that is just {"StateProvCd": [...], "CountryCd": [...]}
        if not lists and "codelist" in source.lower().replace("_", "").replace("-", "").replace(" ", ""):
            add(data, domain_hint)
    elif isinstance(data, list) and "code" in source.lower():
        add(data, domain_hint)
    return lists


# ---------------------------------------------------------------------------
# User stories
# ---------------------------------------------------------------------------
def _story_from(rec: dict, source: str, idx: int) -> UserStory:
    sid = str(_first(rec, ("id", "storyId", "story_id", "key", "storyKey", "number"), f"{Path(source).stem}-{idx}"))
    desc = str(_first(rec, ("description", "narrative", "story", "text", "userStory")))
    if not desc and any(k in rec for k in ("asA", "iWant", "soThat")):
        desc = f"As a {rec.get('asA', '')}, I want {rec.get('iWant', '')} so that {rec.get('soThat', '')}"
    dom = str(_first(rec, DOMAIN_KEYS))
    st = UserStory(id=sid, title=str(_first(rec, ("title", "name", "summary"), sid)),
                   description=desc, domain=dom, source=source, domain_source="declared" if dom else "")
    ents = _first(rec, ("entities", "objects", "relatedEntities", "dataObjects"), [])
    st.declared_entities = [ref_name(e if isinstance(e, str) else _first(e, ("name", "entity")))
                            for e in (ents if isinstance(ents, list) else [ents])]
    for key in ("samples", "sample", "examples", "example", "payload", "payloads",
                "request", "response", "sampleData", "data", "json"):
        val = rec.get(key)
        if isinstance(val, dict):
            st.samples.append(val)
        elif isinstance(val, list):
            st.samples.extend(v for v in val if isinstance(v, dict))
    ac = _first(rec, ("acceptanceCriteria", "acceptance_criteria", "criteria"), [])
    st.acceptance_criteria = [str(a if not isinstance(a, dict) else _first(a, ("text", "description", "given")))
                              for a in (ac if isinstance(ac, list) else [ac])]
    return st


STORY_KEYS = ("title", "asA", "iWant", "soThat", "storyId", "sample", "samples", "acceptanceCriteria",
              "userStory", "narrative", "example", "examples", "payload")


def extract_stories(data: Any, source: str, hint: str = "", force: bool = False) -> dict[str, UserStory]:
    """force=True: the file came from the user-stories area, so a file with no story structure
    is read as one story whose sample is the file itself (a plain sample payload)."""
    out: dict[str, UserStory] = {}
    recs: list[dict] = []
    if isinstance(data, dict):
        for key in STORY_CONTAINERS:
            if isinstance(data.get(key), list):
                recs.extend(r for r in data[key] if isinstance(r, dict))
        if not recs and re.search(r"stor(y|ies)|us[-_ ]?\d", source, re.I) and \
                any(k in data for k in ("title", "asA", "iWant", "storyId", "sample", "samples", "acceptanceCriteria")):
            recs.append(data)
    elif isinstance(data, list) and (force or re.search(r"stor(y|ies)", source, re.I)):
        recs = [r for r in data if isinstance(r, dict)]
    if force and not recs and isinstance(data, dict):
        if any(k in data for k in STORY_KEYS):
            recs.append(data)
        else:   # a bare sample payload: the file name is the story
            stem = Path(source).stem
            recs.append({"id": stem, "title": re.sub(r"[_-]+", " ", stem), "sample": data})
    for i, r in enumerate(recs, 1):
        st = _story_from(r, source, i)
        if not st.domain and hint and hint != "Unassigned":
            st.domain, st.domain_source = hint, "folder"
        out[st.id] = st
    return out


# ---------------------------------------------------------------------------
# Deprecation list (separate file)
# ---------------------------------------------------------------------------
DEPRECATION_CONTAINERS = ("deprecations", "deprecated", "deprecatedItems", "deprecationList",
                          "deprecated_items", "retired", "deprecatedResources", "deprecatedElements")


def _dep_key(rec: Any) -> list[str]:
    if isinstance(rec, str):
        return [rec.split("#/")[-1].replace("components/schemas/", "").replace("/properties/", ".").strip("/")]
    if not isinstance(rec, dict):
        return []
    ent = _first(rec, ("entity", "resource", "schema", "object", "objectName", "resourceName", "component"))
    att = _first(rec, ("attribute", "property", "field", "propertyName", "element"))
    cl = _first(rec, ("codeList", "codelist", "codeListName", "codeSet"))
    code = _first(rec, ("code", "value", "codeValue"))
    path = _first(rec, ("path", "jsonPath", "$ref", "pointer", "name", "id"))
    if cl:
        return [f"{ref_name(str(cl))}={code}" if code else ref_name(str(cl))]
    if ent:
        return [f"{ref_name(str(ent))}.{att}" if att else ref_name(str(ent))]
    return _dep_key(str(path)) if path else []


def extract_deprecations(data: Any, source: str) -> set[str]:
    recs: list = []
    if isinstance(data, dict):
        for k in DEPRECATION_CONTAINERS:
            if isinstance(data.get(k), (list, dict)):
                v = data[k]
                recs.extend(v if isinstance(v, list) else
                            [{"entity": n, **(x if isinstance(x, dict) else {})} for n, x in v.items()])
    elif isinstance(data, list) and re.search(r"deprecat|retire", source, re.I):
        recs = data
    out: set[str] = set()
    for r in recs:
        out.update(_dep_key(r))
    return {x for x in out if x}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
CUSTOM_FILE_RE = re.compile(r"custom|extension|[-_]ext\b|overlay|client|carrier", re.I)


def load_json_bytes(raw: bytes | str, source: str, domain_hint: str = "", custom: bool | None = None,
                    kind: str = "") -> AcordModel:
    """custom=None -> decide from the file ('x-custom': true) or its name (custom/extension/...).
    kind='stories' -> the file is from the user-stories area (see extract_stories)."""
    data = json.loads(raw)
    hint = domain_hint or "Unassigned"
    model = AcordModel()
    if kind == "stories":
        model.stories.update(extract_stories(data, source, domain_hint, force=True))
        model.load_log.append(f"{source}: {len(model.stories)} user stories")
        return model
    ents, rels = extract_entities(data, source, hint)
    for name, dom in path_tag_domains(data).items():
        e = ents.get(name)
        if e and e.domain_source in ("", "file"):
            e.domain, e.domain_source = dom, "tag"
    if custom is None:
        custom = bool(isinstance(data, dict) and (data.get("x-custom") or data.get("custom"))) \
            or bool(CUSTOM_FILE_RE.search(source))
    if custom:
        for e in ents.values():
            e.origin = "custom"
            for a in e.attributes.values():
                a.extra["origin"] = "custom"
    model.entities.update(ents)
    model.extra_relationships.extend(rels)
    model.codelists.update(extract_codelists(data, source, hint))
    model.stories.update(extract_stories(data, source))
    model.deprecation_list |= extract_deprecations(data, source)
    model.load_log.append(f"{source}: {len(ents)} entities, {len(model.codelists)} code lists, "
                          f"{len(model.stories)} user stories"
                          + (f", {len(model.deprecation_list)} deprecation-list entries" if model.deprecation_list else ""))
    return model


def load_paths(paths: Iterable[str | Path], custom_paths: Iterable[str | Path] = ()) -> AcordModel:
    """Load files and/or folders (recursively). Folder names become domain hints.
    custom_paths are applied afterwards as an overlay on the standard model."""
    model = AcordModel()
    jobs = [(p, None) for p in paths] + [(p, True) for p in custom_paths]
    # standard files first so custom ones (by name) always overlay them
    def _files(p):
        p = Path(p)
        return sorted(p.rglob("*.json")) if p.is_dir() else [p]
    ordered = []
    for p, c in jobs:
        for f in _files(p):
            is_c = c if c is not None else bool(CUSTOM_FILE_RE.search(f.name))
            ordered.append((p, f, is_c))
    ordered.sort(key=lambda t: t[2])
    for p, f, is_c in ordered:
        p = Path(p)
        hint = f.parent.name if p.is_dir() and f.parent != p else ""
        if re.search(r"custom|ext|overlay|stor(y|ies)|code|sample|deprecat|^\d|json|schema", hint, re.I):
            hint = ""   # folder names like custom/, user_stories/ are not domains
        try:
            model.merge(load_json_bytes(f.read_bytes(), f.name, hint, custom=is_c or None))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            model.load_log.append(f"{f.name}: skipped ({exc})")
    return model
