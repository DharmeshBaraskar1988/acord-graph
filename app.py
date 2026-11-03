"""Streamlit UI:  streamlit run app.py

Add ACORD files, your company / custom model and user stories (JSON or ZIP).
Everything you add is saved inside the project, in data/, and loaded automatically next time.
"""
from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from acord_graph import AcordModel, analyze, attribute_rows, build_graph, to_html, to_json
from acord_graph.mapping_view import to_mapping_html
from acord_graph.stories_view import to_stories_html
from acord_graph.matcher import map_and_finalize, mapping_rows
from acord_graph.store import (DATA_DIR, clear_all, delete_item, fingerprint, load_store, save_upload,
                               stored_items)

st.set_page_config(page_title="ACORD Canonical Model Graph", layout="wide")

KIND_LABEL = {"standard": "ACORD standard files", "custom": "Company / custom model", "stories": "User stories"}
KIND_HELP = {
    "standard": "Master resource definition, master code list, deprecation list … (JSON or ZIP)",
    "custom": "Your company / custom model. Applied on top of ACORD and mapped to it (JSON or ZIP)",
    "stories": "User stories and their sample payloads (JSON or a ZIP with folders). "
               "A folder name such as Claims/ becomes the story's domain when the story does not declare one",
}

with st.sidebar:
    st.subheader("Add files to the project")
    kind = st.radio("What are you adding?", list(KIND_LABEL), format_func=KIND_LABEL.get, key="kind")
    st.caption(KIND_HELP[kind])
    up_key = f"up_{st.session_state.get('up_n', 0)}"
    ups = st.file_uploader("JSON or ZIP files", type=["json", "zip"], accept_multiple_files=True, key=up_key)
    if st.button("Save to project", type="primary", disabled=not ups, use_container_width=True):
        saved, notes = 0, []
        for f in ups:
            try:
                res = save_upload(kind, f.name, f.getvalue())
                saved += sum(1 for r in res if not r.note)
                notes += [f"{r.path.name}: {r.note}" for r in res if r.note]
            except Exception as exc:
                notes.append(f"{f.name}: not saved ({exc})")
        st.session_state["up_n"] = st.session_state.get("up_n", 0) + 1     # clears the uploader
        st.session_state["flash"] = (saved, notes)
        st.rerun()
    if "flash" in st.session_state:
        saved, notes = st.session_state.pop("flash")
        st.success(f"Saved {saved} file(s) to the project.")
        for n in notes:
            st.warning(n)

    st.divider()
    items = stored_items()
    st.subheader("Project data")
    st.caption(f"Stored in `{DATA_DIR}`")
    if not items:
        st.write("Nothing stored yet.")
    for k in KIND_LABEL:
        mine = [(p, n) for kk, p, n in items if kk == k]
        if not mine:
            continue
        with st.expander(f"{KIND_LABEL[k]} · {sum(n for _, n in mine)} JSON file(s)"):
            for p, n in mine:
                c1, c2 = st.columns([5, 1])
                c1.write(f"{'📁' if p.is_dir() else '📄'} {p.name}" + (f"  \n<small>{n} JSON files</small>" if p.is_dir() else ""),
                         unsafe_allow_html=True)
                if c2.button("✕", key=f"del_{p}", help=f"Remove {p.name} from the project"):
                    delete_item(p); st.rerun()
    if items:
        if st.checkbox("I want to remove all project data", key="wipe"):
            if st.button("Remove all", use_container_width=True):
                clear_all(); st.session_state["wipe"] = False; st.rerun()

    st.divider()
    show_cl = st.checkbox("Show code lists", True)
    show_st = st.checkbox("Show user stories", True)
    inc_dep = st.checkbox("Include deprecated items (greyed out)", False,
                          help="Off: anything marked deprecated, or listed in a deprecation file, is left out.")
    height = st.slider("Graph height", 500, 1400, 800, 50)


@st.cache_data(show_spinner="Reading project data…")
def _load(_fp: tuple, _v: int = 4) -> AcordModel:
    return load_store()


if not stored_items():
    st.info("No data in the project yet. Use **Add files to the project** in the sidebar: start with the ACORD "
            "master resource definition and master code list, then your company model and user stories (JSON or ZIP).")
    st.stop()

