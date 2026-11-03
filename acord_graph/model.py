"""Normalized in-memory model for ACORD-style metadata.

Every input format (JSON Schema, master-object lists, code-list files,
user-story files) is converted into these dataclasses so the graph builder
and the UI never need to know which source format the data came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CodeValue:
    code: str
    description: str = ""
    deprecated: bool = False


@dataclass
class CodeList:
    name: str
    description: str = ""
    domain: str = ""
    values: list[CodeValue] = field(default_factory=list)
    source: str = ""
    deprecated: bool = False

    @property
    def codes(self) -> set[str]:
        return {v.code for v in self.values}


@dataclass
class Attribute:
    name: str
    type: str = "string"
    required: bool = False
    description: str = ""
    format: str = ""
    is_array: bool = False
    ref_entity: str = ""          # target entity if this attribute points to another object
    codelist: str = ""            # name of the code list that governs the values
    enum: list[str] = field(default_factory=list)   # inline enum values
    max_length: int | None = None
    pattern: str = ""
    deprecated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def cardinality(self) -> str:
        lo = "1" if self.required else "0"
        hi = "*" if self.is_array else "1"
        return f"{lo}..{hi}"

    @property
    def kind(self) -> str:
        if self.ref_entity:
            return "relationship"
        if self.codelist or self.enum:
            return "coded"
        return "simple"


@dataclass
class Relationship:
    source: str
    target: str
    name: str
    rel_type: str = "composition"   # composition | association | inheritance | reference
    cardinality: str = "0..1"
    description: str = ""


@dataclass
class Entity:
    name: str
    domain: str = "Unassigned"
    description: str = ""
    attributes: dict[str, Attribute] = field(default_factory=dict)
    parents: list[str] = field(default_factory=list)
    source: str = ""
    abstract: bool = False
    deprecated: bool = False
    origin: str = "standard"          # standard | custom | extended (standard + custom overlay)
    domain_source: str = ""           # declared | file | inherited | inferred | shared
    used_in_domains: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def all_attributes(self, model: "AcordModel", _seen: set[str] | None = None) -> dict[str, Attribute]:
        """Own attributes plus everything inherited from parents."""
        _seen = _seen or set()
        if self.name in _seen:
            return {}
        _seen.add(self.name)
        merged: dict[str, Attribute] = {}
        for p in self.parents:
            parent = model.entities.get(p) or model.removed_entities.get(p)
            if parent:
                merged.update(parent.all_attributes(model, _seen))
        merged.update(self.attributes)
        return merged


@dataclass
class SampleFinding:
    path: str
    level: str      # ok | warning | error
    message: str
    entity: str = ""
    attribute: str = ""


@dataclass
class UserStory:
    id: str
    title: str = ""
    description: str = ""
    domain: str = ""
    declared_entities: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    source: str = ""
    # filled in by the sample mapper
    used_entities: set[str] = field(default_factory=set)
    used_attributes: set[tuple[str, str]] = field(default_factory=set)
    findings: list[SampleFinding] = field(default_factory=list)


@dataclass
class AcordModel:
    entities: dict[str, Entity] = field(default_factory=dict)
    codelists: dict[str, CodeList] = field(default_factory=dict)
    stories: dict[str, UserStory] = field(default_factory=dict)
    extra_relationships: list[Relationship] = field(default_factory=list)
    load_log: list[str] = field(default_factory=list)
    # names from a separate deprecation list: "Party", "Party.taxId", "StateProvCd", "StateProvCd=XX"
    deprecation_list: set[str] = field(default_factory=set)
    # what was taken out because it is deprecated (kept so samples can be warned about it)
    removed_entities: dict[str, Entity] = field(default_factory=dict)
    removed_attributes: dict[tuple[str, str], Attribute] = field(default_factory=dict)
    removed_codelists: dict[str, CodeList] = field(default_factory=dict)
    removed_codes: dict[str, set[str]] = field(default_factory=dict)

    # ---- derived views -------------------------------------------------
    def relationships(self) -> list[Relationship]:
        rels: list[Relationship] = []
        for ent in self.entities.values():
            for parent in ent.parents:
                rels.append(Relationship(ent.name, parent, "extends", "inheritance", ""))
            for attr in ent.attributes.values():
                if attr.ref_entity:
                    rel_type = attr.extra.get("rel_type") or ("association" if attr.is_array else "composition")
                    rels.append(Relationship(ent.name, attr.ref_entity, attr.name, rel_type,
                                             attr.cardinality, attr.description))
        rels.extend(self.extra_relationships)
        return rels

    @staticmethod
    def _overlay(cur: "Entity", custom: "Entity") -> None:
        """Custom entity with the same name as a standard one: apply it as an extension."""
        cur.origin = "extended"
        cur.source = f"{cur.source} + {custom.source}"
        if custom.domain_source == "declared" and custom.domain != cur.domain:
            cur.notes.append(f"custom file puts it in domain '{custom.domain}', standard says '{cur.domain}' (standard kept)")
        cur.parents = list(dict.fromkeys(cur.parents + custom.parents))
        for an, a in custom.attributes.items():
            old = cur.attributes.get(an)
            if old is None:
                a.extra["origin"] = "custom"
                cur.attributes[an] = a
                continue
            if not a.extra.get("req_declared"):
                a.required = old.required          # custom file did not restate 'required'
            changes = []
            for f in ("type", "required", "is_array", "ref_entity", "codelist", "max_length"):
                ov, nv = getattr(old, f), getattr(a, f)
                if ov != nv and not (f in ("codelist", "ref_entity", "max_length") and not nv):
                    changes.append(f"{f}: {ov if ov not in ('', None) else '—'} → {nv if nv not in ('', None) else '—'}")
            if changes:
                a.description = a.description or old.description
                a.codelist = a.codelist or old.codelist
                a.extra["origin"] = "changed"
                a.extra["changes"] = changes
                cur.attributes[an] = a

    def domains(self) -> list[str]:
        ds = {e.domain for e in self.entities.values()}
        ds |= {s.domain for s in self.stories.values() if s.domain}
        return sorted(ds)

    def merge(self, other: "AcordModel") -> None:
        for k, v in other.entities.items():
            if k in self.entities and v.origin == "custom" and self.entities[k].origin != "custom":
                self._overlay(self.entities[k], v)
            elif k in self.entities:
                # keep the richer definition, but union attributes
                cur = self.entities[k]
                for an, a in v.attributes.items():
                    cur.attributes.setdefault(an, a)
                if cur.domain == "Unassigned" and v.domain != "Unassigned":
                    cur.domain = v.domain
                cur.description = cur.description or v.description
                cur.parents = list(dict.fromkeys(cur.parents + v.parents))
                cur.deprecated = cur.deprecated or v.deprecated
            else:
                self.entities[k] = v
        for k, v in other.codelists.items():
            if k in self.codelists and not v.values:
                continue
            self.codelists[k] = v
        self.stories.update(other.stories)
        self.deprecation_list |= other.deprecation_list
        self.extra_relationships.extend(other.extra_relationships)
        self.load_log.extend(other.load_log)
