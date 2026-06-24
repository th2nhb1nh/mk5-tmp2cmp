# Jira TMP → CMP Migration Toolkit

A Python CLI that handles **Phase 3** of a team-managed (TMP) → company-managed
(CMP) Jira project migration: pulling configuration off the source project,
preserving each issue's original key in a custom field so post-move work can
join on it, rebuilding versions / components / sprints in the target project,
re-stitching sprint membership and field values after the bulk-move, validating
the result, and generating a polished Excel report for the project team.

It deliberately does **not** create schemes, workflows, screens, or global
custom fields — those need human design decisions and are best done in the
Jira UI. It also doesn't move the issues themselves; that's the native
**bulk-move** step you run in the UI during the cutover window.

---

## Table of contents

1. [What this toolkit does](#1-what-this-toolkit-does)
2. [Prerequisites](#2-prerequisites)
3. [Install](#3-install)
4. [Configure credentials](#4-configure-credentials)
5. [Step-by-step workflow](#5-step-by-step-workflow)
   - 5.0. [Run `doctor` first](#50-run-doctor-first)
   - 5.1. [Run `extract`](#51-run-extract)
   - 5.2. [Run `stamp-keys`](#52-run-stamp-keys)
   - 5.3. [Build the target shell + run `build`](#53-build-the-target-shell--run-build)
   - 5.4. [Bulk-move in the UI](#54-bulk-move-in-the-ui)
   - 5.5. [Run `update-sprints`](#55-run-update-sprints)
   - 5.5b. [Run `export-sprint-csv` (closed-sprint fallback)](#55b-run-export-sprint-csv-closed-sprint-fallback)
   - 5.6. [Run `copy-fields`](#56-run-copy-fields)
   - 5.7. [Run `validate`](#57-run-validate)
   - 5.8. [Run `report`](#58-run-report)
6. [Command reference](#6-command-reference)
7. [Output artifacts](#7-output-artifacts)
8. [Troubleshooting](#8-troubleshooting)
9. [Known limitations](#9-known-limitations)

---

## 1. What this toolkit does

| Phase | Subcommand | What it does |
|---|---|---|
| Preflight | `doctor` | Verifies your token works, you can see the project(s), lists boards, and resolves the Sprint custom field id. Run this first. No writes. |
| Source extract | `extract` | One pass over all source issues, plus version / component / sprint / role extraction and a full field catalog (with allowed values) built from `/editmeta`. Writes JSON + CSV. |
| Key preservation | `stamp-keys` | Writes each source issue's current key into a designated text custom field (`customfield_25199` by default) so the original key survives the bulk-move key change. |
| Target rebuild | `build` | Recreates versions, components, and sprints in the pre-built target CMP. Outputs ID-mapping CSVs used by the bulk-move wizard and by later phases. |
| Sprint re-stitch (API) | `update-sprints` | After bulk-move, rewrites the Sprint field on target issues from source sprint IDs to the new target sprint IDs (reopening/re-closing closed sprints as needed). |
| Sprint re-stitch (CSV) | `export-sprint-csv` | After bulk-move, emits a semicolon-delimited CSV (`sprint_import.csv`) mapping each target issue to its new sprint id(s), for Jira's **External System Import**. The reliable fallback when closed sprints block `update-sprints`. No writes — you import the CSV manually. |
| Field re-stitch | `copy-fields` | After bulk-move, copies field values from each source issue to its target counterpart, joining on the stamped key and a `field_mapping.csv` you provide. |
| Post-move check | `validate` | Compares source baseline counts against target post-move counts, plus optional sprint-membership and field-value sampling. |
| Reporting | `report` | Generates a formatted multi-sheet Excel workbook (`migration_report.xlsx`) for the project team, including a full source-key → target-key map. |

All write phases are **idempotent**: re-running skips work already done.
Versions / components / sprints dedup by name; `stamp-keys`, `copy-fields`, and
`update-sprints` each use a checkpoint file so an interrupted run resumes
cleanly.

---

## 2. Prerequisites

### 2.1 Python 3.10 or later

Check: `python3 --version`. If older, install a current Python.

### 2.2 An Atlassian API token

1. Sign in to https://id.atlassian.com/manage-profile/security/api-tokens with the account that will run the script.
2. Click **Create API token**, label it `tmp-to-cmp-migration`, copy the token.
3. Treat it like a password. It expires after the period set on creation.

The script authenticates as **the account that owns the token**. That account needs:
- **Browse Projects** + **Edit Issues** on the source TMP (for extract / stamp-keys).
- **Browse Projects** + **Administer Project** on the target CMP (for version/component creation and field edits).
- **Manage Sprints** on the target board (for sprint creation and the reopen/close cycle in `update-sprints`).

A site admin or org admin satisfies all of these by default. If you're using a non-admin token, verify each permission before running.

### 2.3 Source project setup (one-time, in the UI)

Before running `stamp-keys`, the text field you'll use to preserve original keys (default: `customfield_25199`) must be on **every edit screen** of **every issue type** in the source TMP. Without this, the API returns 200 OK on the update but silently drops the value.

In a TMP this means:
1. Source TMP → **Project settings** → **Issue types**.
2. For each issue type, drag the field onto the layout.
3. Save.

`stamp-keys` does a test-stamp-and-readback on the first issue and aborts with a clear error if the field isn't wired up — so you don't have to trust this manually.

### 2.4 Finding your IDs

| What | Where to find it |
|---|---|
| **Project key** | The prefix in any issue key (e.g. `TMP-123` → key is `TMP`). Also in `Project settings → Details`. Case-sensitive in the API. |
| **Board ID** | Open the board in the UI. URL is `/jira/software/projects/<KEY>/boards/<ID>` (or `?rapidView=<ID>`) — the trailing number is the board ID. `doctor` also prints board IDs for both projects. |
| **Account ID** (for `--target-lead`) | URL of a user's profile, or via API: `GET /rest/api/3/user/search?query=<email>`. |
| **Custom field ID** | Site admin → **Issues** → **Custom fields**, or call `GET /rest/api/3/field` and grep by name. Always starts with `customfield_`. The `extract` step's field catalog (§7) lists every project-scoped field with its id. |

---

## 3. Install

```bash
# Clone or copy the script directory, then:
cd mk5-tmp2cmp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Tested on Python 3.10+. Dependencies (see `requirements.txt`):
- `requests` — HTTP client.
- `python-dotenv` — auto-loads `.env` (optional but recommended).
- `openpyxl` — only needed for the `report` command's Excel output.

---

## 4. Configure credentials

The script reads its configuration from environment variables. If a `.env` file
is present it is **loaded automatically on every run** (via `python-dotenv`) —
you do **not** need to `source` it. Copy the template and fill it in:

```bash
cp .env.example .env
# edit .env
```

| Variable | Required? | Used by | Notes |
|---|---|---|---|
| `JIRA_BASE_URL` | Always | all | Site URL, no trailing slash. e.g. `https://your-site.atlassian.net` |
| `JIRA_EMAIL` | Always | all | Email of the account that owns the API token. |
| `JIRA_API_TOKEN` | Always | all | From id.atlassian.com (see §2.2). Token only — there is no `--token` flag. |
| `JIRA_SOURCE_KEY` | Per command | extract, stamp-keys, copy-fields, report | Source TMP project key. |
| `JIRA_TARGET_KEY` | Per command | build, update-sprints, export-sprint-csv, copy-fields, validate, report | Target CMP project key. |
| `JIRA_KEY_FIELD` | Optional | stamp-keys, copy-fields, validate, report | Text field for the preserved key. Default `customfield_25199`. |
| `JIRA_OUTPUT_DIR` | Optional | all | Artifacts directory. Default `./out`. |
| `JIRA_TARGET_BOARD_ID` | Optional | build | Target Scrum board id; required for sprint creation. |
| `JIRA_TARGET_LEAD` | Optional | build | Lead accountId; required only with `--create-target`. |
| `JIRA_TARGET_TEMPLATE` | Optional | build | Project template for `--create-target`. Defaults to Scrum (classic CMP). |

Every variable except the three auth values can also be passed as a CLI flag
(`--source-key`, `--target-key`, `--key-field`, `--output-dir`, etc.), which
takes precedence over the environment. The script refuses to start if any of
`JIRA_BASE_URL`, `JIRA_EMAIL`, or `JIRA_API_TOKEN` is missing.

---

## 5. Step-by-step workflow

The phases in order. Each section explains what to run, what to expect, and what
the command produces. The bulk-move itself (§5.4) is a manual UI step that sits
in the middle of the toolkit run.

### 5.0. Run `doctor` first

**Purpose:** Confirm the script can authenticate and see both projects before you invest time running anything real.

```bash
python3 tmp_to_cmp.py doctor \
    --source-key TMP \
    --target-key NEW \
    --key-field customfield_25199
```

`--target-key` and `--key-field` are optional. If you only have a source project so far, omit `--target-key`.

It authenticates, checks each project you pass, lists the boards on each (so you
can grab the **target board ID** for `build`), validates that the key field is a
text field, and resolves the Sprint custom field id (e.g. `customfield_10020`).

If anything fails, the script logs a structured error with the likely cause. See [§8 Troubleshooting](#8-troubleshooting).

---

### 5.1. Run `extract`

**Purpose:** Pull everything you need to (a) understand the source, (b) rebuild structural objects in the target, (c) decide field mappings, and (d) reconcile after bulk-move.

```bash
python3 tmp_to_cmp.py extract --source-key TMP
# ./out is the default output dir. To override, put --output-dir BEFORE the
# subcommand (it's a global option): python3 tmp_to_cmp.py --output-dir ./out extract --source-key TMPi
```

**What it does, in order:**

1. Pulls project metadata (name, lead, style).
2. Single pass over every issue, collecting distinct issue types, distinct (issue type, status) pairs, and counts by type, status, and resolution.
3. Pulls all versions, components, the board, all sprints (closed + active + future), board config, and project roles.
4. Runs five JQL approximate-count queries for boolean buckets (with attachments, comments, fixVersion, components, sprint) into the baseline.
5. Builds a **full field catalog**: every project-scoped field, with allowed values pulled from `/rest/api/3/issue/{key}/editmeta` on a sample issue per type.
6. Writes everything to `./out/extract/` as JSON (and CSV for the field catalog).

The field catalog (`12_source_field_catalog.csv`) is what you use to author the `field_mapping.csv` that `copy-fields` consumes.

**Inspect the output:** `cat ./out/extract/11_inventory_baseline.json` to see the numbers `validate` reconciles against later. Re-running overwrites the artifacts safely.

---

### 5.2. Run `stamp-keys`

**Purpose:** Bulk-move generates new issue keys in the target project. To survive that, every source issue gets its current key written into a text custom field. After the move you can join old-key → new-key by that field for field copies, audit trails, dashboards.

**Prerequisite check:** the key field must be on the edit screen of every source TMP issue type (see [§2.3](#23-source-project-setup-one-time-in-the-ui)).

Start with dry-run:

```bash
python3 tmp_to_cmp.py stamp-keys --source-key TMP --key-field customfield_25199 --dry-run
```

Then for real:

```bash
python3 tmp_to_cmp.py stamp-keys --source-key TMP --key-field customfield_25199
```

**What it does:**

1. Verifies the field exists and has a text (`string`/`any`) schema type.
2. Test-stamps the oldest issue and reads it back. **Aborts with a clear error if the value didn't persist.**
3. Asks you to confirm before running on the rest.
4. Walks every issue with paginated JQL, writing the key via `PUT /rest/api/3/issue/{KEY}?notifyUsers=false`.
5. Appends each successfully stamped key to `./out/stamp/stamped.txt` as it goes, sleeping 100 ms between writes.

**Interrupt and resume:** Ctrl-C is safe. Re-run the same command; it reads the checkpoint and skips already-stamped keys.

**Failures:** any keys that fail land in `./out/stamp/failures.csv` with the error. Investigate and re-run; only un-checkpointed keys retry.

**Notification noise:** `notifyUsers=false` suppresses email for the field update itself, but source TMP **automation rules** listening on "Issue Updated" can still fire. **Disable source automation before running.**

---

### 5.3. Build the target shell + run `build`

This step has **manual UI work first**, then the script.

#### 5.3.1 Manual: create the target CMP in the UI

Project creation via API gives no real control over scheme assignment, and you almost always want to assign existing standard schemes. Doing it in the UI gives you the scheme dialogs.

1. **Create project** → Company-managed → Software (Scrum/Kanban as appropriate).
2. **Project key**: pick a temporary key like `NEW`. You'll rename it to the original `TMP` key after bulk-move, once the source has been renamed (e.g. to `TMP_OLD`).
3. **Project settings → Schemes** — assign workflow, screen, field configuration, permission, and notification schemes (a dedicated empty "Silent Migration" notification scheme is recommended for the cutover window).
4. Add `customfield_25199` and any other global custom fields you'll map to the appropriate field configurations and screens.
5. Create a Scrum **board** on the target. Note its board ID (see §2.4).

#### 5.3.2 Run the build script

```bash
python3 tmp_to_cmp.py build --target-key NEW --target-board-id 99 --dry-run
```

After the dry-run looks clean, drop `--dry-run`:

```bash
python3 tmp_to_cmp.py build --target-key NEW --target-board-id 99
```

**What it does, in order:**

1. Confirms the target project exists and is accessible.
2. Reads the source extract artifacts.
3. **Versions** — dedup by name; otherwise create via `POST /rest/api/3/version` with dates, then patch `released`/`archived`. Writes `version_id_mapping.csv`.
4. **Components** — same dedup-by-name pattern via `POST /rest/api/3/component`. Writes `component_id_mapping.csv`.
5. **Sprints** — needs `--target-board-id`. Creates each source sprint on the target board with matching name, dates, and goal, then transitions it through `active` (and `closed`) to match the source state. Writes `sprint_id_mapping.csv`.

**The three mapping CSVs are inputs** to the bulk-move wizard (Fix Version / Component / Sprint mapping screens) and to the later `update-sprints` / `report` phases.

**Sprint closed-date caveat:** the Agile API sets `completeDate` to the moment you close a sprint via script — it cannot be backdated. Closed sprints recreated here show the recreation time.

**Re-running** is safe: existing versions/components/sprints (by name) are detected and skipped.

#### 5.3.3 (Optional) Have the script create the target project

If you can't create the target in the UI, the script can — but you'll still re-assign schemes manually afterward:

```bash
python3 tmp_to_cmp.py build \
    --target-key NEW \
    --create-target \
    --target-lead 5b10a2844c20165700ede21g \
    --target-template com.pyxis.greenhopper.jira:gh-simplified-scrum-classic \
    --target-board-id 99
```

`--target-lead` takes the lead user's accountId (see §2.4).

---

### 5.4. Bulk-move in the UI

This is **not** a script step. During the cutover window:

1. In the source TMP, select all issues and use Jira's native **bulk-move** to move them into the target CMP.
2. On the wizard's mapping screens, use the `version_id_mapping.csv`, `component_id_mapping.csv`, and `sprint_id_mapping.csv` from `build` to map fields correctly.
3. After the move, rename the projects: source → `TMP_OLD`, target → `TMP` (so the target inherits the original key).

The bulk-move carries issue history, comments, attachments, and worklogs. It does **not** preserve TMP-scoped custom field values or the original sprint membership in a usable form — that's what `update-sprints` and `copy-fields` repair next.

---

### 5.5. Run `update-sprints`

**Purpose:** After bulk-move, target issues may still reference the *source* sprint IDs (or lose sprint membership). This command rewrites the Sprint field on every target issue from source sprint IDs to the corresponding target sprint IDs, using `build/sprint_id_mapping.csv`.

```bash
python3 tmp_to_cmp.py update-sprints --target-key TMP --dry-run
python3 tmp_to_cmp.py update-sprints --target-key TMP
```

**Execution model:**

1. **Plan** — scan the target once, grouping issues by whether their target sprint set includes any *closed* sprints.
2. **Phase A** — issues whose target sprints are all non-closed are updated directly.
3. **Phase B** — for each closed target sprint with pending issues: reopen → update its issues → re-close. Only one closed sprint is reopened at a time (Jira disallows more than one active sprint unless parallel sprints are enabled on the board).

**Caveats** (logged and written to CSV):
- Re-closing a sprint stamps a new `completeDate` of *now*; the original is unrecoverable.
- Issues spanning **more than one** closed sprint are skipped and listed in `multi_closed_issues.csv` for manual handling.
- Issues referencing a source sprint with no target mapping are listed in `missing_sprint_map.csv`.

**Resume:** checkpointed in `./out/update_sprints/updated.txt`. Per-sprint cycle outcomes go to `sprint_cycle_log.csv`; failures to `failures.csv`.

> **If closed sprints get in your way:** the reopen/re-close cycle this command
> performs is fragile — it fails when the board disallows more than one active
> sprint, and it skips issues that belong to multiple closed sprints. When that
> happens, skip the API approach entirely and use
> [`export-sprint-csv`](#55b-run-export-sprint-csv-closed-sprint-fallback) (§5.5b)
> instead: it sets sprint membership by id through Jira's External Import, which
> has no such restriction on closed sprints.

---

### 5.5b. Run `export-sprint-csv` (closed-sprint fallback)

**Purpose:** `update-sprints` rewrites the Sprint field through the REST API,
which can only attach an issue to a *closed* sprint by temporarily reopening it.
That breaks down when a board forbids more than one active sprint, or when an
issue spans several closed sprints. Jira's built-in **External System Import**
sets the Sprint field by **sprint id** with no reopen/re-close dance, so it is
the reliable way to re-attach historical sprint membership. This command
produces the CSV that import consumes — it makes **no writes** to Jira; the
import itself is a manual UI step.

It reads the same `build/sprint_id_mapping.csv` as `update-sprints`, scans the
target project for every issue that still carries a sprint, maps the source
sprint ids to the target sprint ids, and writes one row per issue.

```bash
python3 tmp_to_cmp.py export-sprint-csv --target-key TMP
```

**Output:** `./out/export_sprint_csv/sprint_import.csv`, a **semicolon-delimited**
(`;`) CSV with these columns:

| Column | Notes |
|---|---|
| `Issue Key` | The live target issue key (used by import to match the issue). |
| `Assignee` | The assignee's email address (falls back to account id, then display name; blank if unassigned). |
| `Summary` | Issue summary (for human readability during the import field-mapping step). |
| `Project name` | Target project name. |
| `Project Key` | Target project key. |
| `Project Type` | Target project type key (e.g. `software`). |
| `Sprint ID` | The **target** sprint id(s). An issue in more than one sprint gets all its ids **comma-separated** in this single cell. |

The delimiter is a semicolon **on purpose**: the `Sprint ID` cell uses commas to
separate multiple ids, so a comma can't also be the field separator. Fields that
themselves contain a semicolon (e.g. a summary) are double-quoted per standard
CSV rules, so the file round-trips cleanly.

Issues whose source sprint has no target mapping are **omitted** from the import
CSV and listed in `./out/export_sprint_csv/missing_sprint_map.csv` for follow-up
(rerun `build` if a sprint is missing from the mapping).

**Importing into Jira:**

1. Jira → **Settings** → **System** → **External System Import** → **CSV**.
2. Upload `sprint_import.csv`. On the settings screen set the **delimiter to
   semicolon (`;`)**.
3. On the field-mapping screen, map `Issue Key` to **Issue key** (so import
   updates existing issues rather than creating new ones), `Sprint ID` to
   **Sprint**, and the remaining columns to their like-named fields.
4. Run the import and review Jira's import log.

This command is read-only and safe to re-run; it overwrites the CSV each time.

---

### 5.6. Run `copy-fields`

**Purpose:** Copy custom-field values that didn't survive the bulk-move from each source issue to its target counterpart, joining on the stamped key.

**Prerequisite:** author a mapping CSV at `./out/mapping/field_mapping.csv` with columns:

```
source_field_id,target_field_id,transform
customfield_10050,customfield_22001,copy
customfield_10051,customfield_22002,append_suffix
```

- `transform` is optional per row. Supported values: `copy` (default), `append_prefix` (writes `Original: <value>`), `append_suffix` (writes `<value> (migrated)`).
- Use the `extract` field catalog (`12_source_field_catalog.csv`) to find source field ids.

```bash
python3 tmp_to_cmp.py copy-fields --source-key TMP_OLD --target-key TMP --dry-run
python3 tmp_to_cmp.py copy-fields --source-key TMP_OLD --target-key TMP
```

**What it does:** finds every target issue with a stamped source key, fetches the mapped fields from the source issue, applies any transform, and writes them to the target via `PUT`. Checkpointed in `./out/copy_fields/copied.txt`; failures in `failures.csv`.

---

### 5.7. Run `validate`

After bulk-move (and the re-stitch steps), reconcile against the baseline:

```bash
python3 tmp_to_cmp.py validate --target-key TMP --key-field customfield_25199
```

**What it does:**

1. Loads the baseline from `./out/extract/11_inventory_baseline.json`.
2. Scans the target project with the same logic used in extract.
3. Computes per-category diffs and writes `target_inventory.json`, `validation_delta.json`, and `validation_summary.csv`.
4. If `build/sprint_id_mapping.csv` exists, compares per-sprint issue counts source-vs-target and writes `sprint_mismatches.json` on any mismatch.
5. If `mapping/field_mapping.csv` exists, samples up to 50 target issues and compares mapped field values against source, writing `field_mismatches.json` on any mismatch.

If aggregate diffs are non-zero the script logs which buckets are off; open `validation_delta.json` to see by how much before declaring success.

---

### 5.8. Run `report`

**Purpose:** Produce a polished, multi-sheet Excel workbook for the project team summarizing the whole migration.

```bash
python3 tmp_to_cmp.py report --source-key TMP_OLD --target-key TMP --key-field customfield_25199
```

Both `--source-key` and `--target-key` are required. It reads from `extract/`,
`build/`, `update_sprints/`, and (optionally) `validate/`, then re-queries the
target live for current inventory, per-sprint counts, and a full key map. The
output is `./out/report/migration_report.xlsx` with sheets:

- **Summary** — headline KPIs with OK/REVIEW/ATTENTION status, sprint cycle outcomes, and callouts.
- **Issue Counts** — source vs target by type / status / resolution (live Excel formulas).
- **Sprints** — per-sprint reconciliation with deltas.
- **Versions** / **Components** — present/missing checks.
- **Content Buckets** — attachment/comment/version/component/sprint coverage %.
- **Issue Key Map** — every migrated issue: source key → clickable target key, type, status, summary.
- **Exceptions** — unmapped sprints, multi-closed issues, update/copy failures, membership mismatches.
- **Methodology** — plain-language notes on caveats (including the sprint `completeDate` behavior).

Requires `openpyxl` (installed via `requirements.txt`).

---

## 6. Command reference

```
python3 tmp_to_cmp.py [--base-url URL] [--email EMAIL] [--output-dir DIR] [--verbose] <subcommand> [opts]

Global options:
  --base-url    Jira site URL (or set JIRA_BASE_URL).
  --email       Account email (or set JIRA_EMAIL).
  --output-dir  Artifacts directory. Default: ./out (or JIRA_OUTPUT_DIR).
  --verbose     Debug logging.

Subcommands:

  doctor [--source-key X] [--target-key Y] [--key-field customfield_25199]
      Auth + project access check, board listing, sprint-field resolution. No writes.

  extract --source-key X
      Extract source project + full field catalog to ./out/extract/.

  stamp-keys --source-key X [--key-field customfield_25199] [--dry-run]
      Write each source issue's key into the text custom field.
      Idempotent via ./out/stamp/stamped.txt.

  build --target-key Y [--target-board-id N]
        [--create-target --target-lead ACCOUNT_ID]
        [--target-template TEMPLATE_KEY] [--dry-run]
      Recreate versions, components, sprints in target. Writes mapping CSVs.

  update-sprints --target-key Y [--dry-run]
      Rewrite the Sprint field on target issues from source to target sprint IDs,
      reopening/re-closing closed sprints as needed. Needs build/sprint_id_mapping.csv.

  export-sprint-csv --target-key Y
      Emit a semicolon-delimited CSV (./out/export_sprint_csv/sprint_import.csv)
      mapping each target issue to its new sprint id(s), for Jira's External
      System Import. Read-only fallback for closed sprints. Needs
      build/sprint_id_mapping.csv.

  copy-fields --source-key X --target-key Y [--key-field customfield_25199] [--dry-run]
      Copy mapped field values from source to target, joining on the stamped key.
      Needs ./out/mapping/field_mapping.csv.

  validate --target-key Y [--key-field customfield_25199]
      Compare baseline (from extract) against current target; optional sprint
      and field-value checks.

  report --source-key X --target-key Y [--key-field customfield_25199]
      Generate ./out/report/migration_report.xlsx for the project team.
```

---

## 7. Output artifacts

After running all phases, `./out/` contains:

```
out/
├── migration.log                          # Append-only log of every run
├── extract/
│   ├── 01_project_meta.json
│   ├── 02_issue_types.json
│   ├── 03_statuses.json
│   ├── 04_versions.json
│   ├── 05_components.json
│   ├── 06_source_board.json
│   ├── 07_sprints.json
│   ├── 08_source_board_config.json
│   ├── 09_roles.json
│   ├── 11_inventory_baseline.json
│   ├── 12_source_field_catalog.json
│   └── 12_source_field_catalog.csv        # Use this to author field_mapping.csv
├── stamp/
│   ├── stamped.txt                        # Checkpoint: one issue key per line
│   └── failures.csv                       # If any
├── build/
│   ├── version_id_mapping.csv
│   ├── component_id_mapping.csv
│   └── sprint_id_mapping.csv
├── mapping/
│   └── field_mapping.csv                  # You create this (input to copy-fields)
├── update_sprints/
│   ├── updated.txt                        # Checkpoint
│   ├── missing_sprint_map.csv             # If any
│   ├── multi_closed_issues.csv            # If any (manual follow-up)
│   ├── sprint_cycle_log.csv               # Reopen/close outcomes per closed sprint
│   └── failures.csv                       # If any
├── export_sprint_csv/
│   ├── sprint_import.csv                  # Semicolon-delimited; for Jira External Import
│   └── missing_sprint_map.csv            # If any (issues with no target sprint mapping)
├── copy_fields/
│   ├── copied.txt                         # Checkpoint
│   └── failures.csv                       # If any
├── validate/
│   ├── target_inventory.json
│   ├── validation_delta.json
│   ├── validation_summary.csv
│   ├── sprint_mismatches.json             # If sprint mapping present and mismatched
│   └── field_mismatches.json              # If field mapping present and mismatched
└── report/
    └── migration_report.xlsx
```

---

## 8. Troubleshooting

### `Authentication failed` (401)

1. **Token validity.** Tokens expire. Generate a fresh one at https://id.atlassian.com/manage-profile/security/api-tokens.
2. **Email matches token owner.** `JIRA_EMAIL` must be the exact email of the account that created the token.
3. **Base URL / site access.** The account must have access to the specific `JIRA_BASE_URL` site. For a sandbox, ensure your account is provisioned there.

### Source/target project not accessible

- **Key case mismatch** → use the exact key the API reports (keys are case-sensitive).
- **Project exists but no BROWSE permission** → the token owner can't see it; add them to a project role or use a different token.
- **Wrong site** → browse to the project in the UI and confirm the key and site match `JIRA_BASE_URL`.

### `Test stamp failed: field not editable on issue type.`

The key field is not on the edit screen for that issue type in the source TMP. Add it to the layout for **every** issue type (see §2.3), save, and re-run.

### `Key field customfield_X must be a text field (type string/any).`

You picked a non-text field. The stamping target must be a single-line text field. Create one if needed and re-run with the correct `--key-field`.

### `Could not resolve Sprint custom field id`

The Sprint field is resolved at runtime from `/rest/api/3/field` by its schema key. This error means Jira Software (Agile) isn't enabled on the instance, or the token can't see agile fields. `update-sprints` and sprint-related steps need it.

### `429 Too Many Requests`

The client already retries with exponential backoff. If sustained, increase `SLEEP_BETWEEN_WRITES` at the top of `tmp_to_cmp.py`.

### `POST /rest/api/3/search/jql -> 400`

The new search endpoint validates strictly: it returns **no field data unless `fields` is set in the body**, rejects `startAt` (pagination is `nextPageToken`-based), and rejects a `null` first-page token. The toolkit handles all of these; the error log includes the full request body so you can see exactly what was sent.

### Sprint reopen fails with an active-sprint conflict

A board can have only one active sprint unless **parallel sprints** are enabled. `update-sprints` reopens one closed sprint at a time, but if another sprint is already active the reopen fails — enable parallel sprints on the target board and re-run.

### `Baseline not found. Run 'extract' first.` / `Sprint mapping not found. Run 'build' first.`

`validate`/`report` read `extract/11_inventory_baseline.json`; `update-sprints` reads `build/sprint_id_mapping.csv`. If you used a non-default `--output-dir`, point every command at the same one (it's a global option, before the subcommand).

---

## 9. Known limitations

1. **Sprint `completeDate` cannot be backdated.** The Agile API stamps it at the moment of close. Closed sprints created by `build`, and closed sprints re-closed by `update-sprints`, show the run time rather than the original close time. The reports flag affected sprints; historical Velocity may need recomputing.

2. **TMP-scoped custom field IDs are not portable.** `extract` catalogs every project-scoped field so you can author `field_mapping.csv`; `copy-fields` does the actual re-stitching, joining on the stamped key.

3. **Board configuration is exported to JSON for reference only.** Columns, swimlanes, quick filters, card layout — all manual rebuild. Usually a good chance to clean these up rather than copy blindly.

4. **Issues spanning multiple closed sprints are skipped by `update-sprints`** unless parallel sprints are enabled on the target board. They're listed in `multi_closed_issues.csv`. The robust workaround is `export-sprint-csv` (§5.5b): it sets sprint membership by id via Jira's External System Import, which has no reopen/active-sprint restriction, so closed and multi-closed sprints import cleanly.

5. **`stamp-keys` notification suppression** covers the field update itself only. Source automation rules listening on "Issue Updated" can still fire — **disable source TMP automation before running.**

6. **Schemes, workflows, screens, and global custom fields are out of scope.** Create the target shell and assign schemes in the UI (§5.3.1); the toolkit only rebuilds versions, components, and sprints.