model = copy.deepcopy(_load(fingerprint()))   # analyze() edits the model; keep the cached copy clean
report = analyze(model, inc_dep)
maps = map_and_finalize(model, report)
domains = st.multiselect("Domains", model.domains(), default=model.domains())
g = build_graph(model, show_cl, show_st, domains)

c = st.columns(5)
c[0].metric("Entities", len(model.entities)); c[1].metric("Relationships", len(model.relationships()))
c[2].metric("Code lists", len(model.codelists)); c[3].metric("User stories", len(model.stories))
c[4].metric("Sample errors", sum(1 for s in model.stories.values() for f in s.findings if f.level == "error"))

tab_g, tab_m, tab_a, tab_cu, tab_c, tab_s, tab_q, tab_x = st.tabs(
    ["Graph", "Company ↔ ACORD mapping", "Attribute mapping", "Custom attributes", "Code lists", "User stories",
     "Data quality", "Export"])

with tab_m:
    if not maps:
        st.info("Add your company / custom model in the sidebar (choose **Company / custom model**) to see how it maps to ACORD.")
    else:
        map_html = to_mapping_html(model, maps)
        if hasattr(st, "iframe"):
            st.iframe(map_html, height=height + 120)
        else:
            components.html(map_html, height=height + 120, scrolling=False)
        ent_rows, attr_rows = mapping_rows(maps)
        with st.expander("Mapping tables"):
            st.dataframe(ent_rows, width="stretch", hide_index=True)
            st.dataframe(attr_rows, width="stretch", hide_index=True)

html = to_html(model, g)
with tab_g:
    if hasattr(st, "iframe"):          # Streamlit >= 1.52
        st.iframe(html, height=height)
    else:
        components.html(html, height=height, scrolling=False)

rows = attribute_rows(model)
with tab_a:
    c1, c2 = st.columns([3, 2])
    ent_filter = c1.multiselect("Entities", sorted(model.entities), key="ef")
    origin_filter = c2.multiselect("Origin", ["custom", "changed", "standard"], key="of",
                                   help="custom = new attribute from a custom file · changed = standard attribute the custom file overrides")
    view = [r for r in rows if r["Domain"] in domains and (not ent_filter or r["Entity"] in ent_filter)
            and (not origin_filter or r["Origin"] in origin_filter)]
    st.dataframe(view, width="stretch", hide_index=True, height=600)

with tab_cu:
    cust = [r for r in rows if r["Origin"] in ("custom", "changed")]
    if not cust:
        st.info("No custom files yet. Add them in the sidebar (choose **Company / custom model**).")
    else:
        k = st.columns(3)
        k[0].metric("Custom attributes", sum(r["Origin"] == "custom" for r in cust))
        k[1].metric("Changed standard attributes", sum(r["Origin"] == "changed" for r in cust))
        k[2].metric("Entities touched", len({r["Entity"] for r in cust}))
        st.dataframe([{k_: r[k_] for k_ in ("Domain", "Entity", "Attribute", "Origin", "Change", "Type", "Required",
                                             "Cardinality", "Related Entity", "Code List", "Description")} for r in cust],
                     width="stretch", hide_index=True)
        st.download_button("Download custom attributes (CSV)",
                           "\n".join([",".join(cust[0].keys())] + [",".join('"' + str(v).replace('"', '""') + '"' for v in r.values()) for r in cust]),
                           "custom_attributes.csv", "text/csv")

with tab_c:
    if model.codelists:
        name = st.selectbox("Code list", sorted(model.codelists))
        cl = model.codelists[name]
        st.write(cl.description or "")
        used = [f"{e.name}.{a.name}" for e in model.entities.values() for a in e.attributes.values() if a.codelist == name]
        st.write("**Used by:** " + (", ".join(used) or "—"))
        st.dataframe([{"Code": v.code, "Description": v.description} for v in cl.values],
                     width="stretch", hide_index=True)
    else:
        st.write("No code lists loaded.")

