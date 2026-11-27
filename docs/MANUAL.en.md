# GhostPID user manual

GhostPID removes whatever identifies the client from P&IDs and PFDs in
PDF before you give them to an LLM. It removes companies, operator,
manufacturers, location, project codes, references to other documents,
authors, logos, title blocks and metadata. It keeps everything technical:
equipment, instrument tags, lines, sizes, design conditions, alarms,
interlocks and notes.

- **Everything happens on your computer.** Nothing is sent to the internet or
  to any LLM.
- **It is deterministic:** it decides with rules, patterns and zones that you
  control, not with artificial intelligence. With the same PDF and the same
  rules, the result is always the same.
- **Text is really deleted:** it is not just covered with a box. An LLM
  cannot recover it from the text layer or by "looking" at the image.

> **Language.** The app and the command-line tools follow the operating
> system language: Spanish if it is Spanish (any `es_*`), English otherwise.
> To force one, set the environment variable `GHOSTPID_LANG=en` or
> `GHOSTPID_LANG=es`. The Spanish manual is [MANUAL.md](MANUAL.md).

---

## 1. Installation

### Windows (download)

1. Download `GhostPID-windows-x64.zip` from the
   [Releases](https://github.com/osbertodiaz/GhostPID/releases) page.
2. Unzip it into a folder you can write to, for example
   `Documents\GhostPID`. Do not run it from inside the zip.
3. Open `GhostPID.exe`. The app is not signed, so Windows SmartScreen
   may warn you: click **More info** and then **Run anyway**.

Nothing else needs installing: OCR (Tesseract, with English and Spanish) is
included.

### macOS (from source)

You need [uv](https://docs.astral.sh/uv/) and Tesseract:

```bash
brew install uv tesseract tesseract-lang
git clone https://github.com/osbertodiaz/GhostPID.git
cd GhostPID
uv sync
./scripts/build_app.sh
```

The app ends up in `dist/GhostPID.app`. Open it from there: it works
with the project's `data/` and `configs/` folders, so do not move it to
Applications.

### Working folders

The first time it opens, the app creates these folders. On Windows they go
next to the `.exe`; on macOS, in the project folder.

| Folder | Contents |
|---|---|
| `data/` | Your original PDFs and everything that is generated |
| `data/out/` | **The anonymized PDFs**: the only thing you share |
| `data/reports/<document>/` | Anonymization and audit reports |
| `data/maps/` | Substitution maps (the real names and their replacement) |
| `configs/` | Rules: `base.yaml` and `example.yaml` (not edited) and one config per client |

> ⚠️ `data/` and `configs/<client>.yaml` contain the real names you want to
> hide. Do not share them, do not upload them anywhere and do not give them
> to an LLM. Share only the PDFs in `data/out/`.

---

## 2. Getting started

1. Copy the PDFs you want to anonymize to `data/`. This is optional, but it
   keeps them at hand.
2. Open the app. In the start dialog:
   - **Original PDF**: choose the drawing.
   - **Client config**: choose your client's or create one with **New…**,
     which starts from the example. One config per client works for all of
     their drawings.
   - **Self-repair passes**: leave 1. With scanned drawings with very small
     lettering, 2 helps.
3. Click **Open**.

The dialog also shows whether Tesseract was found. Without it OCR does not
work, and scanned drawings or drawings with text converted to paths cannot be
cleaned.

> The config created by **New…** contains **fictitious** names (ACME,
> Globex, Initech…). The first thing to do is replace them with your
> client's, from the app itself (section 3.4) or by editing the YAML
> (section 5).

---

## 3. The review window

On the left is the drawing page. On the right, the list of what the tool has
detected.

### 3.1 What you see when it opens

- If you already generated that document before, with the same config and
  the same PDF, those reports load instantly.
- If not, a **quick preview without OCR** runs, which takes seconds. This
  preview does not see the text in scanned drawings or text converted to
  paths (common in PDFs exported from AutoCAD). To see it, click **With
  OCR**, which can take several minutes on large documents.

### 3.2 Toolbar

| Button | What it does |
|---|---|
| ◀ ▶ and the number | Changes page |
| **Show anonymized** | Switches between the marked original and the anonymized version |
| **Fit**, ＋, － | Zoom. Ctrl + mouse wheel also works; to move around, drag the page |
| **Draw zone** | Drag a rectangle to delete that whole area (section 3.5) |
| **Recalculate** | Repeats the quick preview with the current config |
| **With OCR** | Repeats the preview with OCR (slow, but it sees everything) |
| **Confirm and generate** | Full process: creates the final PDF and the audit |

### 3.3 Colors and detection types

| Type | Color | Meaning |
|---|---|---|
| substitution | green | Sensitive text replaced by a generic one (`EPC`, `OPR`, `VENDOR-1`…) |
| zone | blue | Area deleted entirely (title block, margins…) |
| logo | purple | Image removed (logo repeated on several pages or marked in the config) |
| leftover | red | **The audit found something sensitive still in the PDF** |
| warning | orange | The audit is unsure; check it (e.g. a rotated OCR reading) |
| near miss | yellow | Something that almost matches a rule but was not replaced |
| blocked | grey | It matched a rule but was protected because it is technical data (a tag) |
| review | turquoise | Text left in the title block that no rule knows |

Above the list you can tick or untick each type. **This page only** limits
the list to the current page. When you pick a row, the drawing jumps to it
and highlights it in pink.

**Above all, check the *review*, *near miss* and *leftover* types.** That is
where the names your rules do not know yet usually show up: the full plant
name, an operator acronym, an address or an e-mail.

### 3.4 Fixing the rules

Every correction is saved immediately in the client config. A `.bak` copy is
made first and, if the change would make the config invalid, it is not
applied. After a correction the window warns that the detections are out of
date: click **Recalculate** (or **With OCR**) to see the effect.

**Discard a false positive.** Select the row and click **Discard**.
Depending on the type:

- *substitution* (or *leftover*/*warning* from a pattern): that text becomes
  an exception of the rule and will not be replaced again in any document;
- *logo*: that image will always be kept;
- *zone*: the zone is disabled.

**Add a term.** Select a row (for example, a *review* line with the plant
name) and click **Add term…**:

1. Adjust the text: keep only the name, without the rest of the line.
2. Choose the rule it belongs to. For example, `operator → OPR` for the
   owner, `site → SITE` for the location or `vendors → VENDOR-{n}` for a
   manufacturer.
3. If it fits none, choose **New rule…** and give an id (`plant_name`), a
   category and the replacement (`SITE`, or `PLANT-{n}` to number them).

Terms are matched as whole words, case-insensitively, and allow spaces or
line breaks between words.

**The industry blacklist.** The rules `kb-epc`, `kb-licensor`,
`kb-manufacturer` and `kb-operator` (marked *blacklist (CSV)* in the
drop-down) come from `configs/blacklist/industria.csv`, a database with more
than a thousand engineering firms, licensors, manufacturers and operators. If
you choose one of them when adding a term, the name is saved as a new row in
`configs/blacklist/propia.csv` and will apply to all clients. Discarding a
blacklist detection turns it into an exception for this client only. The CSV
can be opened and extended in Excel (see section 5).

### 3.5 Drawing zones

They are for what is not text, such as a vector logo or a stamp, or to delete
a whole block, such as the title block or a strip with file paths.

1. Click **Draw zone** and drag a rectangle over the drawing.
2. Choose where it applies: **this page only**, **all pages** or a list
   (`1, 3-7, -1`, where `-1` is the last page).
3. Optional: a text to write in the gap, such as `[TITLE BLOCK]`.

The zone is saved relative to the page, so it works for other drawings of the
same format even if the sheet size changes.

> The title block often also holds useful data, such as the revision number
> or the scale. If you want to keep it, instead of deleting the whole title
> block let the rules replace only the names.

### 3.6 Generating the final PDF

Click **Confirm and generate**. The full process does this:

1. Applies zones, logos, text rules and OCR.
2. Cleans the metadata: author, software, dates, layers, bookmarks,
   comments, forms and attachments.
3. Audits the result independently of the process: it looks for leftovers
   in the text, in the PDF's internal objects, in the metadata and with OCR
   of every page.
4. Automatically fixes the leftovers OCR finds and audits again.

It takes from minutes to over half an hour, depending on the number of pages
and whether they are scanned. When it finishes, the window shows the
anonymized version and the log shows the result:

- **OK**: no leftovers. The PDF is in `data/out/<document>.sanitized.pdf`.
- **with FAILURES**: there are leftovers (red *leftover* rows). Fix the rules
  (section 3.4) and generate again.

**Open audit report** shows `auditoria.md`, with what was removed, how many
times and on which pages, and the text worth reviewing. In
`data/reports/<document>/paginas/` there is one PNG per page with the touched
areas highlighted.

> **Before sharing the PDF, open it and look at it.** The tool finds what the
> rules know and what OCR reads, but a name that is not in the rules and that
> OCR misreads can stay. The *review* list exists precisely for that.

---

## 4. Recommended workflow with a new client

1. Create their config with **New…** and replace the fictitious names with
   the real ones: engineering company, operator, manufacturers, plant, city
   and people.
2. Open a representative drawing and click **With OCR**.
3. Check the *review*, *near miss* and *leftover* rows: add terms and
   discard false positives.
4. Draw zones for vector logos or blocks you want to delete entirely.
5. **Recalculate** until the preview convinces you and then **Confirm and
   generate**.
6. With the config tuned, the client's other drawings usually come out right
   the first time. To process them in batch, use the command line
   (section 6).

---

## 5. The client config (YAML)

It is a text file in `configs/<client>.yaml`. The app edits it for you, but
you can also open it with any text editor. It inherits from `base.yaml`,
which brings the whitelist of technical data, so it only contains what is
specific to the client. The YAML keys, rule ids and categories are the same
in both languages.

```yaml
version: 1
name: My client
extends: base.yaml

substitutions:
  - id: operator                 # rule identifier
    category: operator           # company, operator, manufacturer, person,
                                 # location, project, document_ref, other
    replacement: OPR             # generic written in its place
    terms: [Globex Petroleum, Globex]

  - id: vendors
    category: manufacturer
    replacement: VENDOR-{n}      # {n}: a different number per manufacturer
    terms: [Initech Pumps, Umbrella Valves]

  - id: document_refs
    category: document_ref
    replacement: DOC-REF-{n}
    patterns: ['ACM-\d{4}-[A-Z]{3}-\d{3}']   # regular expression
    examples: [ACM-2041-PID-001]              # required with patterns
    exceptions: [ACM-0000-PID-000]            # matches that are NOT replaced

  - id: people
    category: person
    replacement: ''              # empty: deleted without a replacement
    terms: [John Doe, J. Doe]

zones:
  - id: title_block
    rect: [0.0, 0.0, 0.22, 0.30] # x0, y0, x1, y1 as page fractions
    anchor: bottom-right         # reference corner
    pages: all                   # or [1, "3-7", -1]
    label: "[TITLE BLOCK]"       # optional

logos:
  keep: []                       # images never removed
  remove: []                     # images always removed
```

Key points:

- **Whitelist:** instrument and equipment tags, line numbers, sizes, design
  conditions, alarms and interlocks are protected in `base.yaml` and take
  priority over any substitution.
- **Patterns:** for things with a fixed shape, such as drawing or project
  numbers, use `patterns` with a regular expression and always put real
  examples of the format in `examples`. The tool checks that they match.
- **Inherited rules:** to adjust a rule from `base.yaml` or the example,
  repeat its `id` with the keys that change. To remove it, use
  `disable: [id]`.
- **Check a config** from the command line:
  `uv run validate-config configs/<client>.yaml --check "test text"`.
- **Industry blacklist:** `configs/blacklist/*.csv`, one row per company with
  the columns `name` (main name), `aliases` (other forms separated by `|`),
  `category` (`epc`, `licensor`, `manufacturer`, `operator`, or in Spanish
  `ingenieria`, `licenciante`, `fabricante`, `operador`), `segment`,
  `industries` (`oil_gas`, `chemicals`, `gases`, `pharma`, `power`,
  `general`), `country`, `active` (`no` disables it) and `notes`. On saving,
  the tool rejects names shorter than 3 characters, P&ID words ("Shell",
  "Total", "Crane") and repeated names, giving the file and line. For a
  specific client: `blacklist: {industries: [pharma], exclude: [Vega]}` in
  its YAML.

---

## 6. Command line (optional, from source)

To process many PDFs at once or integrate it into other processes:

```bash
# All PDFs in a folder, with a shared map for the project
uv run sanitize data/project/ --config configs/<client>.yaml --map data/maps/project.map.json

# The review UI from the terminal
uv run review data/drawing.pdf --config configs/<client>.yaml

# Put the real names back into an LLM answer
uv run revert --map data/maps/project.map.json llm_answer.txt > answer.txt
```

`sanitize` prints a summary table at the end and exits with code 1 if any
document fails the audit. Each command's options are shown with `--help`.

### The substitution map and `revert`

Each run saves in `data/maps/` which original corresponds to each generic
(`VENDOR-1` → the real manufacturer). So:

- With the same `--map` for all the drawings of a project, `VENDOR-1` is the
  same manufacturer in all of them.
- When the LLM answers talking about `VENDOR-1` or `OPR`, `revert` puts the
  real names back into its answer.

For an exact reversal, use `{n}` in rules that group several different
entities. For example, `SITE-{n}` instead of `SITE` if there are several
locations. `revert` is only available from the command line.

---

## 7. Common problems

| Problem | Solution |
|---|---|
| Windows will not open the app | SmartScreen: **More info → Run anyway** |
| "tesseract not found" | Windows: use the full zip, which includes it. macOS: `brew install tesseract tesseract-lang` |
| The preview shows nothing on a scanned drawing | It is the quick preview without OCR: click **With OCR** |
| Technical data was replaced by mistake | Select it and click **Discard** |
| A name keeps appearing | Add it as a term (section 3.4); if it is a vector logo, draw a zone |
| Leftovers come back after generating on scans with very small lettering | Raise the **self-repair passes** to 2 |
| "Change rejected" when adding something | The change would make the config invalid (e.g. a term that is also an exception). Read the message; the config was not touched |
| The process is slow | It is normal with many scanned pages: OCR is repeated so nothing is left behind. While tuning rules, use **Recalculate** (no OCR) |
| I want to undo the last config change | Copy `configs/<client>.yaml.bak` over `configs/<client>.yaml` |
| The app is in the wrong language | Set `GHOSTPID_LANG=en` (or `es`) before opening it |

---

## 8. Privacy good practice

- Share only `data/out/*.sanitized.pdf`. Never `data/reports/`, `data/maps/`
  or your client config: they contain the real names.
- Check every PDF visually before sending it to an LLM, especially the first
  time you work with a client.
- If a drawing has sensitive information out of the ordinary, such as a
  stamp, a scanned signature or a photo, cover it with a zone.
- What the tool does not know, it cannot remove: keep each client's config
  up to date.
