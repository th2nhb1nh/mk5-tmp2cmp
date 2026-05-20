# Jira TMP → CMP Migration Toolkit

A Python CLI that handles **Phase 3** of a team-managed → company-managed Jira
project migration: pulling configuration off the source project, preserving
each issue's original key in a custom field so post-move CSV imports can
join on it, and rebuilding versions / components / sprints in the
pre-built target project.

It deliberately does **not** create schemes, workflows, screens, or global
custom fields — those need human design decisions and are best done in the
Jira UI. It also doesn't move issues; that's the bulk-move step you'll run
in the UI during the cutover window.

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
   - 5.4. [Run `validate` after bulk-move](#54-run-validate-after-bulk-move)
6. [Command reference](#6-command-reference)
7. [Output artifacts](#7-output-artifacts)
8. [Troubleshooting](#8-troubleshooting)
9. [Known limitations](#9-known-limitations)

---

## 1. What this toolkit does

| Phase | Subcommand | What it does |
|---|---|---|
| Preflight | `doctor` | Verifies your token works and you can see the project(s). Run this first, before anything else. |
| Source extract | `extract` | One pass over all source issues, plus version/component/sprint/role/field extraction. Writes JSON + CSV. |
| Key preservation | `stamp-keys` | Writes each source issue's current key into a designated text custom field (`customfield_25199` by default) so the original key survives the bulk-move key change. |
| Target rebuild | `build` | Recreates versions, components, and sprints in the pre-built target CMP. Outputs ID mapping CSVs to use in the bulk-move wizard. |
| Post-move check | `validate` | Compares source baseline counts against target post-move counts. |

All phases are **idempotent**: re-running skips work already done (versions/components/sprints dedup by name; stamp-keys uses a checkpoint file).

---

## 2. Prerequisites

### 2.1 Python 3.10 or later

Check: `python3 --version`. If older, install a current Python.

### 2.2 An Atlassian API token

1. Sign in to https://id.atlassian.com/manage-profile/security/api-tokens with the account that will run the script.
2. Click **Create API token**, label it `tmp-to-cmp-migration`, copy the token.
3. Treat it like a password. It expires after the period set on creation.

The script authenticates as **the account that owns the token**. That account needs:
- **Browse Projects** on the source TMP
- **Browse Projects** + **Administer Project** on the target CMP (for version/component creation)
- **Edit Issues** on the source TMP (for stamp-keys)
- **Manage Sprints** on the target board (for sprint creation)

A site admin or org admin satisfies all of these by default. If you're using a non-admin token, verify each permission before running.

### 2.3 Source project setup (one-time, in the UI)

Before running `stamp-keys`, the text field you'll use to preserve original keys (default: `customfield_25199`) must be on **every edit screen** of **every issue type** in the source TMP. Without this, the API will return 200 OK on the update but silently drop the value.

In a TMP this means:
1. Source TMP → **Project settings** → **Issue types**.
2. For each issue type, drag the field onto the layout.
3. Save.

The script's `stamp-keys` command does a test-stamp-and-readback on the first issue and aborts with a clear error if the field isn't wired up — so you don't have to trust this manually.

### 2.4 Finding your IDs

| What | Where to find it |
|---|---|
| **Project key** | The prefix in any issue key (e.g. `TMP-123` → key is `TMP`). Also in `Project settings → Details`. Case-sensitive in the API. |
| **Board ID** | Open the board in the UI. URL is `/jira/software/projects/<KEY>/boards/<ID>` — the trailing number is the board ID. |
| **Account ID** (for `--target-lead`) | URL of a user's profile: `/jira/people/<accountId>`. Or via API: `GET /rest/api/3/user/search?query=<email>`. |
| **Custom field ID** | Site admin → **Issues** → **Custom fields**, hover the field, look at the URL fragment, or call `GET /rest/api/3/field` and grep by name. Always starts with `customfield_`. |

---

## 3. Install

```bash
# Clone or copy the script directory, then:
cd tmp_to_cmp
python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

Tested on Python 3.10+. The only external dependency is `requests`.

---

## 4. Configure credentials

The script reads three environment variables:

| Variable | Example | Notes |
|---|---|---|
| `JIRA_BASE_URL` | `https://opswat-sandbox-113.atlassian.net` | Site URL. No trailing slash. |
| `JIRA_EMAIL` | `binh.pham@opswat.com` | Email of the account that owns the API token. |
| `JIRA_API_TOKEN` | (long opaque string) | From id.atlassian.com (see §2.2). |

Easiest: copy `.env.example` to `.env`, fill in values, then:

```bash
source .env
```

before running the script. The script will refuse to start if any of the three are missing.

---

## 5. Step-by-step workflow

The four phases in order. Each section explains what to run, what to expect on screen, and what the command produces.

### 5.0. Run `doctor` first

**Purpose:** Confirm the script can authenticate and see both projects before you invest time running anything real.

```bash
python tmp_to_cmp.py doctor \
    --source-key TMP \
    --target-key NEW \
    --key-field customfield_25199
```

`--target-key` and `--key-field` are optional. If you only have a source project so far, omit `--target-key`.

**Expected output (success):**

```
2026-05-21 09:00:01 INFO    ======================================================================
2026-05-21 09:00:01 INFO    Preflight
2026-05-21 09:00:01 INFO    ======================================================================
2026-05-21 09:00:02 INFO    Authenticated OK
2026-05-21 09:00:02 INFO      User:      Binh Pham
2026-05-21 09:00:02 INFO      Email:     binh.pham@opswat.com
2026-05-21 09:00:02 INFO      AccountId: 5b10a2844c20165700ede21g
2026-05-21 09:00:02 INFO      Site:      https://opswat-sandbox-113.atlassian.net
2026-05-21 09:00:02 INFO    Source project 'TMP' OK
2026-05-21 09:00:02 INFO      Name:  My Team Managed Project
2026-05-21 09:00:02 INFO      Style: next-gen
2026-05-21 09:00:02 INFO      Type:  software
2026-05-21 09:00:03 INFO    Target project 'NEW' OK
2026-05-21 09:00:03 INFO      Name:  My Company Managed Project
2026-05-21 09:00:03 INFO      Style: classic
2026-05-21 09:00:03 INFO      Type:  software
2026-05-21 09:00:03 INFO    ======================================================================
2026-05-21 09:00:03 INFO    Boards on source 'TMP':
2026-05-21 09:00:03 INFO      id=42  type=scrum   name=TMP board
2026-05-21 09:00:04 INFO    Boards on target 'NEW':
2026-05-21 09:00:04 INFO      id=99  type=scrum   name=NEW board
2026-05-21 09:00:04 INFO    Key field OK: customfield_25199 = 'Original Issue Key' (type=string)
2026-05-21 09:00:04 INFO    Doctor done. All checks above are green => safe to run extract.
```

**Note the board IDs** — you'll need the target board ID for the build step.

If anything fails, the script prints a structured error with the likely cause and remediation. See [§8 Troubleshooting](#8-troubleshooting).

---

### 5.1. Run `extract`

**Purpose:** Pull all the data you need to (a) understand what's in the source, (b) rebuild structural objects in the target, and (c) reconcile after bulk-move.

```bash
python tmp_to_cmp.py extract --source-key TMP --output-dir ./out
```

**What it does, in order:**

1. Pulls project metadata (name, lead, style).
2. Single pass over every issue, collecting:
   - Distinct issue types used.
   - Distinct (issue type, status) pairs.
   - Counts by type, status, and resolution.
3. Pulls all versions, components, the board, all sprints (closed + active + future), board config, project roles + members.
4. Samples 500 issues with `fields=*all` to catalog every custom field that has data.
5. Runs five JQL approximate-count queries for boolean buckets (with attachments, with comments, with fix version, with components, with sprint).
6. Writes everything to `./out/extract/` as JSON and CSV.

**Expected output (key lines):**

```
EXTRACT: source=TMP output=/.../out/extract
Wrote /.../out/extract/01_project_meta.json
Scanning all issues (single pass)...
  scanned 500 issues...
  scanned 1000 issues...
Wrote /.../out/extract/02_issue_types.json
...
Sampling custom fields in use...
Wrote /.../out/extract/11_inventory_baseline.json
EXTRACT complete. 1247 issues scanned. Artifacts in /.../out/extract
```

**Inspect the output:** `cat ./out/extract/11_inventory_baseline.json` to see the numbers you'll be reconciling against later. Save this file — `validate` reads it.

**Re-running** overwrites the artifacts safely.

---

### 5.2. Run `stamp-keys`

**Purpose:** Bulk-move generates new issue keys in the target project. To survive that, every source issue gets its current key written into a text custom field. After the move, you can join old-key → new-key by that field for CSV imports, audit trails, dashboards.

**Prerequisite check:** `customfield_25199` (or whatever field you specified) must be on the edit screen of every source TMP issue type (see [§2.3](#23-source-project-setup-one-time-in-the-ui)).

Start with dry-run:

```bash
python tmp_to_cmp.py stamp-keys \
    --source-key TMP \
    --key-field customfield_25199 \
    --dry-run
```

Then for real:

```bash
python tmp_to_cmp.py stamp-keys \
    --source-key TMP \
    --key-field customfield_25199
```

**What it does:**

1. Verifies the field exists and has `schema.type=string`.
2. Test-stamps the oldest issue and reads it back. **Aborts with a clear error if the value didn't persist.**
3. Asks you to confirm before running on the rest.
4. Walks every issue with paginated JQL search. For each one calls
   `PUT /rest/api/3/issue/{KEY}?notifyUsers=false`
   with body `{"fields": {"customfield_25199": "<KEY>"}}`.
5. Writes each successfully stamped key to a checkpoint file as it goes.
6. Sleeps 100 ms between writes to stay under Cloud rate limits.

**Expected output (key lines):**

```
STAMP-KEYS: TMP -> customfield_25199
Key field OK: customfield_25199 = 'Original Issue Key' (type=string)
Test stamp on TMP-1
Test stamp verified.
Approximate total issues: 1247
About to stamp customfield_25199 on ~1247 issues in TMP. Continue? [y/N]: y
Stamped 100 / ~1247 ...
Stamped 200 / ~1247 ...
...
STAMP-KEYS complete. Processed 1247 issues this run.
```

**Notification noise:** the `notifyUsers=false` parameter on the PUT suppresses email for the field update. **Important**: source TMP **automation rules** listening on "Issue Updated" can still fire and generate notifications. Disable source automation rules before running.

**Interrupt and resume:** Ctrl-C is safe. Re-run the same command; the script reads the checkpoint and skips already-stamped keys.

**Failures:** any keys that fail land in `./out/stamp/stamp_failures.csv` with the error. Investigate and re-run; only failed keys retry.

---

### 5.3. Build the target shell + run `build`

This step has **manual UI work first**, then the script.

#### 5.3.1 Manual: create the target CMP in the UI

Why not script it? Project creation via API gives you no real control over scheme assignment, and you almost always want to assign existing standard schemes rather than the defaults Atlassian picks. Doing it in the UI gives you the scheme dialogs.

In the Jira UI:

1. **Create project** → Company-managed → Software (Scrum/Kanban as appropriate).
2. **Project key**: pick a temporary key like `NEW` for now. You'll rename it to the original `TMP` key after bulk-move, once the source has been renamed to `TMP_OLD`.
3. **Project settings → Schemes** — assign:
   - Workflow scheme: standard one matching the source's intent, or your org default.
   - Screen scheme & Issue type screen scheme: ones that expose all migrated fields on create/view/edit.
   - Field configuration scheme.
   - Permission scheme.
   - **Notification scheme: a dedicated empty "Silent Migration" scheme** for the duration of cutover (more in the migration playbook).
   - Issue security scheme: only if needed.
4. Add `customfield_25199` and any other global custom fields you'll need to the appropriate field configurations and screens.
5. Create a Scrum **board** on the target. Note its board ID (see §2.4).

#### 5.3.2 Run the build script

```bash
python tmp_to_cmp.py build \
    --target-key NEW \
    --target-board-id 99 \
    --dry-run
```

After the dry-run looks clean:

```bash
python tmp_to_cmp.py build \
    --target-key NEW \
    --target-board-id 99
```

**What it does, in order:**

1. Confirms the target project exists and is accessible.
2. Reads source extract artifacts.
3. **Versions** — for each source version, if a version with the same name already exists in target, records the mapping and skips. Otherwise creates it via `POST /rest/api/3/version` with start/release dates, then patches `released`/`archived` flags. Writes `version_id_mapping.csv`.
4. **Components** — same dedup-by-name pattern. Creates via `POST /rest/api/3/component` with lead and assignee type. Writes `component_id_mapping.csv`.
5. **Sprints** — needs `--target-board-id`. For each source sprint, creates a sprint on the target board with matching name, start/end dates, and goal. If the source sprint was active or closed, transitions the new sprint through `active` (and `closed`) to match. Writes `sprint_id_mapping.csv`.

**Expected output (key lines):**

```
BUILD: target=NEW
Wrote /.../out/build/version_id_mapping.csv (12 rows)
Wrote /.../out/build/component_id_mapping.csv (4 rows)
Wrote /.../out/build/sprint_id_mapping.csv (23 rows)
BUILD complete. Mappings in /.../out/build
```

**The three mapping CSVs are inputs** to:
- The bulk-move wizard (Fix Version / Component / Sprint field mapping screens).
- Your post-move CSV imports for any data you're re-stitching by old field IDs.

**Sprint closed-date caveat:** the Agile API sets `completeDate` to the moment you close a sprint via script — there's no way to backdate it. Closed sprints recreated by this script will show the recreation time. The script flags every closed sprint in `sprint_id_mapping.csv` with a `note` column saying so.

**Re-running** is safe: existing versions/components/sprints (by name) are detected and skipped.

#### 5.3.3 (Optional) Have the script create the target project

If for some reason you can't create the target in the UI, the script can do it — but you'll need to re-assign schemes manually afterward anyway. Pass:

```bash
python tmp_to_cmp.py build \
    --target-key NEW \
    --create-target \
    --target-lead 5b10a2844c20165700ede21g \
    --target-template com.pyxis.greenhopper.jira:gh-simplified-scrum-classic \
    --target-board-id 99
```

`--target-lead` takes the lead user's accountId (see §2.4).

---

### 5.4. Run `validate` after bulk-move

After you've performed the bulk-move in the UI during the cutover window and renamed the project keys (source → `TMP_OLD`, target → `TMP`), run:

```bash
python tmp_to_cmp.py validate --target-key TMP
```

**What it does:**

1. Loads the baseline from `./out/extract/11_inventory_baseline.json`.
2. Scans the target project with the same logic used in extract.
3. Computes per-category diffs and writes:
   - `./out/validate/target_inventory.json`
   - `./out/validate/validation_delta.json` — full diff per type / status / resolution / bucket.
   - `./out/validate/validation_summary.csv` — flat CSV for quick eyeball.

**Expected output:**

```
Scanning target project for current counts...
Wrote /.../out/validate/target_inventory.json
Wrote /.../out/validate/validation_delta.json
Validation: all top-level counts match.
VALIDATE complete.
```

If diffs are non-zero:

```
Validation DIFFS found: total, withSprint
```

Open `validation_delta.json` to see which buckets are off and by how much. Investigate before declaring success.

---

## 6. Command reference

```
python tmp_to_cmp.py [--base-url URL] [--email EMAIL] [--output-dir DIR] [--verbose] <subcommand> [opts]

Global options:
  --base-url    Jira site URL (or set JIRA_BASE_URL).
  --email       Account email (or set JIRA_EMAIL).
  --output-dir  Artifacts directory. Default: ./out
  --verbose     Debug logging.

Subcommands:

  doctor [--source-key X] [--target-key Y] [--key-field customfield_25199]
      Auth + project access check. No writes.

  extract --source-key X
      Extract source project to ./out/extract/.

  stamp-keys --source-key X [--key-field customfield_25199] [--dry-run]
      Write each source issue's key into the text custom field.
      Idempotent via checkpoint file ./out/stamp/stamped.txt.

  build --target-key Y [--target-board-id N]
        [--create-target --target-lead ACCOUNT_ID]
        [--target-template TEMPLATE_KEY] [--dry-run]
      Recreate versions, components, sprints in target.

  validate --target-key Y
      Compare baseline (from extract) against current target.
```

---

## 7. Output artifacts

After running all phases, `./out/` contains:

```
out/
├── migration.log                          # Append-only log of every run
├── extract/
│   ├── 01_project_meta.json
│   ├── 02_issue_types.{json,csv}
│   ├── 03_statuses.{json,csv}
│   ├── 04_versions.{json,csv}
│   ├── 05_components.{json,csv}
│   ├── 06_source_board.json
│   ├── 07_sprints.{json,csv}
│   ├── 08_source_board_config.json
│   ├── 09_roles.json
│   ├── 10_custom_fields_in_use.{json,csv}
│   └── 11_inventory_baseline.json
├── stamp/
│   ├── stamped.txt                        # Checkpoint: one issue key per line
│   └── stamp_failures.csv                 # If any
├── build/
│   ├── version_id_mapping.csv
│   ├── component_id_mapping.csv
│   └── sprint_id_mapping.csv
└── validate/
    ├── target_inventory.json
    ├── validation_delta.json
    └── validation_summary.csv
```

---

## 8. Troubleshooting

### `AUTHENTICATION FAILED (401)`

The 401 means the token+email pair was rejected. Things to check, in order:

1. **Token validity.** Tokens expire. Generate a fresh one at https://id.atlassian.com/manage-profile/security/api-tokens.
2. **Email matches token owner.** The `JIRA_EMAIL` env var must be the exact email of the account that created the token.
3. **Base URL is correct.** Tokens are scoped to an organization, not a site. But the email account needs access to the specific `JIRA_BASE_URL` site. If you're using a sandbox, ensure your account has been provisioned there.
4. **Manual sanity check** — the script's error message gives you a `curl` command. Run it. If that 401s, the script will 401.

### `SOURCE PROJECT 'XYZ' NOT ACCESSIBLE` (e.g. the MK5 case)

The script disambiguates this for you:

- **"Project key case mismatch"** → typo. Use the exact key the API reports.
- **"Project EXISTS but the authenticated user lacks BROWSE permission"** → the token owner can't see the project. Either add them to a project role, or use a different token.
- **"No project with key 'X' is visible. Sample of accessible keys: A, B, C"** → either the key is wrong, or you're authenticated to the wrong site. Browse to the project in the UI manually and check the key.

### `Test stamp did not persist on TMP-1`

`customfield_25199` is not on the edit screen for that issue type in the source TMP. Go to the source project's settings → Issue types, drag the field onto the layout for every issue type, save, re-run.

### `Field customfield_25199 has schema.type='option'. Must be a text (string) field.`

You picked a non-text field by mistake. The stamping target needs to be a single-line text field. Create one if needed, then re-run with `--key-field <correct id>`.

### `429 Too Many Requests`

The script already retries with exponential backoff. If sustained, increase `SLEEP_BETWEEN_WRITES` at the top of the script.

### `POST /rest/api/3/search/jql -> 400: Invalid request payload`

The new search endpoint validates the body strictly. Common payload mistakes:

- `expand` sent as a JSON array. Must be a **comma-separated string**: `"names,schema"`, not `["names","schema"]`. The script handles this for you and will coerce lists to strings, but if you're hitting this from a custom call, check that.
- `startAt` sent. The new endpoint rejects it — pagination is `nextPageToken`-based. The script never sends `startAt`.
- `nextPageToken: null` on the first request. Atlassian's docs say the first page has token `null`, but actually passing `null` errors with "The provided next page token is invalid or expired." The script omits the field entirely on the first request.

Since v1.1 the error log includes the full request body so you can see exactly what was sent:

```
ERROR POST .../search/jql -> 400 body={"jql":"project = \"MK5\"","fields":["*all"],"maxResults":100,"fieldsByKeys":false,"expand":"names"}: {"errorMessages":["Invalid request payload..."]}
```

If your payload looks right and the call still 400s, the JQL itself may be unbounded or reference an inaccessible field — try the same JQL in the UI's issue navigator first.

### A redirected URL appears in error logs

E.g. log shows `/rest/api/2/project/X` even though the script uses `/rest/api/3/`. Atlassian transparently redirects v3 to v2 for some endpoints. The script logs the original v3 URL it requested plus the redirect target so you can see what's happening. The behavior is correct — the 404 (or whatever code) applies to the redirected URL just as it would to the original.

### `Baseline inventory not found. Run 'extract' first.`

`validate` reads `./out/extract/11_inventory_baseline.json`. If you ran `extract` with a different `--output-dir`, point `validate` at the same one (it's a global option, comes before the subcommand).

---

## 9. Known limitations

1. **Sprint `completeDate` cannot be backdated.** The Agile API stamps it at the moment of close. Closed sprints created by `build` will show the run time, not the original close time. Velocity charts in the target will reflect this. The mapping CSV flags affected sprints.

2. **TMP-scoped custom field IDs are not portable.** The `extract` step lists every custom field with data so you know what to map. The post-move CSV-import step (your plan) handles the actual data re-stitching using `customfield_25199` as the join key.

3. **Board configuration is exported to JSON for reference only.** Columns, swimlanes, quick filters, card layout — all manual rebuild. This is usually a good opportunity to clean these up rather than copy them blindly.

4. **`completeDate` on issues** (sprint close-out timestamps, etc.) is not preserved when bulk-moving across projects. This is a Jira limitation, not the script's.

5. **Custom fields with data but no representation on source screens** won't appear in `extract` step 10. The `*all` field expansion only returns what Jira knows about the issue context, which for TMP means screen-mapped fields.

6. **`stamp-keys` notification suppression** is limited to the field update itself. Source automation rules that listen on "Issue Updated" can still fire. **Disable source TMP automation before running stamp-keys.**