# Jira TMP → CMP Migration Toolkit

A Python command-line tool for moving a Jira project from **team-managed** to
**company-managed**, with all the before-and-after bookkeeping that Jira's native
bulk-move leaves on the floor.

---

I've run a fair number of these migrations, and here's the thing nobody tells you
up front: the issue move itself is the easy part. Jira does that for you. What
eats your week is everything *around* it — preserving original keys, rebuilding
sprints so velocity charts don't fall apart, copying over the custom fields that
quietly vanish, and then proving to a skeptical project lead that nothing got
lost. That's what this toolkit is for. It handles the boring, error-prone, do-it-
exactly-right parts so you can focus on the judgment calls.

This README is written as a tutorial. If you read it top to bottom and run the
commands in order, you'll get through a clean migration. I've tried to flag the
spots where I've personally been burned, because those are the ones that'll cost
you a re-run (or a very awkward Slack message) if you skip them.

A quick word on who this is for: you don't have to be a developer, but you do need
a Jira admin account and a willingness to read the output. The tool is chatty on
purpose — when something's wrong, it tells you what and usually why.

---

## Table of contents

1. [What we're actually doing here](#1-what-were-actually-doing-here)
2. [Glossary, so we're speaking the same language](#2-glossary-so-were-speaking-the-same-language)
3. [The full migration arc](#3-the-full-migration-arc)
4. [Prerequisites — get these squared away first](#4-prerequisites--get-these-squared-away-first)
5. [Installing the tool](#5-installing-the-tool)
6. [Credentials and the `.env` file](#6-credentials-and-the-env-file)
7. [The walkthrough](#7-the-walkthrough)
   - [7.0 `doctor` — kick the tires](#70-doctor--kick-the-tires)
   - [7.1 `extract` — snapshot the source](#71-extract--snapshot-the-source)
   - [7.2 `stamp-keys` — give every issue a name tag](#72-stamp-keys--give-every-issue-a-name-tag)
   - [7.3 `build` — stand up the target's structure](#73-build--stand-up-the-targets-structure)
   - [7.4 The bulk-move (you, in the Jira UI)](#74-the-bulk-move-you-in-the-jira-ui)
   - [7.5 `update-sprints` — put issues back on their sprints](#75-update-sprints--put-issues-back-on-their-sprints)
   - [7.5b `export-sprint-csv` — the move that always works](#75b-export-sprint-csv--the-move-that-always-works)
   - [7.6 `copy-fields` — bring the field values across](#76-copy-fields--bring-the-field-values-across)
   - [7.7 `validate` — prove nothing was lost](#77-validate--prove-nothing-was-lost)
   - [7.8 `report` — the deliverable for the team](#78-report--the-deliverable-for-the-team)
8. [Command reference](#8-command-reference)
9. [Everything the tool writes to `out/`](#9-everything-the-tool-writes-to-out)
10. [Troubleshooting — the stuff that actually goes wrong](#10-troubleshooting--the-stuff-that-actually-goes-wrong)
11. [Known limitations — read before you promise anything](#11-known-limitations--read-before-you-promise-anything)

---

## 1. What we're actually doing here

Quick refresher on the two project types, because the whole migration hinges on
the difference. **Team-managed projects (TMP)** are the ones a team can spin up
and configure themselves — everything lives inside the project, which is great for
autonomy and terrible for standardization. **Company-managed projects (CMP)** pull
their workflows, fields, and schemes from shared, admin-controlled configuration.
When an org matures, or wants reporting and governance across teams, TMP → CMP is
the move.

Here's the honest division of labor:

- **Jira's bulk-move** carries the issues themselves — history, comments,
  attachments, worklogs. It's solid. You run it in the browser.
- **This toolkit** handles everything bulk-move drops or scrambles: original issue
  keys, sprint membership (especially closed sprints), TMP-scoped custom field
  values, and the validation + reporting you'll want when it's done.

Think of it like a house move. You photograph every room before you pack
(`extract`), label every box so you know where it came from (`stamp-keys`), build
matching shelves at the new place (`build`), call the movers (bulk-move), put the
boxes back where they belong (`update-sprints`, `copy-fields`), walk through to
make sure nothing's missing (`validate`), and hand everyone a summary of how it
went (`report`).

**What this tool deliberately won't do:** create workflows, screens, permission
schemes, or global custom fields. Those are design decisions, and they're far
safer to make by hand in the UI where Jira gives you the proper dialogs. The tool
sticks to the mechanical, repeatable stuff — versions, components, sprints, field
values, and reconciliation.

Two design choices that'll save your bacon:

- **Dry-run everything.** Every command that writes takes `--dry-run`. It shows
  you exactly what it *would* do without touching Jira. I dry-run every single
  time, even when I'm sure. Costs nothing, catches plenty.
- **It picks up where it left off.** These runs can take a while on a big project,
  and networks hiccup. If a command dies halfway, just run it again — it keeps a
  checkpoint and skips what it already finished. Re-running a completed step is a
  no-op.

---

## 2. Glossary, so we're speaking the same language

| Term | What it means in practice |
|---|---|
| **TMP** | *Team-managed project* — your **source**, the project you're migrating away from. |
| **CMP** | *Company-managed project* — your **target**, the project you're migrating into. |
| **Issue** | Any work item — story, bug, task, sub-task. |
| **Issue key** | The issue's name, like `TMP-123`. Bulk-move changes these, which is the root of half our work. |
| **Project key** | The prefix on every issue key (`TMP`, `ABC`…). Case-sensitive in the API — bite me once, shame on Jira. |
| **Bulk-move** | Jira's native cross-project move. Browser only. This toolkit doesn't do it; it sets it up and cleans up after. |
| **Sprint** | A time-boxed batch of work on a board. State is future, active, or closed — and closed sprints are where the pain lives. |
| **Version / Component** | Structural labels on a project ("Release 2.1", "Backend"). |
| **Custom field** | Any field beyond Jira's built-ins, identified like `customfield_25199`. |
| **Board** | The Scrum/Kanban board a project's sprints hang off of, identified by a numeric **board ID**. |
| **JQL** | *Jira Query Language* — the search syntax (`project = "TMP" AND ...`). |
| **API token** | A password-equivalent secret the tool uses to act as your account. |
| **Idempotent** | Engineer-speak for "safe to run twice." Re-running skips finished work. |

> **The one gotcha to internalize now: the key field has two different names.**
> To keep each issue's original key through the move, we stash it in a text custom
> field. That field gets referenced two ways — by its **id**
> (`customfield_25199`, used when reading or writing the value) and by its **JQL
> name** (`Original Work Item Key[Short text]`, used when *searching* for it). They
> are not interchangeable, and mixing them up is the single most common reason
> these scripts throw a 400. You'll set both in `.env`, and `doctor` prints them
> side by side so you can eyeball it. More in [§7.0](#70-doctor--kick-the-tires)
> and the [troubleshooting section](#field-customfield_25199-does-not-exist-jql-400).

---

## 3. The full migration arc

Everything runs in this order. The one manual step — the bulk-move — sits right in
the middle. The toolkit gets you ready for it, then stitches things back together
on the far side.

```
   SOURCE (old TMP)                                  TARGET (new CMP)
   ───────────────                                   ────────────────

   ┌─────────────┐
   │   doctor    │  ← always first: "can I auth and see both projects?"
   └─────────────┘

   ┌─────────────┐
   │  extract    │  ── snapshot the source ──►   (baseline saved to ./out)
   └─────────────┘
   ┌─────────────┐
   │ stamp-keys  │  ── tag each issue with its
   └─────────────┘     original key so it survives the move

                                                    ┌─────────────┐
                          rebuild structure ─────►  │   build     │
                                                    └─────────────┘
                                                    (versions, components,
                                                     sprints + mapping CSVs)

        ════════════ BULK-MOVE (you, in the Jira browser UI) ════════════
        Move all issues source → target using the mapping CSVs from build.
        Then optionally rename projects so the new one inherits the old key.

                                                    ┌──────────────────┐
                              reconnect sprints ──► │ update-sprints   │
                                                    │  or              │
                                                    │ export-sprint-csv│
                                                    └──────────────────┘
                                                    ┌─────────────┐
                              copy field values ──► │ copy-fields │
                                                    └─────────────┘
                                                    ┌─────────────┐
                              check + report ─────► │  validate   │
                                                    │  report     │
                                                    └─────────────┘
```

The one-liner on each:

| When | Command | What it does |
|---|---|---|
| Preflight | `doctor` | Confirms auth, project access, and that your settings resolve. Read-only — never writes a thing. |
| Pre-move | `extract` | Snapshots the source: issue counts, versions, components, sprints, and a full field catalog. Your baseline. |
| Pre-move | `stamp-keys` | Writes each issue's current key into a text field so it survives bulk-move's re-keying. |
| Pre-move | `build` | Recreates versions, components, and sprints in the target, and writes the mapping CSVs the bulk-move wizard needs. |
| **Manual** | *bulk-move* | Jira moves the issues. Not part of this tool. |
| Post-move | `update-sprints` | Re-points moved issues at the matching target sprints. |
| Post-move | `export-sprint-csv` | The reliable fallback for closed sprints — emits a CSV you import via External System Import. |
| Post-move | `copy-fields` | Copies dropped custom-field values, matching old issue to new via the stamped key. |
| Post-move | `validate` | Compares the target against the `extract` baseline and flags anything off. |
| Post-move | `report` | Generates a multi-sheet Excel workbook for the team. |

> **Why the post-move steps only count "migrated issues."** If your target is
> brand-new, ignore this. But a lot of real migrations consolidate *into* a
> project that already has issues. In that case, the tool has to compare and
> update only the issues that came from the source — not the ones already living
> there. It tells them apart by the stamped key. That's the real reason
> `stamp-keys` matters even if you don't personally care about old key numbers.

---

## 4. Prerequisites — get these squared away first

### 4.1 Python 3.10 or newer

`python3 --version`. If it's older, grab a current build from
[python.org](https://www.python.org/) or your package manager. Don't fight an
ancient Python; just install a fresh one.

### 4.2 An Atlassian API token

1. Sign in at <https://id.atlassian.com/manage-profile/security/api-tokens> as
   **the account that's going to run the migration**.
2. **Create API token**, name it something honest like `tmp-to-cmp-migration`, and
   copy it somewhere safe.
3. Treat it like a password. It expires on whatever date you picked at creation —
   set a reminder if your migration window is far out.

The tool acts as whoever owns the token, so that account needs:

| Where | Permissions | For |
|---|---|---|
| Source TMP | Browse Projects + Edit Issues | `extract`, `stamp-keys` |
| Target CMP | Browse Projects + Administer Project | versions/components, field edits |
| Target board | Manage Sprints | sprint creation and the reopen/close cycle |

A site or org admin has all of this by default. Running with a scoped-down
service account? Verify each permission before you start — finding out mid-run
that you can't manage sprints is a bad time.

### 4.3 One-time source setup (in the UI)

Before `stamp-keys` will work, the text field you're using to preserve keys
(default `customfield_25199`) has to be on the **edit screen of every issue type**
in the source TMP. This trips people up because Jira will happily return `200 OK`
on an update to a field that isn't on the screen — and silently drop the value.
Looks like it worked. Didn't.

In a team-managed project:

1. Source project → **Project settings** → **Issue types**.
2. For each issue type, drag the field onto the layout.
3. Save.

You don't have to take this on faith. `stamp-keys` writes one test issue and reads
it back before doing anything else, and it stops cold with a clear error if the
value didn't stick. That readback has saved me more than once.

### 4.4 The IDs you'll need to dig up

| What | Where it lives |
|---|---|
| **Project key** | Prefix of any issue key (`TMP-123` → `TMP`). Also **Project settings → Details**. Case-sensitive. |
| **Board ID** | Open the board; the URL ends in `/boards/<ID>` or `?rapidView=<ID>`. `doctor` prints these for both projects too. |
| **Account ID** (for `--target-lead`) | A user's profile URL, or `GET /rest/api/3/user/search?query=<email>`. |
| **Custom field ID** | Admin → **Issues** → **Custom fields**, or `GET /rest/api/3/field`. Always starts with `customfield_`. The `extract` catalog lists them too. |

---

## 5. Installing the tool

```bash
cd mk5-tmp2cmp
python3 -m venv .venv          # isolated environment so you don't pollute system Python
source .venv/bin/activate
pip install -r requirements.txt
```

Three dependencies, nothing exotic:

- **`requests`** — the HTTP client that talks to Jira.
- **`python-dotenv`** — auto-loads your `.env` so you're not exporting variables by hand.
- **`openpyxl`** — only the `report` command needs it, for the Excel output.

---

## 6. Credentials and the `.env` file

Configuration comes from environment variables, and the easiest path is a `.env`
file in the project folder. The tool **loads it automatically on every run** — no
`source` step, no exporting. Set it once.

```bash
cp .env.example .env
# open .env and fill it in
```

The whole idea: **populate `.env` once and your commands get short.** With it
filled in, you're just running `python3 tmp_to_cmp.py extract`, `… build`,
`… validate`, and so on — no repeating `--source-key` / `--target-key` every time.
Every value except the three auth secrets can still be overridden with a flag for
a one-off.

| Variable | Required? | Used by | What it is |
|---|---|---|---|
| `JIRA_BASE_URL` | Always | all | Site URL, no trailing slash: `https://your-site.atlassian.net` |
| `JIRA_EMAIL` | Always | all | Email of the account that owns the token |
| `JIRA_API_TOKEN` | Always | all | The token from §4.2. There's no `--token` flag — it lives here only |
| `JIRA_SOURCE_KEY` | Per command | extract, stamp-keys, copy-fields, report | Source (old) project key |
| `JIRA_TARGET_KEY` | Per command | build, update-sprints, export-sprint-csv, copy-fields, validate, report | Target (new) project key |
| `JIRA_KEY_FIELD` | Optional | stamp-keys, copy-fields, validate, report | Text field **id** for the preserved key. Default `customfield_25199` |
| `JIRA_KEY_FIELD_NAME` | Optional | doctor, copy-fields, validate, report, export-sprint-csv | JQL **name** of that same field, e.g. `Original Work Item Key[Short text]` (see [the gotcha](#2-glossary-so-were-speaking-the-same-language)) |
| `JIRA_OUTPUT_DIR` | Optional | all | Where artifacts land. Default `./out` |
| `JIRA_TARGET_BOARD_ID` | Optional | build | Target Scrum board id; required to create sprints |
| `JIRA_TARGET_LEAD` | Optional | build | Lead account id; only needed with `--create-target` |
| `JIRA_TARGET_TEMPLATE` | Optional | build | Project template for `--create-target`. Defaults to Scrum (classic CMP) |

The tool refuses to start if `JIRA_BASE_URL`, `JIRA_EMAIL`, or `JIRA_API_TOKEN` is
missing — better a clear failure than a confusing one.

> **If you rename projects during cutover** (covered in
> [§7.4](#74-the-bulk-move-you-in-the-jira-ui)), your keys change mid-migration —
> typically source `TMP`→`TMP_OLD` and target `NEW`→`TMP`. The moment you rename,
> update `JIRA_SOURCE_KEY` / `JIRA_TARGET_KEY` in `.env` so the post-move commands
> point at the right places. Keeping stable keys instead? Then `.env` never needs
> a mid-run edit. Either way, decide this *before* you start, not at 11pm during
> the window.

---

## 7. The walkthrough

We'll go phase by phase. Each one tells you **why** it exists, **what to run**, and
**what you get out of it**. The bulk-move ([§7.4](#74-the-bulk-move-you-in-the-jira-ui))
is the manual browser step in the middle — everything before it is prep,
everything after is repair and reporting.

> **House rule:** any command that writes, run with `--dry-run` first, read the
> output, *then* run it for real. Every time.

### 7.0 `doctor` — kick the tires

**Why:** Before you sink an hour into a real run, confirm the tool can authenticate
and actually see both projects. `doctor` is read-only; it can't hurt anything.

```bash
python3 tmp_to_cmp.py doctor
# Pulls JIRA_SOURCE_KEY / JIRA_TARGET_KEY / JIRA_KEY_FIELD[_NAME] from .env.
# One-off override:
#   python3 tmp_to_cmp.py doctor --source-key TMP --target-key NEW
```

Only have the source project so far? Leave `JIRA_TARGET_KEY` blank and it'll skip
the target checks.

It authenticates, verifies each project, lists the boards on both (so you can grab
that **target board ID** for `build`), confirms the key field is actually a text
field, resolves the Sprint custom field id, and prints the key field's **id and
resolved JQL name** together. That last bit is your chance to catch a `.env`
mistake before it costs you. Anything wrong gets logged with the likely cause —
see [§10](#10-troubleshooting--the-stuff-that-actually-goes-wrong).

### 7.1 `extract` — snapshot the source

**Why:** This is your "before" photo. You'll use it to rebuild the target's
structure, to author your field mappings, and — crucially — to prove at the end
that the counts line up. Don't skip it or shortcut it; everything downstream leans
on this baseline.

```bash
python3 tmp_to_cmp.py extract
# Source from JIRA_SOURCE_KEY (override: --source-key TMP).
# Heads up: --output-dir is a GLOBAL option, so it goes BEFORE the subcommand:
#   python3 tmp_to_cmp.py --output-dir ./out extract
```

What it pulls, in order:

1. Project metadata — name, lead, style.
2. A single pass over every issue: distinct issue types, distinct (type, status)
   pairs, and counts by type, status, and resolution.
3. All versions, components, the board, every sprint (closed + active + future),
   the board config, and project roles.
4. Five "how many issues have X" counts — attachments, comments, fix version,
   components, sprint. These five are your reconciliation baseline.
5. A **full field catalog**: every project-scoped field, including the allowed
   values for select fields, pulled from a sample issue of each type via
   `/editmeta`.
6. Writes all of it to `./out/extract/` as JSON, plus a CSV for the field catalog.

That catalog (`12_source_field_catalog.csv`) is the source of truth you'll use to
write the `field_mapping.csv` that `copy-fields` consumes later. Keep it handy.

Re-running is safe — it overwrites cleanly. Want to see what `validate` will check
against? `cat ./out/extract/11_inventory_baseline.json`.

### 7.2 `stamp-keys` — give every issue a name tag

**Why:** The second bulk-move runs, every issue gets a brand-new key in the target,
and the old one is gone. If you ever want to trace `TMP-123` to its new identity —
for field copies, audit trails, dashboards, anyone asking "where'd my ticket go" —
you need that old key stored *on the issue* beforehand. So we write each issue's
current key into a text field while it still has it.

**Prerequisite:** the key field has to be on every source issue type's edit screen
(see [§4.3](#43-one-time-source-setup-in-the-ui)). This is the step that catches
people.

```bash
python3 tmp_to_cmp.py stamp-keys --dry-run   # look first
python3 tmp_to_cmp.py stamp-keys             # then commit
# Source key + field from .env (JIRA_SOURCE_KEY, JIRA_KEY_FIELD).
```

What it does:

1. Confirms the field exists and is text-typed (`string`/`any`).
2. Test-stamps the oldest issue and reads it back — **bails with a clear error if
   the value didn't persist.** This is the readback I mentioned; trust it.
3. Asks you to confirm before it touches the rest.
4. Walks every issue, writing its key, pausing 100 ms between writes to be polite
   to the API.
5. Logs each stamped key to `./out/stamp/stamped.txt` as it goes.

**Got interrupted? Just re-run it.** It reads the checkpoint and skips what's done.
Failures land in `./out/stamp/failures.csv` with the reason — investigate, fix,
re-run, and only the un-checkpointed keys retry.

> **One I learned the hard way:** `notifyUsers=false` mutes the field-update email,
> but it does *not* stop source **automation rules** listening on "Issue Updated."
> Stamp a few thousand issues with an active automation rule and you've just fired
> a few thousand webhooks (or worse, reassigned everything). **Disable source
> automation before you run this.**

### 7.3 `build` — stand up the target's structure

This phase is **manual UI work first, then the script.** Don't skip the manual
part — it's where the real design decisions live.

#### 7.3.1 Create the target project in the UI

Yes, the script *can* create it (see 7.3.3), but I almost always do this by hand.
Creating it in the browser gives you the scheme-assignment dialogs, and you nearly
always want to attach existing standard schemes rather than whatever the API picks.

1. **Create project** → Company-managed → Software (Scrum or Kanban, your call).
2. **Project key:** use a temporary one like `NEW`. You'll rename it to the real
   `TMP` key after the move, once the source is renamed to `TMP_OLD`.
3. **Project settings → Schemes:** assign workflow, screen, field configuration,
   permission, and notification schemes. Pro move for the cutover window: a
   dedicated, silent "Migration" notification scheme so you don't spam everyone.
4. Add `customfield_25199` and any other custom fields you plan to map, onto the
   right field configurations and screens.
5. Create a Scrum **board** and note its board ID ([§4.4](#44-the-ids-youll-need-to-dig-up)).

#### 7.3.2 Run the build

Set `JIRA_TARGET_KEY` and `JIRA_TARGET_BOARD_ID` in `.env`, then:

```bash
python3 tmp_to_cmp.py build --dry-run        # preview
python3 tmp_to_cmp.py build                  # for real
```

What it does, in order:

1. Confirms the target exists and is reachable.
2. Reads your `extract` artifacts.
3. **Versions** — reuses any with a matching name, otherwise creates them (dates
   included), then patches released/archived state. Writes `version_id_mapping.csv`.
4. **Components** — same reuse-or-create-by-name pattern. Writes
   `component_id_mapping.csv`.
5. **Sprints** — needs the board id. Recreates each source sprint with matching
   name, dates, and goal, then transitions it to the same state. Writes
   `sprint_id_mapping.csv`.

**Those three mapping CSVs aren't just logs — they're inputs.** The bulk-move
wizard's Fix Version / Component / Sprint mapping screens want them, and so do
`update-sprints` and `report` later. Don't lose them.

> **The closed-sprint date thing, up front:** the Agile API stamps a sprint's
> `completeDate` to the moment *you* close it. There's no backdating. So any closed
> sprint this step recreates shows the recreation time, not the original
> completion. The reports flag affected sprints, but if anyone leans on historical
> Velocity, warn them now. This is a Jira limitation, not a bug in the tool.

Re-running is safe — existing versions/components/sprints (matched by name) get
detected and skipped.

#### 7.3.3 (Optional) Let the script create the target

Can't create the project in the UI for some reason? The script will, though you'll
still re-assign schemes by hand afterward:

```bash
python3 tmp_to_cmp.py build \
    --create-target \
    --target-lead 5b10a2844c20165700ede21g
# --target-key, --target-board-id, --target-template default from .env.
```

`--target-lead` wants the lead's account id ([§4.4](#44-the-ids-youll-need-to-dig-up)),
or set `JIRA_TARGET_LEAD`. `--target-template` defaults to Scrum classic CMP.

### 7.4 The bulk-move (you, in the Jira UI)

**Not a script step — this is all you, in the browser, during the cutover window.**

1. In the source TMP, select all issues and run Jira's native **bulk-move** into
   the target CMP.
2. On the wizard's mapping screens, feed it the `version_id_mapping.csv`,
   `component_id_mapping.csv`, and `sprint_id_mapping.csv` from `build`. This is
   what keeps fix versions, components, and sprints pointing at the right targets.
3. Optionally rename: source → `TMP_OLD`, target → `TMP`, so the new project
   inherits the original key and nobody's bookmarks break.

> **If you renamed, update `.env` right now** — set `JIRA_SOURCE_KEY=TMP_OLD` and
> `JIRA_TARGET_KEY=TMP` before you run anything below, or pass `--source-key` /
> `--target-key` per command. Skip this if you kept stable keys.

Bulk-move brings history, comments, attachments, and worklogs across cleanly. What
it does *not* preserve in any usable form: TMP-scoped custom field values and the
original sprint membership. Repairing those two is exactly what `update-sprints`
and `copy-fields` are for.

### 7.5 `update-sprints` — put issues back on their sprints

**Why:** After the move, target issues often still reference the *source* sprint
ids, or lose sprint membership entirely. This rewrites each issue's Sprint field
from the old sprint id to the matching target one, using `build/sprint_id_mapping.csv`.

```bash
python3 tmp_to_cmp.py update-sprints --dry-run
python3 tmp_to_cmp.py update-sprints
# Target key from .env (JIRA_TARGET_KEY).
```

How it works under the hood:

1. **Plan** — one scan of the target (migrated issues only), grouping issues by
   whether their target sprints include any *closed* ones.
2. **Phase A** — issues whose sprints are all open get updated directly. Easy.
3. **Phase B** — for each closed sprint with pending issues: reopen it → update its
   issues → close it again. Only one closed sprint is reopened at a time, because
   Jira won't allow two active sprints on a board unless parallel sprints are on.

The caveats, and I want to be straight with you because this is the fiddliest part
of the whole toolkit:

- Re-closing a sprint stamps a fresh `completeDate` of *now*. The original is gone.
- Issues that belong to **more than one** closed sprint get skipped and listed in
  `multi_closed_issues.csv` for manual handling.
- Issues whose source sprint has no mapping land in `missing_sprint_map.csv`.

Resumable via `./out/update_sprints/updated.txt`. Per-sprint outcomes go to
`sprint_cycle_log.csv`, failures to `failures.csv`.

> **Real talk:** this reopen/re-close dance is the fragile part. It falls over when
> the board forbids multiple active sprints, and it punts on multi-closed issues.
> If closed sprints are giving you grief — and on a mature project they will —
> don't fight it. Skip straight to
> [`export-sprint-csv`](#75b-export-sprint-csv--the-move-that-always-works) below.
> It sets sprint membership by id through Jira's import, with none of these
> restrictions. On bigger migrations I often just go straight to the CSV route.

### 7.5b `export-sprint-csv` — the move that always works

**Why:** `update-sprints` can only attach an issue to a closed sprint by
temporarily reopening it, and that approach breaks on boards that forbid multiple
active sprints or when an issue spans several closed sprints. Jira's built-in
**External System Import** sets the Sprint field directly by **sprint id** — no
reopening, no re-closing, no restrictions. It's the dependable way to restore
historical sprint membership, full stop. This command just builds the CSV that the
import consumes; it **makes zero changes to Jira**. The import itself is a manual
UI step.

It reads the same `build/sprint_id_mapping.csv`, scans the target for every
migrated issue still carrying a sprint, maps source sprint ids to target, and
writes one row per issue.

```bash
python3 tmp_to_cmp.py export-sprint-csv
# Target key from .env (JIRA_TARGET_KEY).
```

**Output:** `./out/export_sprint_csv/sprint_import.csv` — **semicolon-delimited**,
with these columns:

| Column | Notes |
|---|---|
| `Issue Key` | The live target issue key; import matches issues by this. |
| `Assignee` | Assignee email (falls back to account id, then display name; blank if unassigned). |
| `Summary` | For human readability during the import's field-mapping step. |
| `Project name` | Target project name. |
| `Project Key` | Target project key. |
| `Project Type` | Target project type key (e.g. `software`). |
| `Sprint ID` | The **target** sprint id(s). An issue in several sprints gets them all in one cell, **comma-separated**. |

> **Why semicolons and not commas?** Because the `Sprint ID` cell already uses
> commas to separate multiple sprint ids, so a comma can't double as the column
> separator. Anything containing a semicolon gets double-quoted per standard CSV
> rules, so the file round-trips cleanly. When you set up the import, you'll point
> Jira at the semicolon delimiter.

Issues whose source sprint has no target mapping are **left out** of the CSV and
listed in `missing_sprint_map.csv` (re-run `build` if a sprint's missing from the
map).

Importing it:

1. Jira → **Settings** → **System** → **External System Import** → **CSV**.
2. Upload `sprint_import.csv`. On the settings screen, set the **delimiter to
   semicolon (`;`)** — don't skip this or every row mangles.
3. On field mapping: `Issue Key` → **Issue key** (so it updates existing issues
   instead of creating new ones — important!), `Sprint ID` → **Sprint**, and the
   rest to their like-named fields.
4. Run it, then read Jira's import log.

Read-only and safe to re-run; it overwrites the CSV each time.

### 7.6 `copy-fields` — bring the field values across

**Why:** Bulk-move drops TMP-scoped custom field values. This copies them from each
source issue to its target counterpart, matching on the stamped key.

**Prerequisite:** author a mapping CSV at `./out/mapping/field_mapping.csv`:

```
source_field_id,target_field_id,transform
customfield_10050,customfield_22001,copy
customfield_10051,customfield_22002,append_suffix
```

- `transform` is optional per row. Options: `copy` (default), `append_prefix`
  (writes `Original: <value>`), `append_suffix` (writes `<value> (migrated)`).
- Use the `extract` field catalog (`12_source_field_catalog.csv`) to find the
  source field ids — that's what it's there for.

```bash
python3 tmp_to_cmp.py copy-fields --dry-run
python3 tmp_to_cmp.py copy-fields
# Source/target keys from .env (after the §7.4 rename, that's TMP_OLD / TMP).
```

It finds every target issue with a stamped source key, pulls the mapped fields off
the source issue, applies any transform, and writes them to the target. Resumable
via `./out/copy_fields/copied.txt`; failures in `failures.csv`.

### 7.7 `validate` — prove nothing was lost

This is where you earn back the project lead's trust. After the move and the
reconnect steps, reconcile against the baseline:

```bash
python3 tmp_to_cmp.py validate
# Target key + key field (id and JQL name) from .env.
```

What it does:

1. Loads the baseline from `./out/extract/11_inventory_baseline.json`.
2. Re-scans the target with the same logic as `extract` — but **counting migrated
   issues only**, so a reused target's pre-existing issues don't pollute the
   comparison.
3. Computes per-category diffs and writes `target_inventory.json`,
   `validation_delta.json`, and `validation_summary.csv`.
4. If a sprint mapping exists, compares per-sprint counts and writes
   `sprint_mismatches.json` on any mismatch.
5. If a field mapping exists, samples up to 50 target issues and compares mapped
   field values, writing `field_mismatches.json` on any mismatch.

If the aggregate diffs aren't zero, it logs which buckets are off. Don't declare
victory on a glance — open `validation_delta.json` and see *by how much*. Small
expected deltas happen (a status renamed mid-flight, say); surprises don't get to
slide.

### 7.8 `report` — the deliverable for the team

**Why:** A clean, multi-sheet Excel workbook is what you actually hand to
stakeholders. It's the artifact that closes out the migration.

```bash
python3 tmp_to_cmp.py report
# Source/target keys + key field from .env (after §7.4 rename, TMP_OLD / TMP).
```

You need both a source and target key set, or it errors out. It reads from
`extract/`, `build/`, `update_sprints/`, and (if present) `validate/`, re-queries
the target live, and writes `./out/report/migration_report.xlsx` with these sheets:

- **Summary** — headline KPIs with OK/REVIEW/ATTENTION status, sprint cycle
  outcomes, and callouts.
- **Issue Counts** — source vs. target by type / status / resolution, with live
  Excel formulas.
- **Sprints** — per-sprint reconciliation with deltas.
- **Versions** / **Components** — present/missing checks.
- **Content Buckets** — attachment/comment/version/component/sprint coverage %.
- **Issue Key Map** — every migrated issue: old key → clickable new key, plus type,
  status, summary.
- **Exceptions** — unmapped sprints, multi-closed issues, update/copy failures,
  membership mismatches.
- **Methodology** — plain-language notes on the caveats, including that
  `completeDate` behavior, so nobody's surprised later.

Every target-side number counts migrated issues only, same as `validate`. Needs
`openpyxl` (it's in `requirements.txt`).

---

## 8. Command reference

```
python3 tmp_to_cmp.py [--base-url URL] [--email EMAIL] [--output-dir DIR] [--verbose] <subcommand> [opts]

Global options (these go BEFORE the subcommand):
  --base-url    Jira site URL (or JIRA_BASE_URL).
  --email       Account email (or JIRA_EMAIL).
  --output-dir  Artifacts directory. Default ./out (or JIRA_OUTPUT_DIR).
  --verbose     Debug logging.

With a populated .env, the flags below are just optional overrides — most runs
boil down to `tmp_to_cmp.py extract`, `… build`, `… validate`, `… report`.

  doctor [--source-key X] [--target-key Y] [--key-field ID] [--key-field-name NAME]
      Auth + project access check, board listing, sprint-field + key-field name
      resolution. Never writes.

  extract --source-key X
      Snapshot the source project + full field catalog to ./out/extract/.

  stamp-keys --source-key X [--key-field ID] [--dry-run]
      Write each source issue's key into the text custom field. Resumable.

  build --target-key Y [--target-board-id N]
        [--create-target --target-lead ACCOUNT_ID]
        [--target-template TEMPLATE_KEY] [--dry-run]
      Recreate versions, components, sprints in target. Writes mapping CSVs.

  update-sprints --target-key Y [--dry-run]
      Rewrite the Sprint field from source to target sprint IDs.
      Needs build/sprint_id_mapping.csv.

  export-sprint-csv --target-key Y
      Emit a semicolon-delimited CSV for Jira's External System Import — the
      reliable closed-sprint fallback. Read-only. Needs build/sprint_id_mapping.csv.

  copy-fields --source-key X --target-key Y [--key-field ID] [--key-field-name NAME] [--dry-run]
      Copy mapped field values, matching on the stamped key.
      Needs ./out/mapping/field_mapping.csv.

  validate --target-key Y [--key-field ID] [--key-field-name NAME]
      Compare the baseline against the current target (migrated issues only).

  report --source-key X --target-key Y [--key-field ID] [--key-field-name NAME]
      Generate ./out/report/migration_report.xlsx (target figures = migrated only).
```

---

## 9. Everything the tool writes to `out/`

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
│   └── 12_source_field_catalog.csv        # Author field_mapping.csv from this
├── stamp/
│   ├── stamped.txt                        # Checkpoint: one key per line
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
│   ├── sprint_cycle_log.csv               # Reopen/close outcomes per sprint
│   └── failures.csv                       # If any
├── export_sprint_csv/
│   ├── sprint_import.csv                  # Semicolon-delimited; for External Import
│   └── missing_sprint_map.csv             # If any
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

## 10. Troubleshooting — the stuff that actually goes wrong

### `Authentication failed` (401)

1. **Token expired.** They do. Mint a fresh one at the [token page](https://id.atlassian.com/manage-profile/security/api-tokens).
2. **Email doesn't match the token owner.** `JIRA_EMAIL` has to be the exact email
   of the account that created the token.
3. **Wrong site / no access.** The account must have access to that specific
   `JIRA_BASE_URL`. Sandbox? Make sure you're provisioned there.

### Source/target project not accessible

- **Key case mismatch** → use the exact key the API reports. Keys are case-sensitive.
- **No Browse permission** → the token owner can't see the project. Add them to a
  role or use a different token.
- **Wrong site** → open the project in the browser and confirm the key and site
  match `JIRA_BASE_URL`.

### `Test stamp failed: field not editable on issue type.`

The key field isn't on that issue type's edit screen in the source. Add it to
**every** issue type's layout ([§4.3](#43-one-time-source-setup-in-the-ui)), save,
re-run. This is the most common `stamp-keys` snag, every time.

### `Key field customfield_X must be a text field (type string/any).`

You pointed it at a non-text field. The stamp target has to be a single-line text
field — create one if needed and re-run with the right `--key-field`.

### `Could not resolve Sprint custom field id`

Jira Software (Agile) isn't enabled on the instance, or the token can't see agile
fields. Every sprint-related step needs this resolved.

### `429 Too Many Requests`

The client already retries with exponential backoff, so a stray 429 is fine. If
it's *sustained*, bump `SLEEP_BETWEEN_WRITES` near the top of `tmp_to_cmp.py`.

### `POST /rest/api/3/search/jql -> 400`

Jira's newer search endpoint is strict: no field data unless `fields` is in the
body, no `startAt` (pagination is token-based), and no null first-page token. The
tool handles all three — and the error log includes the full request body so you
can see exactly what went out.

### `Field 'customfield_25199' does not exist` (JQL 400)

This is the two-names gotcha biting. JQL matches custom fields by **name**, not by
the `customfield_x` id. `validate`, `report`, and `copy-fields` filter on the key
field, so they need its name. Set `JIRA_KEY_FIELD_NAME` to the exact field-picker
name (including any `[Short text]` suffix), e.g. `Original Work Item Key[Short text]`.
Run `doctor` to confirm the id and resolved name line up.

### Sprint reopen fails with an active-sprint conflict

A board allows one active sprint unless **parallel sprints** are enabled.
`update-sprints` reopens one closed sprint at a time, but if another's already
active, the reopen fails. Enable parallel sprints and re-run — or, honestly, just
use [`export-sprint-csv`](#75b-export-sprint-csv--the-move-that-always-works) and
sidestep the whole thing.

### `Baseline not found. Run 'extract' first.` / `Sprint mapping not found. Run 'build' first.`

`validate`/`report` read `extract/11_inventory_baseline.json`; `update-sprints`
reads `build/sprint_id_mapping.csv`. If you used a custom `--output-dir`, point
*every* command at the same one — it's a global option, so it goes before the
subcommand.

---

## 11. Known limitations — read before you promise anything

These aren't bugs to fix later; they're constraints of Jira itself. Set
expectations with your stakeholders accordingly.

1. **Sprint `completeDate` can't be backdated.** The Agile API stamps it at the
   moment of close. Closed sprints created by `build`, and ones re-closed by
   `update-sprints`, show the run time, not the original. The reports flag the
   affected sprints; if anyone relies on historical Velocity, it may need
   recomputing.

2. **TMP-scoped custom field ids aren't portable.** That's why `extract` catalogs
   every field and `copy-fields` does the re-stitching by matching on the stamped
   key. There's no shortcut around the mapping CSV.

3. **Board configuration is exported for reference only.** Columns, swimlanes,
   quick filters, card layout — all manual rebuild on the target. I usually treat
   this as a chance to clean them up rather than copy old cruft blindly.

4. **Issues spanning multiple closed sprints are skipped by `update-sprints`**
   unless parallel sprints are enabled. They're listed in `multi_closed_issues.csv`.
   The robust answer is
   [`export-sprint-csv`](#75b-export-sprint-csv--the-move-that-always-works) —
   the External System Import has no reopen/active-sprint restriction, so closed
   and multi-closed sprints import cleanly.

5. **`stamp-keys` notification suppression covers the field update only.** Source
   automation rules on "Issue Updated" can still fire — **disable source automation
   before running.** I've said it twice now because it's the one that surprises
   people.

6. **Schemes, workflows, screens, and global custom fields are out of scope.** You
   build the target shell and assign schemes in the UI
   ([§7.3.1](#731-create-the-target-project-in-the-ui)); the toolkit handles
   versions, components, and sprints. The design decisions stay with you, where
   they belong.
```