with tab_s:
    if not model.stories:
        st.info("No user stories yet. Add them in the sidebar (choose **User stories**, JSON or ZIP).")
    else:
        HOW = {"declared": "declared in the story", "folder": "folder name",
               "entities": "from the entities its samples use", "": "not determined"}
        by_dom: dict[str, list] = {}
        for s_ in model.stories.values():
            by_dom.setdefault(s_.domain or "Unassigned", []).append(s_)
        story_html = to_stories_html(model, DATA_DIR)
        if hasattr(st, "iframe"):
            st.iframe(story_html, height=height + 80)
        else:
            components.html(story_html, height=height + 80, scrolling=False)
        st.download_button("Download story graph (HTML)", story_html, "acord_stories.html", "text/html")
        st.markdown("**User stories by domain**")
        st.dataframe([{
            "Domain": d, "Stories": len(ss),
            "How the domain was found": ", ".join(sorted({HOW.get(x.domain_source, x.domain_source) for x in ss})),
            "Entities used": ", ".join(sorted({e for x in ss for e in x.used_entities}))[:300],
            "Sample errors": sum(1 for x in ss for f in x.findings if f.level == "error"),
            "Images / GIFs": sum(len(x.assets) for x in ss)} for d, ss in sorted(by_dom.items())],
            width="stretch", hide_index=True)
        pick = st.multiselect("Show domains", sorted(by_dom), default=[d for d in sorted(by_dom) if d in domains] or sorted(by_dom))
        q = st.text_input("Search stories", "", placeholder="id, title or entity")
        for s in sorted(model.stories.values(), key=lambda s: (s.domain, s.id)):
            if (s.domain or "Unassigned") not in pick:
                continue
            if q and q.lower() not in f"{s.id} {s.title} {' '.join(s.used_entities)}".lower():
                continue
            errs = sum(f.level == "error" for f in s.findings); warns = sum(f.level == "warning" for f in s.findings)
            with st.expander(f"{s.id} · {s.title} — {s.domain} · {errs} errors, {warns} warnings"):
                st.caption(f"Domain: **{s.domain}** ({HOW.get(s.domain_source, s.domain_source)}) · file: `{s.source}`")
                st.write(s.description)
                if s.acceptance_criteria:
                    st.markdown("\n".join(f"- {a}" for a in s.acceptance_criteria))
                if s.used_entities:
                    st.write("**Entities used:** " + ", ".join(sorted(s.used_entities)))
                if s.findings:
                    st.dataframe([{"Level": f.level, "Path": f.path, "Entity": f.entity, "Attribute": f.attribute,
                                   "Message": f.message} for f in s.findings], width="stretch", hide_index=True)
                for a_ in s.assets:
                    p_ = DATA_DIR / a_
                    if p_.exists():
                        st.image(str(p_), caption=p_.name)
                for i, smp in enumerate(s.samples):
                    st.caption(f"sample[{i}]"); st.json(smp, expanded=False)

with tab_q:
    st.subheader("Load log"); st.code("\n".join(model.load_log) or "—")
    st.subheader("Custom overlay")
    st.write(report["custom"] or "No custom files loaded.")
    st.subheader("Domain resolution")
    st.write(report["domains"] or "Every entity has a declared, inherited or inferred domain.")
    st.caption("Order used: declared in the file → OpenAPI tag → parent → user stories that use the entity "
               "(root of a sample counts 3, other entities 1) → connected entities → automatic grouping.")
    st.subheader("Deprecated items " + ("(included, greyed out)" if inc_dep else "(excluded)"))
    st.write(report["deprecated"] or "None")
    st.subheader("Undefined references"); st.write(report["dangling"] or "None")
    st.subheader("Code-list links"); st.write(report["codelist_notes"] or "All explicit code-list links resolved.")
    orphans = [n for n in model.entities if not any(r.source == n or r.target == n for r in model.relationships())]
    st.subheader("Entities with no relationships"); st.write(orphans or "None")

with tab_x:
    st.download_button("Interactive graph (HTML)", html, "acord_graph.html", "text/html")
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    st.download_button("Attribute mapping (CSV)", buf.getvalue(), "attribute_mapping.csv", "text/csv")
    if maps:
        ent_rows, attr_rows = mapping_rows(maps)
        st.download_button("Company ↔ ACORD mapping (HTML)", to_mapping_html(model, maps), "acord_mapping.html", "text/html")
        for nm, rr in (("company_mapping.csv", ent_rows), ("attribute_matches.csv", attr_rows)):
            b = io.StringIO(); w = csv.DictWriter(b, fieldnames=list(rr[0])); w.writeheader(); w.writerows(rr)
            st.download_button(nm, b.getvalue(), nm, "text/csv")
    st.download_button("Graph (node-link JSON)", json.dumps(to_json(g), default=str, indent=1), "acord_graph.json")
