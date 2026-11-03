# ACORD Canonical Model Graph

Load ACORD JSON metadata (master object file, master code list and user stories)
and get an interactive graph grouped by domain, plus an attribute-level mapping sheet
and a check of every user-story sample against the model.

![screenshot](docs_screenshot.png)

## Quick start

```bash
pip install -r requirements.txt

# 1) Upload UI (drag and drop any mix of JSON files)
streamlit run app.py

# 2) Command line (files and/or folders, searched recursively)
python -m acord_graph path/to/master_objects.json path/to/codelists.json path/to/user_stories/ -o out
python -m acord_graph acord/ -o out -d Policy -d Claims     # only some domains
```

The CLI writes to `out/`:

| File | What it is |
|---|---|
| `graph.html` | Interactive graph. Self-contained, so it opens offline and can be emailed |
| `attribute_mapping.csv` | One row per entity attribute: domain, type, required, cardinality, related entity, code list, code values, description, user stories that use it |
| `story_findings.csv` | Sample-vs-model check for each user story |
| `graph.graphml` / `graph.json` | For Gephi, yEd or Neo4j import |

## What gets linked

| From → to | How it is detected |
|---|---|
| Entity → entity (composition, 0..1 / 1..1) | property with `$ref` to another definition |
| Entity → entity (association, 0..* / 1..*) | `type: array` with `items.$ref`, or a `relationships[]` block |
| Entity → parent (inheritance) | `allOf: [{$ref}]`, `extends`, `baseType` |
| Reference by id | `x-relationship: reference` on the property |
| Attribute → code list | explicit `x-codelist` / `codeList` / `codeListName` / `$ref` into a code-list path; **otherwise matched by name** (`stateProvCd` → `StateProvCd`, `countryCd` → `CountryCd`). The mapping sheet shows `explicit` or `by-name` |
| Attribute → inline values | `enum` |
| User story → entities | the story's sample payloads are walked through the model. Declared `entities` are used as a fallback |
| Domain | `x-acord-domain` / `domain` / `subjectArea` on the entity, on the file, or the **folder name** when you load a folder |

Required fields come from JSON Schema `required` (inherited ones included), or from
`required` / `mandatory: "Y"` / a `1..` cardinality in flat attribute lists.

## User-story sample checks

For every sample, the tool finds the root entity (from a wrapper key like `{"Policy": {...}}`,
an `entity` / `@type` hint, or the best attribute overlap with the story's declared entities),
then walks it recursively and reports:

* **error**: field is not an attribute of the entity; value is not in the code list or enum; the story declares an entity that does not exist
* **warning**: required attribute is missing; wrong JSON type; longer than `maxLength`; scalar where an array is expected; nested object named after an entity with no attribute linking to it (an implicit association)

## Custom / extension files

Load your own files as an overlay on the standard model: `-c custom/` on the CLI, or the
*Custom / extension files* uploader in the app. A file is also treated as custom if it has
`"x-custom": true` or a name containing custom / extension / overlay.

| Custom file contains | What happens |
|---|---|
| An entity with the **same name** as a standard one (e.g. `Address`) | Applied as an extension. New attributes are tagged **custom**, and attributes that differ are tagged **changed** with the old → new values (type, required, cardinality, code list, max length). The standard domain is kept. |
| `CustomAddress`, `Address_Ext`, `AddressExtension` ... with no `allOf` | Matched to the standard `Address` by name and linked as **extends** |
| `allOf: [{$ref: Address}]` | Extends `Address` and inherits its domain |
| A brand-new entity | Gets its domain from the rules below |

### How the home domain is decided
1. declared on the schema (`x-acord-domain`, `domain`, `tags[0]` ...)
2. file-level domain or folder name (folders such as `custom/` or `user_stories/` are ignored)
3. inherited from its parent (`allOf` / extends)
4. inferred from the entities that point to it, if they are all in one domain
5. **Shared**, if entities in several domains point to it

Every entity also gets **used in domains**: its home domain plus every domain that references it
(Address → Location, Party, Claims, Auto). With *Shared entities in every domain they're used in*
switched on, filtering to one domain still shows the shared entities it depends on.

## Deprecated items

By default, anything deprecated is **left out** of the graph, the mapping sheet and the code values. All other items are kept.

* Flag on the item: `deprecated: true` (also `x-deprecated`, or `status: deprecated/retired`) on a schema, a property, or a code value (`oneOf` const or code-list entry). Enum schemas can also list deprecated codes in `x-deprecated-enum`.
* A separate deprecation list file: any file with `deprecations` / `deprecatedItems` / `deprecationList`, or a list in a file whose name contains "deprecat". Entries can be strings (`"Party"`, `"Party.taxId"`, `"#/components/schemas/Party/properties/taxId"`) or objects (`{"resource": "Party", "property": "taxId"}`, `{"codeList": "StateProvCd", "code": "XX"}`).
* A property that points to a deprecated entity is treated as deprecated too.
* User-story samples that still use a deprecated attribute or code get a **warning** (not "unknown field").
* Use `--include-deprecated` (CLI) or the sidebar checkbox (app) to show them greyed out and struck through instead. The CLI writes the list to `out/deprecated.txt`. The app shows it under *Data quality*.

## Supported input shapes

Each file is offered to all three readers, so one file can hold objects, code lists and stories together.

```jsonc
// Objects: JSON Schema
{"$defs" | "definitions" | "components": {"schemas"}: { "Party": { "x-acord-domain": "Party", "properties": {...}, "required": [...] } } }
// Objects: flat list
{"domain": "Auto", "objects": [ { "name": "Vehicle", "attributes": [ {"name":"vin","dataType":"string","required":"Y","codeList":"..."} ],
                                  "relationships": [ {"target":"Claim","cardinality":"0..*"} ] } ] }
// Code lists
{"codeLists": { "StateProvCd": { "description": "...", "values": [ {"code":"TX","description":"Texas"} ] } } }
// User stories: one per file, or a list
{"userStories": [ { "id": "US-201", "title": "...", "asA": "...", "iWant": "...", "soThat": "...",
                    "entities": ["Claim"], "acceptanceCriteria": [...], "sample" | "samples": {...} } ] }
```

If your files use different key names, add them to the tuples at the top of
`acord_graph/loader.py` (`ENTITY_CONTAINERS`, `CODELIST_KEYS`, `STORY_CONTAINERS`, ...).
Nothing else needs to change.

## Project layout

```
acord_graph/
  model.py      normalized dataclasses (Entity, Attribute, Relationship, CodeList, UserStory)
  loader.py     JSON → model, tolerant of several shapes
  analyzer.py   code-list linking, dangling references, user-story sample mapping, attribute sheet
  graph.py      NetworkX graph + HTML / GraphML / JSON export
  cli.py        command line
  static/       HTML template + vis-network (bundled so the graph works offline)
app.py          Streamlit upload UI
samples/        small illustrative files (not ACORD-published content)
```

## Notes

* The files in `samples/` are made-up examples in ACORD style. They are not ACORD-published content.
* `vis-network` (Apache-2.0 / MIT) is bundled in `acord_graph/static/`.
