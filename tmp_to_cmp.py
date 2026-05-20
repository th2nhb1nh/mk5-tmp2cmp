#!/usr/bin/env python3
"""
Jira TMP -> CMP Migration Toolkit (Phase 3: extract & rebuild)

Subcommands:
  extract     Single-pass scan of source TMP. Writes all artifacts.
  stamp-keys  Write each source issue's current key into a designated text
              custom field. Survives bulk-move.
  build       Recreate versions, components, sprints in target CMP from
              extract artifacts. Outputs ID mapping CSVs.
  validate    Compare source baseline counts against target post-move counts.

Auth: Atlassian Cloud API token + email (HTTP Basic).
Config: read directly from .env on every run (no `source` required).

Re-runnable. Each phase writes a checkpoint; re-running skips work already done.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# .env loader (no dependency on python-dotenv)
# ---------------------------------------------------------------------------

def load_dotenv(explicit_path: Optional[str] = None) -> Optional[Path]:
    """Load KEY=value pairs from .env into os.environ, overriding existing
    values so edits to .env take effect without re-sourcing the shell.

    Search order:
      1. --env-file CLI arg if provided
      2. ./.env in current working directory
      3. .env next to this script
    Returns the path that was loaded, or None.
    """
    candidates: List[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent / ".env")

    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Strip matched surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ[key] = value
        return path
    return None


# ---------------------------------------------------------------------------
# Constants & logging
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 30
PAGE_SIZE_SEARCH = 100
PAGE_SIZE_GENERIC = 50
SLEEP_BETWEEN_WRITES = 0.10


def setup_logging(output_dir: Path, verbose: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "migration.log"
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ---------------------------------------------------------------------------
# Config (env-first; CLI overrides)
# ---------------------------------------------------------------------------

@dataclass
class Config:
    base_url: str
    email: str
    api_token: str
    source_key: str
    target_key: str
    key_field_id: str
    output_dir: Path
    dry_run: bool = False
    create_target: bool = False
    target_board_id: Optional[int] = None
    target_project_lead_account_id: Optional[str] = None
    target_template: str = "com.pyxis.greenhopper.jira:gh-simplified-scrum-classic"

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        def pick(cli_val, env_key: str, default: str = "") -> str:
            if cli_val:
                return str(cli_val)
            return os.environ.get(env_key, default)

        base_url = pick(getattr(args, "base_url", None), "JIRA_BASE_URL").rstrip("/")
        email = pick(getattr(args, "email", None), "JIRA_EMAIL")
        token = os.environ.get("JIRA_API_TOKEN", "")

        missing = [
            name for name, val in [
                ("JIRA_BASE_URL", base_url),
                ("JIRA_EMAIL", email),
                ("JIRA_API_TOKEN", token),
            ] if not val
        ]
        if missing:
            raise SystemExit(
                "Missing credentials in .env (or env): " + ", ".join(missing)
                + "\nSee .env.example for the required keys."
            )

        target_board_env = os.environ.get("JIRA_TARGET_BOARD_ID")
        target_board = getattr(args, "target_board_id", None)
        if target_board is None and target_board_env:
            try:
                target_board = int(target_board_env)
            except ValueError:
                raise SystemExit(f"JIRA_TARGET_BOARD_ID must be an integer, got '{target_board_env}'")

        return cls(
            base_url=base_url,
            email=email,
            api_token=token,
            source_key=pick(getattr(args, "source_key", None), "JIRA_SOURCE_KEY"),
            target_key=pick(getattr(args, "target_key", None), "JIRA_TARGET_KEY"),
            key_field_id=pick(getattr(args, "key_field", None), "JIRA_KEY_FIELD",
                              "customfield_25199"),
            output_dir=Path(pick(getattr(args, "output_dir", None), "JIRA_OUTPUT_DIR",
                                  "./out")).resolve(),
            dry_run=getattr(args, "dry_run", False),
            create_target=getattr(args, "create_target", False),
            target_board_id=target_board,
            target_project_lead_account_id=pick(
                getattr(args, "target_lead", None), "JIRA_TARGET_LEAD"),
            target_template=pick(
                getattr(args, "target_template", None), "JIRA_TARGET_TEMPLATE",
                "com.pyxis.greenhopper.jira:gh-simplified-scrum-classic"),
        )


# ---------------------------------------------------------------------------
# Jira REST client
# ---------------------------------------------------------------------------

class JiraClient:
    """Thin wrapper around requests with retry, auth, pagination helpers."""

    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url
        token_pair = f"{email}:{api_token}".encode("utf-8")
        self.auth_header = "Basic " + base64.b64encode(token_pair).decode("ascii")

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": self.auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        retry = Retry(
            total=6, backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE"],
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def _req(self, method: str, path: str,
             expected_404: bool = False, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            # 404 callers handle locally; downgrade to DEBUG to avoid alarm
            level = logging.DEBUG if (resp.status_code == 404 and expected_404) \
                else (logging.WARNING if resp.status_code < 500 else logging.ERROR)
            logging.log(level, "%s %s -> %s: %s",
                        method, url, resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return resp

    def get(self, path: str, params: Optional[Dict[str, Any]] = None,
            expected_404: bool = False) -> Any:
        r = self._req("GET", path, params=params, expected_404=expected_404)
        return r.json() if r.content else None

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._req("POST", path, json=body or {}, params=params)
        return r.json() if r.content else None

    def put(self, path: str, body: Optional[Dict[str, Any]] = None,
            params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._req("PUT", path, json=body or {}, params=params)
        return r.json() if r.content else None

    def paged(self, path: str, params: Optional[Dict[str, Any]] = None,
              values_key: str = "values",
              page_size: int = PAGE_SIZE_GENERIC) -> Iterable[Dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("maxResults", page_size)
        start = 0
        while True:
            params["startAt"] = start
            payload = self.get(path, params=params)
            if not payload:
                break
            items = payload.get(values_key,
                                payload if isinstance(payload, list) else [])
            if not items:
                break
            for item in items:
                yield item
            if payload.get("isLast") is True:
                break
            total = payload.get("total")
            start += len(items)
            if total is not None and start >= total:
                break

    def search_jql(self, jql: str, fields: List[str],
                   expand: Optional[List[str]] = None) -> Iterable[Dict[str, Any]]:
        """Modern /search/jql endpoint with token-based pagination."""
        token: Optional[str] = None
        base_body: Dict[str, Any] = {
            "jql": jql, "fields": fields, "maxResults": PAGE_SIZE_SEARCH,
        }
        if expand:
            base_body["expand"] = expand
        while True:
            body = dict(base_body)
            if token:
                body["nextPageToken"] = token
            payload = self.post("/rest/api/3/search/jql", body=body)
            for issue in payload.get("issues", []):
                yield issue
            if payload.get("isLast", True) or not payload.get("nextPageToken"):
                break
            token = payload["nextPageToken"]

    def approximate_count(self, jql: str) -> int:
        payload = self.post("/rest/api/3/search/approximate-count",
                            body={"jql": jql})
        return int(payload.get("count", 0))


# ---------------------------------------------------------------------------
# Auth verification (every command runs this first)
# ---------------------------------------------------------------------------

def verify_auth(client: JiraClient) -> Dict[str, Any]:
    """Hit /myself before doing anything else. Fail fast on auth issues."""
    try:
        me = client.get("/rest/api/3/myself")
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code in (401, 403):
            raise SystemExit(
                f"Authentication failed ({code}). Check JIRA_EMAIL and "
                f"JIRA_API_TOKEN in .env. Token generated at "
                f"https://id.atlassian.com/manage-profile/security/api-tokens"
            )
        raise SystemExit(f"Auth precheck failed with HTTP {code}: {e}")
    except requests.RequestException as e:
        raise SystemExit(
            f"Cannot reach {client.base_url}. Check JIRA_BASE_URL and network. ({e})"
        )

    logging.info("Authenticated as %s <%s>",
                 me.get("displayName"), me.get("emailAddress"))
    logging.debug("accountId=%s", me.get("accountId"))
    return me


def verify_project_exists(client: JiraClient, key: str, label: str) -> Dict[str, Any]:
    """Confirm a project key resolves. Friendly error if not."""
    proj = client.get(f"/rest/api/3/project/{key}", expected_404=True) \
        if False else None
    try:
        proj = client.get(f"/rest/api/3/project/{key}", expected_404=True)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise SystemExit(
                f"{label} project '{key}' not found on {client.base_url}. "
                f"Check the key in .env."
            )
        raise
    logging.info("%s project: %s (id=%s, style=%s)",
                 label, proj["key"], proj["id"], proj.get("style", "?"))
    return proj


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logging.info("Wrote %s", path)


def write_csv(path: Path, rows: List[Dict[str, Any]],
              fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logging.info("Wrote %s (%d rows)", path, len(rows))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def confirm(prompt: str, dry_run: bool) -> bool:
    if dry_run:
        logging.info("[DRY-RUN] %s -> skipped", prompt)
        return False
    print(prompt + " [y/N]: ", end="", flush=True)
    return input().strip().lower() == "y"


# ---------------------------------------------------------------------------
# EXTRACT phase - single pass over source issues
# ---------------------------------------------------------------------------

def scan_source(client: JiraClient, source_key: str) -> Dict[str, Any]:
    """One iteration of source issues; builds issue types, statuses,
    custom fields, and inventory buckets together."""
    logging.info("Scanning source issues (this is the single pass)...")

    field_catalog = {f["id"]: f for f in (client.get("/rest/api/3/field") or [])}
    sprint_field_ids = {
        fid for fid, f in field_catalog.items()
        if (f.get("schema") or {}).get("custom", "").endswith(":gh-sprint")
    }

    issue_types: Dict[str, Dict[str, Any]] = {}
    statuses: Dict[str, Dict[str, Any]] = {}
    custom_fields: Dict[str, Dict[str, Any]] = {}
    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_resolution: Dict[str, int] = {}
    counters = {
        "total": 0,
        "withAttachments": 0,
        "withComments": 0,
        "withFixVersion": 0,
        "withComponents": 0,
        "withSprint": 0,
    }

    jql = f'project = "{source_key}"'
    for issue in client.search_jql(jql, fields=["*all"], expand=["names"]):
        counters["total"] += 1
        f = issue["fields"]
        names = issue.get("names", {}) or {}

        # Issue type
        it = f.get("issuetype") or {}
        if it.get("id") and it["id"] not in issue_types:
            issue_types[it["id"]] = {
                "id": it["id"], "name": it.get("name"),
                "subtask": it.get("subtask", False),
                "hierarchyLevel": it.get("hierarchyLevel"),
            }
        by_type[it.get("name", "?")] = by_type.get(it.get("name", "?"), 0) + 1

        # Status (per issue type)
        st = f.get("status") or {}
        st_key = f"{it.get('name')}::{st.get('name')}"
        if st.get("id") and st_key not in statuses:
            statuses[st_key] = {
                "issueType": it.get("name"),
                "statusName": st.get("name"),
                "statusId": st.get("id"),
                "statusCategory": (st.get("statusCategory") or {}).get("key"),
            }
        by_status[st.get("name", "?")] = by_status.get(st.get("name", "?"), 0) + 1

        # Resolution
        res = f.get("resolution")
        res_name = res["name"] if isinstance(res, dict) else "<unresolved>"
        by_resolution[res_name] = by_resolution.get(res_name, 0) + 1

        # Inventory flags
        if f.get("attachment"):
            counters["withAttachments"] += 1
        comment_obj = f.get("comment")
        if isinstance(comment_obj, dict) and comment_obj.get("comments"):
            counters["withComments"] += 1
        if f.get("fixVersions"):
            counters["withFixVersion"] += 1
        if f.get("components"):
            counters["withComponents"] += 1
        if any(f.get(fid) for fid in sprint_field_ids):
            counters["withSprint"] += 1

        # Custom fields with non-empty values
        for fid, val in f.items():
            if not fid.startswith("customfield_"):
                continue
            if val in (None, "", [], {}):
                continue
            if fid in custom_fields:
                continue
            cat = field_catalog.get(fid, {})
            schema = cat.get("schema") or {}
            custom_fields[fid] = {
                "fieldId": fid,
                "displayName": names.get(fid) or cat.get("name", ""),
                "schemaType": schema.get("type", ""),
                "schemaCustom": schema.get("custom", ""),
                "exampleValue": str(val)[:200],
            }

        if counters["total"] % 200 == 0:
            logging.info("...scanned %d issues", counters["total"])

    logging.info("Scan complete: %d issues, %d distinct issue types, "
                 "%d distinct status pairs, %d custom fields in use",
                 counters["total"], len(issue_types), len(statuses),
                 len(custom_fields))

    return {
        "issue_types": list(issue_types.values()),
        "statuses": list(statuses.values()),
        "custom_fields": list(custom_fields.values()),
        "by_type": by_type,
        "by_status": by_status,
        "by_resolution": by_resolution,
        "counters": counters,
    }


def extract_project_meta(client: JiraClient, key: str) -> Dict[str, Any]:
    meta = client.get(f"/rest/api/3/project/{key}",
                      params={"expand": "description,lead,issueTypes"})
    return {
        "id": meta["id"], "key": meta["key"], "name": meta["name"],
        "projectTypeKey": meta.get("projectTypeKey"),
        "style": meta.get("style"),
        "description": meta.get("description"),
        "lead": (meta.get("lead") or {}).get("accountId"),
        "leadDisplayName": (meta.get("lead") or {}).get("displayName"),
        "assigneeType": meta.get("assigneeType"),
        "category": (meta.get("projectCategory") or {}).get("name"),
        "url": meta.get("url"),
    }


def extract_versions(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    versions = client.get(f"/rest/api/3/project/{key}/versions") or []
    return [{
        "sequence": i, "id": v["id"], "name": v["name"],
        "description": v.get("description", ""),
        "startDate": v.get("startDate", ""),
        "releaseDate": v.get("releaseDate", ""),
        "released": v.get("released", False),
        "archived": v.get("archived", False),
    } for i, v in enumerate(versions)]


def extract_components(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    comps = client.get(f"/rest/api/3/project/{key}/components") or []
    return [{
        "id": c["id"], "name": c["name"],
        "description": c.get("description", ""),
        "leadAccountId": (c.get("lead") or {}).get("accountId", ""),
        "assigneeType": c.get("assigneeType", "PROJECT_DEFAULT"),
        "realAssigneeType": c.get("realAssigneeType", ""),
    } for c in comps]


def find_source_board(client: JiraClient, key: str) -> Optional[Dict[str, Any]]:
    boards = list(client.paged("/rest/agile/1.0/board",
                                params={"projectKeyOrId": key}, page_size=50))
    if not boards:
        return None
    scrum = [b for b in boards if b.get("type") == "scrum"]
    return (scrum or boards)[0]


def extract_sprints(client: JiraClient, board_id: int) -> List[Dict[str, Any]]:
    sprints = list(client.paged(f"/rest/agile/1.0/board/{board_id}/sprint",
                                params={"state": "closed,active,future"},
                                page_size=50))
    state_order = {"closed": 0, "active": 1, "future": 2}

    def sort_key(s):
        return (state_order.get(s.get("state", "future"), 3),
                s.get("completeDate") or s.get("startDate") or "")

    return [{
        "id": s["id"], "name": s["name"], "state": s["state"],
        "startDate": s.get("startDate", ""),
        "endDate": s.get("endDate", ""),
        "completeDate": s.get("completeDate", ""),
        "goal": s.get("goal", ""),
        "originBoardId": s.get("originBoardId"),
    } for s in sorted(sprints, key=sort_key)]


def extract_roles(client: JiraClient, key: str) -> Dict[str, Any]:
    role_map = client.get(f"/rest/api/3/project/{key}/role") or {}
    out: Dict[str, Any] = {}
    for role_name, role_url in role_map.items():
        detail = client.get(role_url)
        out[role_name] = {
            "id": detail.get("id"),
            "actors": [{
                "displayName": a.get("displayName"),
                "type": a.get("type"),
                "accountId": (a.get("actorUser") or {}).get("accountId"),
                "groupName": (a.get("actorGroup") or {}).get("name"),
            } for a in detail.get("actors", [])],
        }
    return out


def cmd_extract(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    verify_auth(client)
    verify_project_exists(client, cfg.source_key, "Source")

    out = cfg.output_dir / "extract"
    out.mkdir(parents=True, exist_ok=True)
    logging.info("=== EXTRACT phase: source project %s ===", cfg.source_key)

    meta = extract_project_meta(client, cfg.source_key)
    write_json(out / "01_project_meta.json", meta)
    if meta.get("style") != "next-gen":
        logging.warning("Source style is '%s' (expected 'next-gen' for TMP).",
                        meta.get("style"))

    versions = extract_versions(client, cfg.source_key)
    write_json(out / "04_versions.json", versions)
    write_csv(out / "04_versions.csv", versions,
              ["sequence", "id", "name", "description", "startDate",
               "releaseDate", "released", "archived"])

    components = extract_components(client, cfg.source_key)
    write_json(out / "05_components.json", components)
    write_csv(out / "05_components.csv", components,
              ["id", "name", "description", "leadAccountId",
               "assigneeType", "realAssigneeType"])

    board = find_source_board(client, cfg.source_key)
    if board:
        write_json(out / "06_source_board.json", board)
        sprints = extract_sprints(client, board["id"])
        write_json(out / "07_sprints.json", sprints)
        write_csv(out / "07_sprints.csv", sprints,
                  ["id", "name", "state", "startDate", "endDate",
                   "completeDate", "goal", "originBoardId"])
        board_cfg = client.get(f"/rest/agile/1.0/board/{board['id']}/configuration")
        write_json(out / "08_source_board_config.json", board_cfg)
    else:
        logging.warning("No board on source; skipping sprint extract.")

    roles = extract_roles(client, cfg.source_key)
    write_json(out / "09_roles.json", roles)

    # Single-pass scan: issue types, statuses, custom fields, inventory
    scan = scan_source(client, cfg.source_key)
    write_json(out / "02_issue_types.json", scan["issue_types"])
    write_csv(out / "02_issue_types.csv", scan["issue_types"],
              ["id", "name", "subtask", "hierarchyLevel"])
    write_json(out / "03_statuses.json", scan["statuses"])
    write_csv(out / "03_statuses.csv", scan["statuses"],
              ["issueType", "statusName", "statusId", "statusCategory"])
    write_json(out / "10_custom_fields_in_use.json", scan["custom_fields"])
    write_csv(out / "10_custom_fields_in_use.csv", scan["custom_fields"],
              ["fieldId", "displayName", "schemaType",
               "schemaCustom", "exampleValue"])
    inventory = {
        **scan["counters"],
        "byType": scan["by_type"],
        "byStatus": scan["by_status"],
        "byResolution": scan["by_resolution"],
    }
    write_json(out / "11_inventory_baseline.json", inventory)

    logging.info("Extract complete. Artifacts in %s", out)


# ---------------------------------------------------------------------------
# STAMP-KEYS phase
# ---------------------------------------------------------------------------

def precheck_key_field(client: JiraClient, field_id: str) -> Dict[str, Any]:
    fields = client.get("/rest/api/3/field") or []
    found = next((f for f in fields if f.get("id") == field_id), None)
    if not found:
        raise SystemExit(f"Field {field_id} not found in this Jira site.")
    schema_type = (found.get("schema") or {}).get("type")
    if schema_type not in ("string", "any"):
        raise SystemExit(
            f"Field {field_id} ('{found.get('name')}') has schema.type="
            f"'{schema_type}'. Must be a text (string) field."
        )
    logging.info("Key field OK: %s = '%s' (type=%s)",
                 field_id, found.get("name"), schema_type)
    return found


def stamp_test_first(client: JiraClient, source_key: str,
                     field_id: str, dry_run: bool) -> str:
    first = next(
        client.search_jql(f'project = "{source_key}" ORDER BY created ASC',
                          fields=["summary"]),
        None,
    )
    if not first:
        raise SystemExit(f"No issues found in source project {source_key}.")
    issue_key = first["key"]
    if dry_run:
        logging.info("[DRY-RUN] Would test-stamp %s", issue_key)
        return issue_key
    logging.info("Test stamp on %s", issue_key)
    client.put(f"/rest/api/3/issue/{issue_key}",
               body={"fields": {field_id: issue_key}},
               params={"notifyUsers": "false"})
    check = client.get(f"/rest/api/3/issue/{issue_key}",
                       params={"fields": field_id})
    actual = check["fields"].get(field_id)
    if actual != issue_key:
        raise SystemExit(
            f"Test stamp did not persist on {issue_key}. "
            f"Expected '{issue_key}', got '{actual}'. "
            f"Confirm {field_id} is on the edit screen of all issue types "
            f"in source TMP."
        )
    logging.info("Test stamp verified on %s", issue_key)
    return issue_key


def cmd_stamp_keys(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    verify_auth(client)
    verify_project_exists(client, cfg.source_key, "Source")

    out = cfg.output_dir / "stamp"
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / "stamped.txt"
    failures_path = out / "stamp_failures.csv"

    logging.info("=== STAMP-KEYS phase: %s -> %s ===",
                 cfg.source_key, cfg.key_field_id)

    precheck_key_field(client, cfg.key_field_id)
    stamp_test_first(client, cfg.source_key, cfg.key_field_id, cfg.dry_run)

    done: set = set()
    if checkpoint.exists():
        done = set(checkpoint.read_text(encoding="utf-8").splitlines())
        logging.info("Resuming: %d issues already stamped per checkpoint", len(done))

    total = client.approximate_count(f'project = "{cfg.source_key}"')
    logging.info("Approximate total issues: %d", total)

    if not cfg.dry_run and not confirm(
        f"Stamp {cfg.key_field_id} on up to {total} issues in {cfg.source_key}?",
        cfg.dry_run,
    ):
        logging.info("Aborted by user.")
        return

    failures: List[Dict[str, str]] = []
    count = 0
    with checkpoint.open("a", encoding="utf-8") as ckpt:
        for issue in client.search_jql(f'project = "{cfg.source_key}"',
                                        fields=["summary"]):
            key = issue["key"]
            if key in done:
                continue
            count += 1
            try:
                if cfg.dry_run:
                    logging.debug("[DRY-RUN] stamp %s", key)
                else:
                    client.put(f"/rest/api/3/issue/{key}",
                               body={"fields": {cfg.key_field_id: key}},
                               params={"notifyUsers": "false"})
                    ckpt.write(key + "\n")
                    ckpt.flush()
                    time.sleep(SLEEP_BETWEEN_WRITES)
                if count % 100 == 0:
                    logging.info("Stamped %d issues...", count)
            except requests.HTTPError as e:
                failures.append({"key": key, "error": str(e)[:300]})
                logging.warning("Failed to stamp %s: %s", key, e)

    if failures:
        write_csv(failures_path, failures, ["key", "error"])
        logging.warning("%d failures recorded in %s", len(failures), failures_path)
    logging.info("Stamp complete. Processed %d issues this run.", count)


# ---------------------------------------------------------------------------
# BUILD phase
# ---------------------------------------------------------------------------

def get_project_or_none(client: JiraClient, key: str) -> Optional[Dict[str, Any]]:
    try:
        return client.get(f"/rest/api/3/project/{key}", expected_404=True)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def create_target_project(client: JiraClient, cfg: Config,
                          source_meta: Dict[str, Any]) -> Dict[str, Any]:
    if not cfg.target_project_lead_account_id:
        raise SystemExit("--target-lead <accountId> (or JIRA_TARGET_LEAD env) "
                         "required when --create-target is set.")
    body = {
        "key": cfg.target_key,
        "name": source_meta["name"] + " (CMP)",
        "projectTypeKey": "software",
        "projectTemplateKey": cfg.target_template,
        "leadAccountId": cfg.target_project_lead_account_id,
        "assigneeType": source_meta.get("assigneeType") or "PROJECT_LEAD",
        "description": source_meta.get("description") or "",
    }
    logging.info("Creating target CMP %s ...", cfg.target_key)
    if cfg.dry_run:
        logging.info("[DRY-RUN] POST /rest/api/3/project body=%s", body)
        return {"id": "0", "key": cfg.target_key}
    return client.post("/rest/api/3/project", body=body)


def _existing_by_name(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {x["name"]: x for x in (items or [])}


def build_versions(client: JiraClient, cfg: Config,
                   target_project_id: str) -> List[Dict[str, Any]]:
    src_path = cfg.output_dir / "extract" / "04_versions.json"
    if not src_path.exists():
        logging.warning("No versions extract found; skipping.")
        return []
    versions = read_json(src_path)
    existing = _existing_by_name(
        client.get(f"/rest/api/3/project/{cfg.target_key}/versions") or []
    )

    mapping: List[Dict[str, Any]] = []
    for v in versions:
        name = v["name"]
        if name in existing:
            mapping.append({
                "source_id": v["id"], "source_name": name,
                "target_id": existing[name]["id"], "action": "existing",
            })
            logging.info("Version exists, skipping: %s", name)
            continue

        body = {"name": name, "projectId": int(target_project_id)}
        for opt in ("description", "startDate", "releaseDate"):
            if v.get(opt):
                body[opt] = v[opt]

        if cfg.dry_run:
            logging.info("[DRY-RUN] Create version %s", name)
            mapping.append({"source_id": v["id"], "source_name": name,
                            "target_id": "(dry-run)", "action": "would-create"})
            continue

        created = client.post("/rest/api/3/version", body=body)
        target_id = created["id"]
        if v.get("released") or v.get("archived"):
            patch: Dict[str, Any] = {}
            if v.get("released"):
                patch["released"] = True
                if v.get("releaseDate"):
                    patch["releaseDate"] = v["releaseDate"]
            if v.get("archived"):
                patch["archived"] = True
            client.put(f"/rest/api/3/version/{target_id}", body=patch)

        mapping.append({"source_id": v["id"], "source_name": name,
                        "target_id": target_id, "action": "created"})
        time.sleep(SLEEP_BETWEEN_WRITES)

    write_csv(cfg.output_dir / "build" / "version_id_mapping.csv", mapping,
              ["source_id", "source_name", "target_id", "action"])
    return mapping


def build_components(client: JiraClient, cfg: Config) -> List[Dict[str, Any]]:
    src_path = cfg.output_dir / "extract" / "05_components.json"
    if not src_path.exists():
        logging.warning("No components extract found; skipping.")
        return []
    components = read_json(src_path)
    existing = _existing_by_name(
        client.get(f"/rest/api/3/project/{cfg.target_key}/components") or []
    )

    mapping: List[Dict[str, Any]] = []
    for c in components:
        name = c["name"]
        if name in existing:
            mapping.append({"source_id": c["id"], "source_name": name,
                            "target_id": existing[name]["id"], "action": "existing"})
            logging.info("Component exists, skipping: %s", name)
            continue
        body: Dict[str, Any] = {"name": name, "project": cfg.target_key}
        for opt in ("description", "leadAccountId", "assigneeType"):
            if c.get(opt):
                body[opt] = c[opt]

        if cfg.dry_run:
            logging.info("[DRY-RUN] Create component %s", name)
            mapping.append({"source_id": c["id"], "source_name": name,
                            "target_id": "(dry-run)", "action": "would-create"})
            continue

        created = client.post("/rest/api/3/component", body=body)
        mapping.append({"source_id": c["id"], "source_name": name,
                        "target_id": created["id"], "action": "created"})
        time.sleep(SLEEP_BETWEEN_WRITES)

    write_csv(cfg.output_dir / "build" / "component_id_mapping.csv", mapping,
              ["source_id", "source_name", "target_id", "action"])
    return mapping


def build_sprints(client: JiraClient, cfg: Config) -> List[Dict[str, Any]]:
    if not cfg.target_board_id:
        logging.warning("No --target-board-id (or JIRA_TARGET_BOARD_ID). "
                        "Skipping sprint creation. Create a Scrum board on "
                        "the target CMP, then re-run.")
        return []
    src_path = cfg.output_dir / "extract" / "07_sprints.json"
    if not src_path.exists():
        logging.warning("No sprints extract found; skipping.")
        return []
    sprints = read_json(src_path)

    try:
        existing_by_name = {
            s["name"]: s for s in client.paged(
                f"/rest/agile/1.0/board/{cfg.target_board_id}/sprint",
                params={"state": "closed,active,future"}, page_size=50,
            )
        }
    except requests.HTTPError:
        existing_by_name = {}

    mapping: List[Dict[str, Any]] = []
    for s in sprints:
        name = s["name"]
        if name in existing_by_name:
            mapping.append({
                "source_id": s["id"], "source_name": name,
                "source_state": s["state"],
                "target_id": existing_by_name[name]["id"],
                "target_state": existing_by_name[name]["state"],
                "action": "existing", "note": "",
            })
            continue

        body: Dict[str, Any] = {"name": name,
                                "originBoardId": cfg.target_board_id}
        for opt in ("startDate", "endDate", "goal"):
            if s.get(opt):
                body[opt] = s[opt]

        if cfg.dry_run:
            logging.info("[DRY-RUN] Create sprint %s (-> %s)", name, s["state"])
            mapping.append({
                "source_id": s["id"], "source_name": name,
                "source_state": s["state"],
                "target_id": "(dry-run)", "target_state": "future",
                "action": "would-create", "note": "",
            })
            continue

        created = client.post("/rest/agile/1.0/sprint", body=body)
        target_id = created["id"]
        target_state = "future"

        if s["state"] == "active":
            client.post(f"/rest/agile/1.0/sprint/{target_id}",
                        body={"state": "active"})
            target_state = "active"
        elif s["state"] == "closed":
            client.post(f"/rest/agile/1.0/sprint/{target_id}",
                        body={"state": "active"})
            client.post(f"/rest/agile/1.0/sprint/{target_id}",
                        body={"state": "closed"})
            target_state = "closed"

        mapping.append({
            "source_id": s["id"], "source_name": name,
            "source_state": s["state"],
            "target_id": target_id, "target_state": target_state,
            "action": "created",
            "note": ("completeDate set to now, not original"
                     if s["state"] == "closed" else ""),
        })
        time.sleep(SLEEP_BETWEEN_WRITES)

    write_csv(cfg.output_dir / "build" / "sprint_id_mapping.csv", mapping,
              ["source_id", "source_name", "source_state",
               "target_id", "target_state", "action", "note"])
    return mapping


def cmd_build(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    verify_auth(client)

    out = cfg.output_dir / "build"
    out.mkdir(parents=True, exist_ok=True)
    logging.info("=== BUILD phase: target %s ===", cfg.target_key)

    target = get_project_or_none(client, cfg.target_key)
    source_meta_path = cfg.output_dir / "extract" / "01_project_meta.json"
    if not source_meta_path.exists():
        raise SystemExit("No extract artifacts found. Run 'extract' first.")
    source_meta = read_json(source_meta_path)

    if target is None:
        if not cfg.create_target:
            raise SystemExit(
                f"Target project '{cfg.target_key}' not found on "
                f"{cfg.base_url}.\n"
                f"  - Create it in the Jira UI (recommended, lets you assign "
                f"schemes), OR\n"
                f"  - Re-run with --create-target --target-lead <accountId>."
            )
        target = create_target_project(client, cfg, source_meta)
    else:
        logging.info("Target project %s exists (id=%s, style=%s)",
                     target["key"], target["id"], target.get("style", "?"))

    build_versions(client, cfg, str(target["id"]))
    build_components(client, cfg)
    build_sprints(client, cfg)
    logging.info("Build complete. Mappings in %s", out)


# ---------------------------------------------------------------------------
# VALIDATE phase
# ---------------------------------------------------------------------------

def cmd_validate(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    verify_auth(client)
    verify_project_exists(client, cfg.target_key, "Target")

    out = cfg.output_dir / "validate"
    out.mkdir(parents=True, exist_ok=True)

    baseline_path = cfg.output_dir / "extract" / "11_inventory_baseline.json"
    if not baseline_path.exists():
        raise SystemExit("Baseline inventory not found. Run 'extract' first.")
    baseline = read_json(baseline_path)

    scan = scan_source(client, cfg.target_key)
    target_inv = {
        **scan["counters"],
        "byType": scan["by_type"],
        "byStatus": scan["by_status"],
        "byResolution": scan["by_resolution"],
    }
    write_json(out / "target_inventory.json", target_inv)

    def diff_dict(src: Dict[str, int], tgt: Dict[str, int]) -> Dict[str, Dict[str, int]]:
        return {k: {"source": src.get(k, 0), "target": tgt.get(k, 0),
                    "diff": tgt.get(k, 0) - src.get(k, 0)}
                for k in sorted(set(src) | set(tgt))}

    flat_keys = ("total", "withAttachments", "withComments",
                 "withFixVersion", "withComponents", "withSprint")
    delta = {k: {"source": baseline.get(k, 0), "target": target_inv.get(k, 0),
                 "diff": target_inv.get(k, 0) - baseline.get(k, 0)}
             for k in flat_keys}
    delta["byType"] = diff_dict(baseline.get("byType", {}),
                                 target_inv.get("byType", {}))
    delta["byStatus"] = diff_dict(baseline.get("byStatus", {}),
                                   target_inv.get("byStatus", {}))
    delta["byResolution"] = diff_dict(baseline.get("byResolution", {}),
                                       target_inv.get("byResolution", {}))
    write_json(out / "validation_delta.json", delta)

    lines = ["category,source,target,diff"]
    for k in flat_keys:
        d = delta[k]
        lines.append(f"{k},{d['source']},{d['target']},{d['diff']}")
    (out / "validation_summary.csv").write_text("\n".join(lines), encoding="utf-8")
    logging.info("Validation complete. See %s", out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Jira TMP -> CMP migration toolkit (env-driven; loads .env on each run)")
    p.add_argument("--env-file", help="Path to .env (default: ./.env or alongside script)")
    p.add_argument("--base-url", help="Override JIRA_BASE_URL")
    p.add_argument("--email", help="Override JIRA_EMAIL")
    p.add_argument("--output-dir", help="Override JIRA_OUTPUT_DIR (default ./out)")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="Extract source TMP data.")
    p_ext.add_argument("--source-key", help="Override JIRA_SOURCE_KEY")

    p_stamp = sub.add_parser("stamp-keys",
                              help="Stamp issue keys into text custom field.")
    p_stamp.add_argument("--source-key", help="Override JIRA_SOURCE_KEY")
    p_stamp.add_argument("--key-field", help="Override JIRA_KEY_FIELD")
    p_stamp.add_argument("--dry-run", action="store_true")

    p_build = sub.add_parser("build",
                              help="Recreate versions/components/sprints in target.")
    p_build.add_argument("--target-key", help="Override JIRA_TARGET_KEY")
    p_build.add_argument("--target-board-id", type=int,
                         help="Override JIRA_TARGET_BOARD_ID")
    p_build.add_argument("--create-target", action="store_true",
                         help="Create target if it doesn't exist (limited scheme control).")
    p_build.add_argument("--target-lead", help="Lead accountId for --create-target.")
    p_build.add_argument("--target-template", help="Project template for --create-target.")
    p_build.add_argument("--dry-run", action="store_true")

    p_val = sub.add_parser("validate",
                            help="Compare baseline vs target inventory.")
    p_val.add_argument("--source-key", help="Override JIRA_SOURCE_KEY (label only)")
    p_val.add_argument("--target-key", help="Override JIRA_TARGET_KEY")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Load .env FIRST so cfg can read from os.environ
    loaded = load_dotenv(getattr(args, "env_file", None))
    output_dir = Path(getattr(args, "output_dir", None)
                      or os.environ.get("JIRA_OUTPUT_DIR")
                      or "./out").resolve()
    setup_logging(output_dir, args.verbose)
    if loaded:
        logging.info("Loaded env from %s", loaded)
    else:
        logging.info("No .env file found; using shell environment only.")

    try:
        cfg = Config.from_args(args)
    except SystemExit:
        raise
    except Exception:
        logging.exception("Config error")
        return 2

    try:
        if args.cmd == "extract":
            if not cfg.source_key:
                raise SystemExit("source key required (JIRA_SOURCE_KEY or --source-key).")
            cmd_extract(cfg)
        elif args.cmd == "stamp-keys":
            if not cfg.source_key:
                raise SystemExit("source key required (JIRA_SOURCE_KEY or --source-key).")
            cmd_stamp_keys(cfg)
        elif args.cmd == "build":
            if not cfg.target_key:
                raise SystemExit("target key required (JIRA_TARGET_KEY or --target-key).")
            cmd_build(cfg)
        elif args.cmd == "validate":
            if not cfg.target_key:
                raise SystemExit("target key required (JIRA_TARGET_KEY or --target-key).")
            cmd_validate(cfg)
        else:
            raise SystemExit(f"Unknown command: {args.cmd}")
    except SystemExit:
        raise
    except Exception:
        logging.exception("Unhandled error")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())