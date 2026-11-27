# GhostPID

**English** · [Español](README.md)

A Python tool that runs locally to anonymize P&IDs and PFDs in PDF before
passing them to any LLM. It removes sensitive information (company, title
block, location, authorship, metadata, manufacturers, document references,
logos) and keeps the technical and process data (equipment, tags, lines,
sizes, design conditions, instrumentation, alarms, interlocks and operating
notes).

Anonymization is **deterministic** (rules, regex and coordinates). No LLM is
used to redact. Every redaction removes the real text with
`apply_redactions`; covering it up is not enough.

> **Download for Windows**: [latest release](https://github.com/osbertodiaz/GhostPID/releases/latest) (`GhostPID-windows-x64.zip`, no installation needed)
>
> **User manual** (installation, review app, rules and common problems): [docs/MANUAL.en.md](docs/MANUAL.en.md) · Español: [docs/MANUAL.md](docs/MANUAL.md)

## Language

The review UI, the desktop app, the output and `--help` of the commands, the
config error messages and the Markdown reports come out in **Spanish if the
operating system is in Spanish** (any `es_*`) and in **English otherwise**.
The system UI language is read (`AppleLanguages` on macOS,
`GetUserDefaultUILanguage` on Windows) and, failing that, the `LANGUAGE`,
`LC_ALL`, `LC_MESSAGES` and `LANG` variables. To force it:

```bash
GHOSTPID_LANG=en uv run validate-config configs/example.yaml
```

YAML keys, rule ids, categories (`company`, `manufacturer`…), blacklist CSV
columns, generic placeholders (`EPC-n`, `VENDOR-n`…), report JSON keys and
file names (`auditoria.md`) are not translated. The mechanism lives in
[src/sanitizer/i18n.py](src/sanitizer/i18n.py): each text is written in both
languages where it is used, `tr("texto", "text")`, with no catalogs to
compile or package. Tests force Spanish by default (`tests/conftest.py`).

## Requirements

- macOS (tested on Apple Silicon), Python 3.12
- [uv](https://docs.astral.sh/uv/) and Tesseract:

```bash
brew install uv tesseract
```

## Installation and tests

```bash
uv sync
uv run pytest
```

## PDF diagnostics (phase 1)

Read-only inventory: Info and XMP metadata, document IDs, attachments,
annotations, layers (OCG), JavaScript, fonts and the type of each page
(vector text, image only, image with a text layer, paths only).

It also detects:

- **Text converted to paths or outlines** (AutoCAD with SHX, PostScript): it
  groups letter-sized aligned paths. Pages with 50 glyphs or more count as
  "needs OCR". Thresholds scale with page size.
- **Invisible text**: OCR layers of scans and the text AutoCAD overlays on SHX
  paths.
- **Repeated images** (grouped by content hash): logo candidates.
- **Bookmark titles**, which often hold internal names.

The patterns found in real P&IDs are in
[docs/corpus-pid.md](docs/corpus-pid.md).

```bash
uv run inventory data/ --out data/reports --json
```

Without `--out`, the report goes to the screen. The report reproduces real
data (author, titles, layer names), so keep it only inside `data/`: if
`--out` points elsewhere, the command warns you.

## Per-client rules (phase 2)

Each client or template has a YAML config in `configs/` that inherits from
[configs/base.yaml](configs/base.yaml) (generic P&ID patterns). Start from
[configs/example.yaml](configs/example.yaml), which uses fictitious names:

```bash
cp configs/example.yaml configs/<client>.yaml
uv run validate-config configs/<client>.yaml --check "sample text"
```

Blocks:

- `whitelist`: what is always kept (tags, lines, sizes, design conditions,
  alarms, interlocks). It takes absolute priority over substitutions. By
  default a pattern only matches as a whole token: `PID-001` is not protected
  inside `ACM-2041-PID-001`. `exceptions` removes protection from tag-like
  forms that are not tags (e.g. a project code).
- `substitutions`: literal terms or regexes and their generic placeholder
  (`EPC`, `OPR`, `VENDOR-{n}`, `DOC-REF-{n}`…), by category. Their
  `exceptions` are literal texts that match the rule but are kept (false
  positives dismissed during review): they are compared whole, with flexible
  spacing, and do not count as leftovers in the audit either.
- `zones`: rectangles that are removed whole, as a page fraction or in mm,
  anchored to a corner and filterable by page and orientation.
- `detection`: detector thresholds. The defaults were calibrated on a sample
  corpus and are only a starting point.

Inheritance with `extends`: rules are merged by `id` (repeating an `id`
overrides only the given keys), and `disable: [id]` removes inherited rules.

`validate-config` checks the schema and each rule's examples, and detects
overlaps between whitelist and substitutions (it exits with code 1 on
errors). `--check` shows what each rule would do with a text. The inventory
accepts `--config` to use the client's detection thresholds.

**Client configs contain the names you want to hide**: git only tracks
`base.yaml` and `example.yaml`.

### Industry blacklist

On top of the client rules, the software already knows what to look for:
`configs/blacklist/industria.csv` is a public database of more than a
thousand companies that appear on P&IDs and PFDs in oil and gas, chemicals,
industrial gases, pharma and energy:

| Category (`category`) | What it includes | Placeholder |
|---|---|---|
| `epc` | EPC/EPCM engineering firms and contractors | `EPC-{n}` |
| `licensor` | technology licensors | `LICENSOR-{n}` |
| `manufacturer` | equipment, valve and instrument manufacturers and brands | `VENDOR-{n}` |
| `operator` | operators, owners and industrial gas companies | `OPR-{n}` |

`base.yaml` loads it through the `blacklist` block and turns it into the
`kb-epc`, `kb-licensor`, `kb-manufacturer` and `kb-operator` substitutions.
CSV columns: `name,aliases,category,segment,industries,country,active,notes`
(`,` or `;` separator, UTF-8; it edits well in Excel). Aliases are separated
by `|` and share the placeholder with the main name: "Endress+Hauser" and
"E+H" are the same `VENDOR-n`. A group's brands (Fisher, Rosemount) go in
their own rows so that `revert` returns the right name.

Safeguards, because a company name can be P&ID vocabulary:

- whole words only, also in OCR and in the audit ("Linde" does not match
  "CYLINDER");
- uppercase acronyms of up to 5 characters are case-sensitive;
- terms shorter than 3 characters, terms made only of P&ID vocabulary
  ("Shell", "Crane", "Total", "Weir", "Gate Valve") and duplicates across
  entities are rejected; the error gives the file and line.

```bash
uv run blacklist stats                       # summary by category and industry
uv run blacklist find valve                  # search by name, alias or segment
uv run blacklist check "FISHER 667 / SHELL SIDE"
uv run blacklist add "Name Inc" --category fabricante --aliases "NI|Name" --industries pharma
```

`add` (and **Add term…** in the UI with a `kb-…` rule) writes to
`configs/blacklist/propia.csv`, which git ignores: private names (clients,
plants) go there. Public industry names can be added to `industria.csv` and
tracked. All CSVs in `configs/blacklist/` are merged when the config loads.
In the client config:

```yaml
blacklist:
  industries: [pharma]   # only rows for these industries (and general ones)
  exclude: [Vega]        # terms not searched in this set
  enabled: false         # turn it off
substitutions:
  - id: kb-manufacturer  # list exceptions (false positives)
    exceptions: [Samson]
```

## Metadata cleanup (phase 3)

```bash
uv run clean-metadata data/drawing.pdf -o data/out/drawing.pdf [--config configs/<client>.yaml]
```

It removes Info and XMP (also the XMP of images and other objects), private
application data (`PieceInfo`, `OPI`), attachments, JavaScript and actions,
page labels, the structure tree and thumbnails. It replaces the document
`/ID` with a fixed value and rewrites the whole file, so old incremental
revisions disappear. Destinations pointing to pages outside the page tree
("ghost" pages) are dropped.

By default (configurable in the YAML `metadata` block):

- annotations: kept with their content, without author, dates, IDs, replies,
  popups or review-state marks;
- links: removed (`keep_internal` keeps those that go to another page);
- layers: `Capa-001`, `Capa-002`…, with their visible or hidden state;
- bookmarks: same structure with `Página N` titles;
- forms: kept, without XFA, with names `Campo-001`… and no tooltips.

It does not touch page content (that is phases 4 and 5). When done it
rereads the PDF with pikepdf and fails if anything is left
(`verify_metadata`).

## Text redaction (phase 4)

```bash
uv run redact data/drawing.pdf -o data/out/drawing.pdf --config configs/<client>.yaml \
    --report data/reports/drawing.redact.json
```

- Substitutions are searched over the text of each block, so they match even
  when a term is split across two lines. Anything touching the whitelist is
  blocked and reported.
- **Stacked tags** in instrument bubbles (`TI` over `01234`) are recognized by
  their geometry and protected.
- `apply_redactions` removes the real text. A narrow band through the middle
  of each character is used so neighbouring lines and overlapping text are
  not dragged along. The generic placeholder (`EPC`, `VENDOR-1`…) is written
  in its place, with the original orientation, also on rotated pages and
  vertical text.
- Depending on the page type (YAML `redaction` block): on normal text only
  the text is removed; on AutoCAD/SHX pages (invisible text over paths) the
  paths that draw those letters are removed too, editing the content
  precisely (`lineart.py`); on scans with OCR, the pixels under the text.
- Text in **hidden layers** is processed as well (its substitution goes into a
  hidden layer), as is the content of **annotations** and **form** values.
- On scanned pages, terms are also searched inside words that the OCR glued
  together (`ocr_relaxed_terms`).
- `{n}` numbering is consistent across the whole document.

When done the PDF is reread: no match may still be there and no protected
data may have disappeared. The report also lists **near misses**: what a rule
would find without word or token boundaries (e.g. `REF-001/002`), to tune the
client's rules.

Text converted to paths without a text layer, logos and images belong to
phase 5.

## Zones, logos and text as paths (phase 5)

```bash
uv run anonymize data/drawing.pdf -o data/out/drawing.pdf --config configs/<client>.yaml \
    --report data/reports/drawing.anon.json
```

It chains, in this order: zones → logos → text redaction (phase 4) → OCR.

- **Zones** (`zones` block): everything inside is removed, that is text, image
  pixels and paths entirely inside. A line that only enters the zone, such as
  the drawing frame, is kept. `label` writes an optional label in the emptied
  zone. **Vector logos** are handled with zones.
- **Image logos** (`logos` block): images are grouped by a perceptual hash, so
  the same image rotated or in grey counts as one. Those repeated on 3 pages
  or more and covering less than 15 % of the page are removed, plus those
  pinned in `logos.remove`. `logos.keep` excludes images. The report gives
  each group's hash.
- **OCR** (`ocr` block): applied to pages with text as paths and to scanned
  pages, including those that already carry an OCR layer. Tesseract reads at
  0°, 90° and 270° (`eng+spa` by default; needs `brew install
  tesseract-lang`). Rules are applied whitelist first and terms tolerate
  typical OCR confusions (`0/O`, `1/I/l`, `5/S`, `8/B`). Each match is erased
  (paths with the precise editor, pixels on scans) and the generic
  placeholder is written in its place.

## Substitution map and reversal (phase 6)

```bash
uv run anonymize data/drawing.pdf -o data/out/drawing.pdf --config configs/<client>.yaml \
    --map data/maps/<project>.map.json
uv run revert --map data/maps/<project>.map.json llm_answer.txt > answer.txt
```

- For each generic placeholder, the map stores the originals it replaces,
  with their rule, category and how many times they appeared (and how many
  were read from real text rather than OCR). It also includes the SHA-256 of
  each original and anonymized PDF.
- **It is only saved inside `data/`**, with 600 permissions: the code refuses
  to write it anywhere else, and checks this before starting work.
- If the same `--map` is passed when anonymizing another PDF, the numbering is
  reused: `VENDOR-1` is the same manufacturer across the whole project.
- `revert` replaces each placeholder with its original: longest first,
  case-insensitive and whole words only. If a placeholder groups several
  different entities (e.g. `SITE` for several locations), it uses the most
  frequent one read from real text and warns. `validate-config` warns about
  those rules: for an exact reversal, use `{n}` in `replacement`.
- Placeholders such as `EPC` are also common acronyms: if an LLM uses them in
  that sense, `revert` will replace them anyway. To avoid this, use something
  unambiguous in the config (e.g. `[EPC]`).

## Verification and audit (phase 7)

```bash
uv run audit data/out/drawing.final.pdf --config configs/<client>.yaml --out data/reports/drawing \
    --original data/drawing.pdf --anon-report data/reports/drawing.anon.json --map data/maps/<project>.map.json
```

It checks the final PDF regardless of how it was produced and exits with
code 1 if it finds leftovers:

- **Blacklist**: terms and patterns from the config and the original values
  from the map (those actually found). They are searched in the text of all
  layers, in annotations and forms, in **every object** of the PDF
  (decompressed streams, except images) and in the **OCR of every page**
  (`audit.ocr_pages`), which is what an LLM with vision sees.
- **Metadata**: reread with pikepdf.
- **Visual review**: one PNG per page with the touched areas highlighted by
  colour (substitution, OCR, zone, logo and leftovers found).
- **Remaining text to review**: lines in the title-block band
  (`audit.review_areas`) that are neither protected data nor generic
  placeholders, to catch names the rules do not know.
- `auditoria.md` and `auditoria.json` report with what was removed, how many
  times and on which pages. Since it contains original values, **it is only
  saved in `data/`**.

## Usage: full CLI (phase 8)

```bash
uv run sanitize data/project/ --config configs/<client>.yaml [--map data/maps/<project>.map.json]
```

For each PDF (files and folders are accepted; folders are walked
recursively):

1. `anonymize`: zones, logos, text and OCR, with a substitution map.
2. `clean-metadata`.
3. `audit`.
4. **Self-repair** (`--repair N`, 1 by default; 0 turns it off): leftovers the
   audit finds **through OCR** are fixed and the document is re-audited, with
   OCR only on the fixed pages. Leftovers in the text layer or over real text
   are not touched: they point to an incomplete rule and are reported.

Outputs:

- `data/out/<doc>.sanitized.pdf`: the PDF you can share.
- `data/reports/<doc>/`: `anonimizado.json`, `metadatos.json`,
  `auditoria.md` and `auditoria.json`, and the review PNGs.
- `data/maps/<doc>.map.json`, or the shared map from `--map`.

A summary table is printed at the end. The exit code is 1 if any document
fails the audit. A config with errors (`validate-config`) is rejected before
starting.

**Recommended cycle with a new client**: `inventory` → prepare the config →
`sanitize` → read the "remaining text to review" of each `auditoria.md` →
add to the rules whatever is a name → repeat.

## Review UI (phase 8)

```bash
uv run review data/drawing.pdf --config configs/<client>.yaml [--map data/maps/<project>.map.json] [--repair N]
```

A PyQt6 window to prepare a client's config while looking at the drawing:

- **See detections**: the original page with colour marks by type
  (substitution, zone, logo, audit leftover and warning, near miss, blocked
  by the whitelist, text to review). The list on the right filters by type
  and page; picking a row highlights it on the drawing (or searches for its
  text). **Show anonymized** switches to the anonymized version.
- **Discard** a false positive: an exception in the rule (`exceptions`), an
  image to keep (`logos.keep`) or a disabled zone (`disable`).
- **Add term** to an existing rule or create a new rule, with the text of the
  chosen row (e.g. a line of "text to review").
- **Draw zone**: drag a rectangle, saved as a page fraction, for this page,
  all pages or a list (`1, 3-7, -1`).
- **Confirm and generate**: the full `sanitize` process (with OCR, metadata,
  audit and self-repair), then opens the report.

On opening, the reports of a previous `sanitize` are loaded if they are newer
than the PDF and the config. Otherwise a **quick preview without OCR** runs:
seconds instead of minutes, but it does not see text as paths or scans.
**With OCR** repeats it in full. The preview works in
`data/reports/<doc>/revision/` with a copy of the map, so it does not touch
the real map.

Each change is written to the client config with ruamel.yaml, which keeps
comments and order, and leaves a `.bak` copy first. If the change would make
the config invalid or create overlaps, it is undone. After a change, the
detections are marked as out of date until **Recalculate**. The UI does not
edit `base.yaml` or `example.yaml`.

## Desktop app

**Windows**: download `GhostPID-windows-x64.zip` from the
[Releases](https://github.com/osbertodiaz/GhostPID/releases) page, unzip it
and open `GhostPID.exe`. Nothing needs installing: OCR (tesseract with
English and Spanish) is included. `data/` and `configs/` are created next to
the .exe. It is built by the `.github/workflows/windows.yml` workflow when a
`v*` tag is pushed; it can also be run by hand from Actions.

**macOS** (from source, with Homebrew's tesseract):

```bash
./scripts/build_app.sh   # → dist/GhostPID.app
```

The macOS app uses the project's `data/` and `configs/`, so it is opened from
`dist/` (do not move it to Applications). On start it asks for the PDF and
the client config, or creates one from the example. `GHOSTPID_SELFTEST=1`
checks the bundle without opening windows (modules, tesseract and OCR).

## Layout

```
src/sanitizer/   engines: metadata, text, zones, logos, verification
configs/         YAML rules per client or template
configs/blacklist/  industry blacklist (CSV)
tests/           tests with synthetic PDFs generated with reportlab
data/            real PDFs and substitution maps (outside git)
```

## Data

`data/` is in `.gitignore`. Real PDFs and substitution maps are never
committed. Tests only use synthetic PDFs.

## License

GhostPID's code is released under the [MIT License](LICENSE).

It uses third-party components under their own licenses. The downloadable
desktop app bundles PyMuPDF/MuPDF (AGPL-3.0) and PyQt6 (GPL-3.0), so the
bundled app, as a combined work, is distributed under the terms of the GNU
AGPL-3.0, and its complete source code is this repository. The list of
components and their licenses is in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The app shows it under
**About and licenses…**, and carries the full texts in `licenses/`.

## Roadmap

0. Environment · 1. Diagnostics · 2. Rules · 3. Metadata · 4. Text redaction ·
5. Zones, logos and vector text · 6. Substitution map · 7. Verification and
audit · 8. CLI and review UI · 9. Local LLM (optional)
