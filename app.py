"""Streamlit UI:  streamlit run app.py

Upload one or more ACORD JSON files (master object file, master code list,
user-story files - any mix) and explore the canonical model as a graph.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from acord_graph import AcordModel, analyze, attribute_rows, build_graph, load_json_bytes, to_html, to_json

st.set_page_config(page_title="ACORD Canonical Model Graph", layout="wide")
st.markdown("""<style>
.block-container {padding-top: 2.5rem; padding-bottom: 0; max-width: 100%;}
</style>""", unsafe_allow_html=True)

SAMPLES = Path(__file__).parent / "samples"

with st.sidebar:
    files = st.file_uploader("ACORD JSON files (standard)", type=["json"], accept_multiple_files=True)
    custom_files = st.file_uploader("Custom / extension files", type=["json"], accept_multiple_files=True,
                                    help="Applied on top of the standard model. Same-name entities become extensions.")
    use_samples = st.checkbox("Use bundled sample files", value=not files)
    hint = st.text_input("Default domain for files without one", "")
    st.divider()
    show_cl = st.checkbox("Show code lists", True)
    show_st = st.checkbox("Show user stories", True)
    inc_dep = st.checkbox("Include deprecated items (greyed out)", False,
                          help="Off: anything marked deprecated, or listed in a deprecation file, is left out.")
    height = st.slider("Graph height", 500, 1600, 900, 50)


@st.cache_data(show_spinner="Reading files…")
def _load(blobs: tuple[tuple[str, bytes, bool], ...], hint: str, _v: int = 3) -> AcordModel:
    model = AcordModel()
    for name, raw, custom in sorted(blobs, key=lambda b: b[2]):   # standard first, then custom overlay
        try:
            model.merge(load_json_bytes(raw, name, hint, custom=True if custom else None))
        except Exception as exc:  # show bad files instead of failing the app
            model.load_log.append(f"{name}: skipped ({exc})")
    return model


blobs = [(f.name, f.getvalue(), False) for f in files or []]
blobs += [(f.name, f.getvalue(), True) for f in custom_files or []]
if use_samples:
    blobs += [(p.name, p.read_bytes(), "custom" in p.parts) for p in sorted(SAMPLES.rglob("*.json"))]
if not blobs:
    st.info("Upload JSON files in the sidebar, or tick 'Use bundled sample files'.")
    st.stop()

import copy
model = copy.deepcopy(_load(tuple(blobs), hint))   # analyze() edits the model; keep the cached copy clean
report = analyze(model, inc_dep)
with st.sidebar:
    domains = st.multiselect("Domains", model.domains(), default=model.domains())
g = build_graph(model, show_cl, show_st, domains)

tab_g, tab_a, tab_c, tab_s, tab_q, tab_x = st.tabs(
    ["Graph", "Attribute mapping", "Code lists", "User stories", "Data quality", "Export"])

html = to_html(model, g)
with tab_g:
    components.html(html, height=height, scrolling=False)

rows = attribute_rows(model)
with tab_a:
    ent_filter = st.multiselect("Entities", sorted(model.entities), key="ef")
    view = [r for r in rows if r["Domain"] in domains and (not ent_filter or r["Entity"] in ent_filter)]
    st.dataframe(view, width="stretch", hide_index=True, height=600)

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
    for s in sorted(model.stories.values(), key=lambda s: s.id):
        if s.domain not in domains:
            continue
        errs = sum(f.level == "error" for f in s.findings); warns = sum(f.level == "warning" for f in s.findings)
        with st.expander(f"{s.id} · {s.title} — {s.domain} · {errs} errors, {warns} warnings"):
            st.write(s.description)
            if s.acceptance_criteria:
                st.markdown("\n".join(f"- {a}" for a in s.acceptance_criteria))
            st.write("**Entities used:** " + ", ".join(sorted(s.used_entities)) if s.used_entities else "")
            if s.findings:
                st.dataframe([{"Level": f.level, "Path": f.path, "Entity": f.entity, "Attribute": f.attribute,
                               "Message": f.message} for f in s.findings], width="stretch", hide_index=True)
            if s.samples:
                st.markdown("**Example data**")
            for i, smp in enumerate(s.samples):
                st.caption(f"Example {i + 1} of {len(s.samples)}"); st.json(smp, expanded=True)

with tab_q:
    st.subheader("Load log"); st.code("\n".join(model.load_log) or "—")
    st.subheader("Custom overlay")
    st.write(report.get("custom") or "No custom files loaded.")
    st.subheader("Domain resolution")
    st.write(report.get("domains") or "Every entity has a declared, inherited or inferred domain.")
    st.subheader("Deprecated items " + ("(included, greyed out)" if inc_dep else "(excluded)"))
    st.write(report.get("deprecated") or "None")
    st.subheader("Undefined references"); st.write(report.get("dangling") or "None")
    st.subheader("Code-list links"); st.write(report.get("codelist_notes") or "All explicit code-list links resolved.")
    orphans = [n for n in model.entities if not any(r.source == n or r.target == n for r in model.relationships())]
    st.subheader("Entities with no relationships"); st.write(orphans or "None")

with tab_x:
    st.download_button("Interactive graph (HTML)", html, "acord_graph.html", "text/html")
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    st.download_button("Attribute mapping (CSV)", buf.getvalue(), "attribute_mapping.csv", "text/csv")
    st.download_button("Graph (node-link JSON)", json.dumps(to_json(g), default=str, indent=1), "acord_graph.json")
