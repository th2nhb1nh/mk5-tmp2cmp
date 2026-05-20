# Jira TMP → CMP Migration Toolkit

Phase 3 (`build target shell + schemes`) of the TMP-to-CMP migration playbook.
This toolkit extracts everything needed from a source team-managed project,
stamps each source issue's key into a designated custom field for post-move
reconciliation, and recreates versions / components / sprints in the
pre-built target company-managed project.

## What it does NOT do

- It does **not** create workflow / screen / field-config / permission /
  notification schemes. Build those in the UI (you want the human review there).
- It does **not** move issues. That's the bulk-move step in the cutover plan.
- It does **not** create global custom fields. You said you'll handle the
  field/value mapping via CSV import after bulk-move. The script only
  preserves the original issue key in `customfield_25199` so you can join
  rows by old key during that CSV import.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

Tested on Python 3.10+.

## Configure

Copy `.env.example` to `.env` and populate:

```bash
export JIRA_BASE_URL="https://yoursite.atlassian.net"
export JIRA_EMAIL="your-email@example.com"
export JIRA_API_TOKEN="<generated at id.atlassian.com>"
```

Then `source .env` before running.

## Workflow

### 0. Pre-req on source TMP

In the source TMP UI, add `customfield_25199` (or whatever single-line text
field you've designated) to **every** issue type's create/edit/view screens.
Without this, the `stamp-keys` PUT will return 200 but silently drop the
value. The script does a test-stamp-and-readback on the first issue to
catch this; it will abort with a clear error if the field isn't wired up.

### 1. Extract source

```bash
python tmp_to_cmp.py extract --source-key TMPKEY --output-dir ./out
```

Produces under `./out/extract/`:

| File | Contents |
|---|---|
| `01_project_meta.json` | Project metadata |
| `02_issue_types.{json,csv}` | Distinct issue types observed |
| `03_statuses.{json,csv}` | Distinct (issueType, status) pairs |
| `04_versions.{json,csv}` | All versions with dates + released/archived |
| `05_components.{json,csv}` | All components with lead + assignee type |
| `06_source_board.json` | Source board metadata |
| `07_sprints.{json,csv}` | All sprints, sorted chronologically |
| `08_source_board_config.json` | Board columns, filter, estimation |
| `09_roles.json` | Project roles and members |
| `10_custom_fields_in_use.{json,csv}` | Custom fields with values (500-issue sample) |
| `11_inventory_baseline.json` | Counts for post-move reconciliation |

### 2. Stamp issue keys (run on Prod, source project)

Dry-run first:

```bash
python tmp_to_cmp.py stamp-keys --source-key TMPKEY \
    --key-field customfield_25199 --dry-run
```

Then for real:

```bash
python tmp_to_cmp.py stamp-keys --source-key TMPKEY \
    --key-field customfield_25199
```

- Calls `PUT /rest/api/3/issue/{key}?notifyUsers=false` for every issue.
- `notifyUsers=false` suppresses notification mail for the update itself.
- Checkpoints every successful stamp to `./out/stamp/stamped.txt`.
  Safe to Ctrl-C and resume.
- Failures logged to `./out/stamp/stamp_failures.csv`.

This must run **before** bulk-move, so each moved issue carries its original
TMP key as a data value that survives the key change.

### 3. Build target CMP shell

Recommended order:

1. **Create the target CMP via UI** (gives you scheme assignment dialogs).
2. Assign the "Silent Migration" notification scheme + your standard
   workflow / screen / field-config / permission schemes.
3. Add `customfield_25199` and any newly created global fields to the
   appropriate Field Configs and Screens of the target.
4. If target is a Scrum CMP, create the board so it has a board ID.
5. Then run:

```bash
python tmp_to_cmp.py build --target-key NEWKEY --target-board-id 1234
```

Dry-run available with `--dry-run`. Outputs `./out/build/`:

- `version_id_mapping.csv` — source version ID → target version ID
- `component_id_mapping.csv` — source component ID → target component ID
- `sprint_id_mapping.csv` — source sprint ID → target sprint ID (+ note on closed-date limitation)

These three mapping files are **inputs to the bulk-move field-mapping wizard**
and to your post-move CSV imports.

#### Optional: have the script create the project

If you accept the limited scheme control:

```bash
python tmp_to_cmp.py build --target-key NEWKEY \
    --create-target \
    --target-lead 5b10a2844c20165700ede21g \
    --target-template com.pyxis.greenhopper.jira:gh-simplified-scrum-classic \
    --target-board-id 1234
```

You'll still need to swap schemes after creation; default schemes are assigned.

### 4. Validate post-move

After bulk-move completes in the cutover window:

```bash
python tmp_to_cmp.py validate --source-key TMPKEY --target-key NEWKEY
```

Reads the baseline from step 1, queries the target, writes:

- `./out/validate/target_inventory.json`
- `./out/validate/validation_delta.json`
- `./out/validate/validation_summary.csv`

Any non-zero `diff` is an exception to investigate before declaring success.

## Known limitations

1. **Sprint `completeDate` cannot be backdated.** The Agile API sets it to
   the moment you close the sprint. Closed sprints recreated by this script
   will show the recreation time, not the original close time. Velocity
   reports will reflect this.

2. **TMP-scoped custom field IDs** in source are not directly portable —
   that's exactly why you're doing the post-move CSV import strategy. The
   script preserves the source key in `customfield_25199` to enable that
   join.

3. **Board configuration** (columns, swimlanes, quick filters) is extracted
   to JSON for reference but not recreated — board setup is UI work, and
   you typically want to take the chance to clean it up.

4. **`completeDate` and sprint-membership history on issues** are lost on
   bulk-move. The bulk-move field-mapping wizard maps the active Sprint
   field value, not historical sprint membership.

5. **Custom fields that aren't on the source TMP screens** will not appear
   in extract step 10 even if they have data. This is a Jira API limitation;
   the field-meta endpoint doesn't expose data-only fields.

## Re-running

Every phase is idempotent:

- `extract` overwrites artifacts (safe).
- `stamp-keys` skips issues already in the checkpoint file.
- `build` checks for existing versions/components/sprints by name and skips.
- `validate` always re-queries the target.

## Troubleshooting

- **401 / 403**: regenerate API token; confirm the user is an org/site admin.
- **404 on target project during build**: target doesn't exist yet; either
  create it in UI or pass `--create-target`.
- **Stamp test fails with "did not persist"**: `customfield_25199` is not on
  the edit screen of one or more issue types in source TMP. Fix that in
  UI, then re-run.
- **429 rate limits**: the client already retries with exponential backoff.
  If you see sustained 429s, lower `SLEEP_BETWEEN_WRITES` is the wrong
  direction — raise it (it's currently 0.1s between writes).
