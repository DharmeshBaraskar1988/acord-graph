"""Command line:  python -m acord_graph <files or folders> -o out/

Writes:
  graph.html           interactive graph (self-contained, opens offline)
  stories.html         user stories: domain → story → ACORD entities, and inside each story's samples
  mapping.html         company model ↔ ACORD mapping: entity graph + attribute matches (when custom files are loaded)
  company_mapping.csv  one row per company entity: ACORD entity, status, similarity, attributes matched
  attribute_matches.csv one row per company attribute: ACORD path, score, match type, reason
  attribute_mapping.csv one row per entity attribute (type, required, code list, stories)
  custom_attributes.csv only the custom + changed attributes (when custom files are loaded)
  story_findings.csv   sample-vs-model check for every user story
  deprecated.txt       what was excluded (or flagged) as deprecated
  graph.graphml        for Gephi / yEd / Neo4j import
  graph.json           node-link JSON
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from . import analyze, attribute_rows, build_graph, load_paths, to_graphml, to_html, to_json
from .mapping_view import to_mapping_html
from .matcher import map_and_finalize, mapping_rows


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="acord_graph", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", help="JSON files and/or folders (searched recursively). "
                                              "Default: everything stored in the project's data/ folder")
    ap.add_argument("-c", "--custom", action="append", default=[],
                    help="custom / extension file or folder, applied on top of the standard model (repeatable)")
    ap.add_argument("-o", "--out", default="out", help="output folder (default: out)")
    ap.add_argument("-d", "--domain", action="append", help="only these domains (repeatable)")
    ap.add_argument("--no-codelists", action="store_true")
    ap.add_argument("--no-stories", action="store_true")
    ap.add_argument("--include-deprecated", action="store_true",
                    help="keep deprecated items (shown greyed out) instead of excluding them")
    ap.add_argument("--title", default="ACORD Canonical Model Graph")
    a = ap.parse_args(argv)

    if a.inputs or a.custom:
        model = load_paths(a.inputs, a.custom)
    else:
        from .store import DATA_DIR, load_store
        model = load_store()
        print(f"  reading project data from {DATA_DIR}")
    report = analyze(model, a.include_deprecated)
    maps = map_and_finalize(model, report)
    g = build_graph(model, not a.no_codelists, not a.no_stories, a.domain)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    (out / "graph.html").write_text(to_html(model, g, a.title), encoding="utf-8")
    rows = attribute_rows(model)
    if rows:
        with open(out / "attribute_mapping.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    cust = [r for r in rows if r.get("Origin") in ("custom", "changed")]
    if cust:
        with open(out / "custom_attributes.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(cust[0])); w.writeheader(); w.writerows(cust)
    with open(out / "story_findings.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["Story", "Title", "Domain", "Level", "Path", "Entity", "Attribute", "Message"])
        for s in model.stories.values():
            for fd in s.findings:
                w.writerow([s.id, s.title, s.domain, fd.level, fd.path, fd.entity, fd.attribute, fd.message])
    (out / "deprecated.txt").write_text(
        ("Included (flagged):\n" if a.include_deprecated else "Excluded as deprecated:\n")
        + "\n".join(report["deprecated"]) + "\n", encoding="utf-8")
    if maps:
        (out / "mapping.html").write_text(to_mapping_html(model, maps), encoding="utf-8")
        ent_rows, attr_rows = mapping_rows(maps)
        for name, rows_ in (("company_mapping.csv", ent_rows), ("attribute_matches.csv", attr_rows)):
            with open(out / name, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows_[0])); w.writeheader(); w.writerows(rows_)
        print(f"\ncompany model mapping ({len(maps)} entities):")
        for m in maps:
            print(f"  {m.company:28} -> {m.aligned or '—':18} {m.status:9} {m.similarity * 100:5.1f}%  {m.n_matched}/{m.n_attrs} attr")
    if model.stories:
        from .stories_view import to_stories_html
        from .store import DATA_DIR
        (out / "stories.html").write_text(to_stories_html(model, DATA_DIR if not (a.inputs or a.custom) else None),
                                          encoding="utf-8")
    to_graphml(g, out / "graph.graphml")
    (out / "graph.json").write_text(json.dumps(to_json(g), indent=1, default=str), encoding="utf-8")

    for line in model.load_log:
        print("  loaded", line)
    print(f"\n{len(model.entities)} entities in {len(model.domains())} domains, "
          f"{len(model.relationships())} relationships, {len(model.codelists)} code lists, "
          f"{len(model.stories)} user stories")
    for k, v in report.items():
        if v:
            print(f"\n{k} ({len(v)}):"); [print("  -", x) for x in v[:25]]
    issues = sum(1 for s in model.stories.values() for f in s.findings if f.level == "error")
    print(f"\nstory sample errors: {issues}\nwrote {out.resolve()}/graph.html")


if __name__ == "__main__":
    main()
