#!/usr/bin/env python3
"""
Jira TMP -> CMP Migration Toolkit (Phase 3: extract & rebuild)

Subcommands:
  extract     Pull source TMP project config + inventory to local artifacts.
  stamp-keys  Write each source issue's current key into a designated text
              custom field (e.g. customfield_25199). Survives bulk move.
  build       Recreate versions, components, and sprints in the target CMP
              from extracted artifacts. Outputs ID mapping CSVs.
  validate    Compare source baseline counts against target post-move counts.

Auth: Atlassian Cloud API token + email (HTTP Basic).

Assumptions:
  - Source is a Jira Software TMP (has a board + sprints).
  - Target CMP shell already exists (created via UI to allow scheme assignment),
    OR pass --create-target to have this script create a minimal shell.
  - Target customfield used for key preservation is a single-line text field
    that has been added to all relevant screens of the SOURCE project before
    running stamp-keys.

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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 30
PAGE_SIZE_SEARCH = 100
PAGE_SIZE_GENERIC = 50
SLEEP_BETWEEN_WRITES = 0.10  # gentle rate limiting on writes


def setup_logging(output_dir: Path, verbose: bool) -> None:
    log_path = output_dir / "migration.log"
    output_dir.mkdir(parents=True, exist_ok=True)
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
        base = os.environ.get("JIRA_BASE_URL", args.base_url or "").rstrip("/")
        email = os.environ.get("JIRA_EMAIL", args.email or "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        if not (base and email and token):
            raise SystemExit(
                "Missing credentials. Set JIRA_BASE_URL, JIRA_EMAIL, "
                "JIRA_API_TOKEN env vars (see .env.example)."
            )
        return cls(
            base_url=base,
            email=email,
            api_token=token,
            source_key=getattr(args, "source_key", "") or "",
            target_key=getattr(args, "target_key", "") or "",
            key_field_id=getattr(args, "key_field", "customfield_25199"),
            output_dir=Path(getattr(args, "output_dir", "./out")).resolve(),
            dry_run=getattr(args, "dry_run", False),
            create_target=getattr(args, "create_target", False),
            target_board_id=getattr(args, "target_board_id", None),
            target_project_lead_account_id=getattr(args, "target_lead", None),
            target_template=getattr(
                args, "target_template",
                "com.pyxis.greenhopper.jira:gh-simplified-scrum-classic",
            ),
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
            total=6,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE"],
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def _req(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            logging.error("%s %s -> %s: %s", method, url, resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return resp

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._req("GET", path, params=params)
        if not r.content:
            return None
        return r.json()

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._req("POST", path, json=body or {}, params=params)
        if not r.content:
            return None
        return r.json()

    def put(self, path: str, body: Optional[Dict[str, Any]] = None,
            params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._req("PUT", path, json=body or {}, params=params)
        if not r.content:
            return None
        return r.json()

    def paged(self, path: str, params: Optional[Dict[str, Any]] = None,
              values_key: str = "values", page_size: int = PAGE_SIZE_GENERIC) -> Iterable[Dict[str, Any]]:
        """Generic offset-based pagination (Agile API, fields, etc.)."""
        params = dict(params or {})
        params.setdefault("maxResults", page_size)
        start = 0
        while True:
            params["startAt"] = start
            payload = self.get(path, params=params)
            if not payload:
                break
            items = payload.get(values_key, payload if isinstance(payload, list) else [])
            for item in items:
                yield item
            is_last = payload.get("isLast")
            total = payload.get("total")
            got = len(items)
            if got == 0:
                break
            start += got
            if is_last is True:
                break
            if total is not None and start >= total:
                break

    def search_jql(self, jql: str, fields: List[str],
                   expand: Optional[List[str]] = None) -> Iterable[Dict[str, Any]]:
        """Paginate via the modern /search/jql endpoint (token-based)."""
        token: Optional[str] = None
        body_base: Dict[str, Any] = {
            "jql": jql,
            "fields": fields,
            "maxResults": PAGE_SIZE_SEARCH,
        }
        if expand:
            body_base["expand"] = expand
        while True:
            body = dict(body_base)
            if token:
                body["nextPageToken"] = token
            payload = self.post("/rest/api/3/search/jql", body=body)
            for issue in payload.get("issues", []):
                yield issue
            if payload.get("isLast", True) or not payload.get("nextPageToken"):
                break
            token = payload["nextPageToken"]

    def approximate_count(self, jql: str) -> int:
        payload = self.post("/rest/api/3/search/approximate-count", body={"jql": jql})
        return int(payload.get("count", 0))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logging.info("Wrote %s", path)


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
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
# EXTRACT phase
# ---------------------------------------------------------------------------

def extract_project_meta(client: JiraClient, key: str) -> Dict[str, Any]:
    meta = client.get(f"/rest/api/3/project/{key}",
                      params={"expand": "description,lead,projectKeys,issueTypes"})
    return {
        "id": meta["id"],
        "key": meta["key"],
        "name": meta["name"],
        "projectTypeKey": meta.get("projectTypeKey"),
        "style": meta.get("style"),  # 'next-gen' = TMP, 'classic' = CMP
        "description": meta.get("description"),
        "lead": (meta.get("lead") or {}).get("accountId"),
        "leadDisplayName": (meta.get("lead") or {}).get("displayName"),
        "assigneeType": meta.get("assigneeType"),
        "category": (meta.get("projectCategory") or {}).get("name"),
        "url": meta.get("url"),
    }


def extract_issue_types_in_use(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    jql = f'project = "{key}"'
    seen: Dict[str, Dict[str, Any]] = {}
    for issue in client.search_jql(jql, fields=["issuetype"]):
        it = issue["fields"]["issuetype"]
        if it["id"] not in seen:
            seen[it["id"]] = {
                "id": it["id"],
                "name": it["name"],
                "subtask": it.get("subtask", False),
                "hierarchyLevel": it.get("hierarchyLevel"),
            }
    return list(seen.values())


def extract_statuses_in_use(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    jql = f'project = "{key}"'
    seen: Dict[str, Dict[str, Any]] = {}
    for issue in client.search_jql(jql, fields=["status", "issuetype"]):
        st = issue["fields"]["status"]
        it = issue["fields"]["issuetype"]
        key_tuple = f"{it['name']}::{st['name']}"
        if key_tuple not in seen:
            seen[key_tuple] = {
                "issueType": it["name"],
                "statusName": st["name"],
                "statusId": st["id"],
                "statusCategory": (st.get("statusCategory") or {}).get("key"),
            }
    return list(seen.values())


def extract_versions(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    versions = client.get(f"/rest/api/3/project/{key}/versions") or []
    out = []
    for i, v in enumerate(versions):
        out.append({
            "sequence": i,
            "id": v["id"],
            "name": v["name"],
            "description": v.get("description", ""),
            "startDate": v.get("startDate", ""),
            "releaseDate": v.get("releaseDate", ""),
            "released": v.get("released", False),
            "archived": v.get("archived", False),
            "overdue": v.get("overdue", False),
        })
    return out


def extract_components(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    comps = client.get(f"/rest/api/3/project/{key}/components") or []
    out = []
    for c in comps:
        out.append({
            "id": c["id"],
            "name": c["name"],
            "description": c.get("description", ""),
            "leadAccountId": (c.get("lead") or {}).get("accountId", ""),
            "assigneeType": c.get("assigneeType", "PROJECT_DEFAULT"),
            "realAssigneeType": c.get("realAssigneeType", ""),
        })
    return out


def find_source_board(client: JiraClient, key: str) -> Optional[Dict[str, Any]]:
    boards = list(client.paged("/rest/agile/1.0/board",
                               params={"projectKeyOrId": key}, page_size=50))
    if not boards:
        return None
    # Prefer scrum board for sprint data
    scrum = [b for b in boards if b.get("type") == "scrum"]
    return (scrum or boards)[0]


def extract_sprints(client: JiraClient, board_id: int) -> List[Dict[str, Any]]:
    sprints = list(client.paged(f"/rest/agile/1.0/board/{board_id}/sprint",
                                params={"state": "closed,active,future"}, page_size=50))
    out = []
    # Sort: closed by completeDate asc, then active, then future
    def sort_key(s):
        state_order = {"closed": 0, "active": 1, "future": 2}
        return (state_order.get(s.get("state", "future"), 3),
                s.get("completeDate") or s.get("startDate") or "")
    for s in sorted(sprints, key=sort_key):
        out.append({
            "id": s["id"],
            "name": s["name"],
            "state": s["state"],
            "startDate": s.get("startDate", ""),
            "endDate": s.get("endDate", ""),
            "completeDate": s.get("completeDate", ""),
            "goal": s.get("goal", ""),
            "originBoardId": s.get("originBoardId"),
        })
    return out


def extract_board_config(client: JiraClient, board_id: int) -> Dict[str, Any]:
    return client.get(f"/rest/agile/1.0/board/{board_id}/configuration")


def extract_roles(client: JiraClient, key: str) -> Dict[str, Any]:
    role_map = client.get(f"/rest/api/3/project/{key}/role") or {}
    out: Dict[str, Any] = {}
    for role_name, role_url in role_map.items():
        role_detail = client.get(role_url)
        out[role_name] = {
            "id": role_detail.get("id"),
            "actors": [
                {
                    "displayName": a.get("displayName"),
                    "type": a.get("type"),
                    "accountId": (a.get("actorUser") or {}).get("accountId"),
                    "groupName": (a.get("actorGroup") or {}).get("name"),
                }
                for a in role_detail.get("actors", [])
            ],
        }
    return out


def extract_custom_fields_in_use(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    """Sample fields actually present on source issues. TMP-scoped fields
    typically appear with names but underlying IDs may be project-scoped."""
    jql = f'project = "{key}"'
    seen_fields: Dict[str, Dict[str, Any]] = {}
    field_catalog = {f["id"]: f for f in (client.get("/rest/api/3/field") or [])}

    sample_count = 0
    for issue in client.search_jql(jql, fields=["*all"], expand=["names"]):
        sample_count += 1
        names = issue.get("names", {}) or {}
        for fid, value in issue["fields"].items():
            if value in (None, "", [], {}):
                continue
            if fid in seen_fields:
                continue
            cat = field_catalog.get(fid, {})
            if cat.get("custom") is False:
                continue  # skip system fields
            if not fid.startswith("customfield_"):
                continue
            seen_fields[fid] = {
                "fieldId": fid,
                "displayName": names.get(fid) or cat.get("name", ""),
                "schemaType": (cat.get("schema") or {}).get("type", ""),
                "schemaCustom": (cat.get("schema") or {}).get("custom", ""),
                "exampleValue": str(value)[:200],
            }
        if sample_count >= 500:
            # 500-issue sample is plenty to identify all TMP fields in use
            break
    return list(seen_fields.values())


def extract_inventory_counts(client: JiraClient, key: str) -> Dict[str, Any]:
    total = client.approximate_count(f'project = "{key}"')

    def bucket(jql: str) -> int:
        return client.approximate_count(jql)

    return {
        "total": total,
        "byType": _bucketed(client, key, "issuetype"),
        "byStatus": _bucketed(client, key, "status"),
        "byResolution": _bucketed(client, key, "resolution"),
        "withAttachments": bucket(f'project = "{key}" AND attachments is not EMPTY'),
        "withComments": bucket(f'project = "{key}" AND comment is not EMPTY'),
        "withSubtasks": bucket(f'project = "{key}" AND issueFunction in hasSubtasks()')
            if False else None,  # issueFunction requires ScriptRunner; skip by default
        "withLinks": bucket(f'project = "{key}" AND issueLinkType is not EMPTY')
            if False else None,
        "withFixVersion": bucket(f'project = "{key}" AND fixVersion is not EMPTY'),
        "withComponents": bucket(f'project = "{key}" AND component is not EMPTY'),
        "withSprint": bucket(f'project = "{key}" AND sprint is not EMPTY'),
    }


def _bucketed(client: JiraClient, key: str, field_name: str) -> Dict[str, int]:
    """Get counts grouped by a field. We do this by scanning a single fetch
    of the field's distinct values rather than N approximate-count calls."""
    out: Dict[str, int] = {}
    for issue in client.search_jql(f'project = "{key}"', fields=[field_name]):
        val = issue["fields"].get(field_name)
        if isinstance(val, dict):
            name = val.get("name") or val.get("value") or "<unknown>"
        elif val is None:
            name = "<empty>"
        else:
            name = str(val)
        out[name] = out.get(name, 0) + 1
    return out


def cmd_extract(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    out = cfg.output_dir / "extract"
    out.mkdir(parents=True, exist_ok=True)

    logging.info("=== EXTRACT phase: source project %s ===", cfg.source_key)

    meta = extract_project_meta(client, cfg.source_key)
    write_json(out / "01_project_meta.json", meta)
    if meta.get("style") != "next-gen":
        logging.warning("Source project style is '%s' (not 'next-gen' / TMP). "
                        "Proceeding anyway.", meta.get("style"))

    types = extract_issue_types_in_use(client, cfg.source_key)
    write_json(out / "02_issue_types.json", types)
    write_csv(out / "02_issue_types.csv", types,
              ["id", "name", "subtask", "hierarchyLevel"])

    statuses = extract_statuses_in_use(client, cfg.source_key)
    write_json(out / "03_statuses.json", statuses)
    write_csv(out / "03_statuses.csv", statuses,
              ["issueType", "statusName", "statusId", "statusCategory"])

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
        board_cfg = extract_board_config(client, board["id"])
        write_json(out / "08_source_board_config.json", board_cfg)
    else:
        logging.warning("No board found on source project; skipping sprint extract.")

    roles = extract_roles(client, cfg.source_key)
    write_json(out / "09_roles.json", roles)

    fields = extract_custom_fields_in_use(client, cfg.source_key)
    write_json(out / "10_custom_fields_in_use.json", fields)
    write_csv(out / "10_custom_fields_in_use.csv", fields,
              ["fieldId", "displayName", "schemaType", "schemaCustom", "exampleValue"])

    inv = extract_inventory_counts(client, cfg.source_key)
    write_json(out / "11_inventory_baseline.json", inv)

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
            f"Field {field_id} ('{found.get('name')}') has schema.type='{schema_type}'. "
            "Must be a text (string) field."
        )
    logging.info("Key field OK: %s = '%s' (type=%s)",
                 field_id, found.get("name"), schema_type)
    return found


def stamp_test_on_first_issue(client: JiraClient, source_key: str,
                              field_id: str, dry_run: bool) -> str:
    """Stamp the first issue we find, then read it back. Aborts if not persisted."""
    first = next(client.search_jql(f'project = "{source_key}" ORDER BY created ASC',
                                    fields=["summary"]), None)
    if not first:
        raise SystemExit(f"No issues found in source project {source_key}.")
    issue_key = first["key"]
    logging.info("Test stamp on %s", issue_key)
    if dry_run:
        logging.info("[DRY-RUN] Would stamp %s into %s on %s",
                     issue_key, field_id, issue_key)
        return issue_key
    client.put(f"/rest/api/3/issue/{issue_key}",
               body={"fields": {field_id: issue_key}},
               params={"notifyUsers": "false"})
    # Read back
    check = client.get(f"/rest/api/3/issue/{issue_key}",
                       params={"fields": field_id})
    actual = check["fields"].get(field_id)
    if actual != issue_key:
        raise SystemExit(
            f"Test stamp did not persist on {issue_key}. "
            f"Expected '{issue_key}', got '{actual}'. "
            f"Confirm {field_id} is on the edit screen of all issue types in source."
        )
    logging.info("Test stamp verified on %s", issue_key)
    return issue_key


def cmd_stamp_keys(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    out = cfg.output_dir / "stamp"
    out.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out / "stamped.txt"
    failures_path = out / "stamp_failures.csv"

    logging.info("=== STAMP-KEYS phase: %s -> %s ===",
                 cfg.source_key, cfg.key_field_id)

    precheck_key_field(client, cfg.key_field_id)
    stamp_test_on_first_issue(client, cfg.source_key, cfg.key_field_id, cfg.dry_run)

    done: set = set()
    if checkpoint_path.exists():
        done = set(checkpoint_path.read_text(encoding="utf-8").splitlines())
        logging.info("Resuming: %d issues already stamped per checkpoint", len(done))

    total_jql = f'project = "{cfg.source_key}"'
    total = client.approximate_count(total_jql)
    logging.info("Approximate total issues: %d", total)

    if not cfg.dry_run and not confirm(
        f"About to stamp {cfg.key_field_id} on up to {total} issues in {cfg.source_key}. Continue?",
        cfg.dry_run,
    ):
        logging.info("Aborted by user.")
        return

    failures: List[Dict[str, str]] = []
    count = 0
    with checkpoint_path.open("a", encoding="utf-8") as ckpt:
        for issue in client.search_jql(total_jql, fields=["summary"]):
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
        return client.get(f"/rest/api/3/project/{key}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def create_target_project(client: JiraClient, cfg: Config,
                          source_meta: Dict[str, Any]) -> Dict[str, Any]:
    if not cfg.target_project_lead_account_id:
        raise SystemExit("--target-lead <accountId> required when --create-target is set.")
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


def build_versions(client: JiraClient, cfg: Config, target_project_id: str) -> List[Dict[str, Any]]:
    src_path = cfg.output_dir / "extract" / "04_versions.json"
    if not src_path.exists():
        logging.warning("No versions extract found; skipping.")
        return []
    versions = read_json(src_path)
    existing = {v["name"]: v for v in
                (client.get(f"/rest/api/3/project/{cfg.target_key}/versions") or [])}

    mapping: List[Dict[str, Any]] = []
    for v in versions:
        name = v["name"]
        if name in existing:
            target_v = existing[name]
            mapping.append({
                "source_id": v["id"], "source_name": name,
                "target_id": target_v["id"], "action": "existing",
            })
            logging.info("Version exists, skipping: %s", name)
            continue

        body = {"name": name, "projectId": int(target_project_id)}
        for opt in ("description", "startDate", "releaseDate"):
            if v.get(opt):
                body[opt] = v[opt]
        # Create as unreleased first; flip released/archived after to avoid
        # "cannot create archived" edge cases.
        if cfg.dry_run:
            logging.info("[DRY-RUN] Create version %s body=%s", name, body)
            mapping.append({
                "source_id": v["id"], "source_name": name,
                "target_id": "(dry-run)", "action": "would-create",
            })
            continue

        created = client.post("/rest/api/3/version", body=body)
        target_id = created["id"]
        # Apply released / archived flags post-create
        if v.get("released") or v.get("archived"):
            patch: Dict[str, Any] = {}
            if v.get("released"):
                patch["released"] = True
                if v.get("releaseDate"):
                    patch["releaseDate"] = v["releaseDate"]
            if v.get("archived"):
                patch["archived"] = True
            client.put(f"/rest/api/3/version/{target_id}", body=patch)

        mapping.append({
            "source_id": v["id"], "source_name": name,
            "target_id": target_id, "action": "created",
        })
        time.sleep(SLEEP_BETWEEN_WRITES)

    out_csv = cfg.output_dir / "build" / "version_id_mapping.csv"
    write_csv(out_csv, mapping,
              ["source_id", "source_name", "target_id", "action"])
    return mapping


def build_components(client: JiraClient, cfg: Config) -> List[Dict[str, Any]]:
    src_path = cfg.output_dir / "extract" / "05_components.json"
    if not src_path.exists():
        logging.warning("No components extract found; skipping.")
        return []
    components = read_json(src_path)
    existing = {c["name"]: c for c in
                (client.get(f"/rest/api/3/project/{cfg.target_key}/components") or [])}

    mapping: List[Dict[str, Any]] = []
    for c in components:
        name = c["name"]
        if name in existing:
            mapping.append({
                "source_id": c["id"], "source_name": name,
                "target_id": existing[name]["id"], "action": "existing",
            })
            logging.info("Component exists, skipping: %s", name)
            continue
        body: Dict[str, Any] = {
            "name": name,
            "project": cfg.target_key,
        }
        if c.get("description"):
            body["description"] = c["description"]
        if c.get("leadAccountId"):
            body["leadAccountId"] = c["leadAccountId"]
        if c.get("assigneeType"):
            body["assigneeType"] = c["assigneeType"]

        if cfg.dry_run:
            logging.info("[DRY-RUN] Create component %s body=%s", name, body)
            mapping.append({
                "source_id": c["id"], "source_name": name,
                "target_id": "(dry-run)", "action": "would-create",
            })
            continue

        created = client.post("/rest/api/3/component", body=body)
        mapping.append({
            "source_id": c["id"], "source_name": name,
            "target_id": created["id"], "action": "created",
        })
        time.sleep(SLEEP_BETWEEN_WRITES)

    write_csv(cfg.output_dir / "build" / "component_id_mapping.csv", mapping,
              ["source_id", "source_name", "target_id", "action"])
    return mapping


def build_sprints(client: JiraClient, cfg: Config) -> List[Dict[str, Any]]:
    if not cfg.target_board_id:
        logging.warning("--target-board-id not provided; skipping sprint creation. "
                        "Create a Scrum board on the target CMP first, then re-run "
                        "with --target-board-id <id>.")
        return []
    src_path = cfg.output_dir / "extract" / "07_sprints.json"
    if not src_path.exists():
        logging.warning("No sprints extract found; skipping.")
        return []
    sprints = read_json(src_path)

    # Existing sprints on target board (avoid duplicates)
    try:
        existing_iter = client.paged(
            f"/rest/agile/1.0/board/{cfg.target_board_id}/sprint",
            params={"state": "closed,active,future"}, page_size=50,
        )
        existing_by_name = {s["name"]: s for s in existing_iter}
    except requests.HTTPError:
        existing_by_name = {}

    mapping: List[Dict[str, Any]] = []
    # Create in order so closed sprints appear chronologically first
    for s in sprints:
        name = s["name"]
        if name in existing_by_name:
            mapping.append({
                "source_id": s["id"], "source_name": name,
                "source_state": s["state"],
                "target_id": existing_by_name[name]["id"],
                "target_state": existing_by_name[name]["state"],
                "action": "existing",
                "note": "completeDate cannot be backdated via API",
            })
            continue

        body: Dict[str, Any] = {
            "name": name,
            "originBoardId": cfg.target_board_id,
        }
        if s.get("startDate"):
            body["startDate"] = s["startDate"]
        if s.get("endDate"):
            body["endDate"] = s["endDate"]
        if s.get("goal"):
            body["goal"] = s["goal"]

        if cfg.dry_run:
            logging.info("[DRY-RUN] Create sprint %s (target state: %s) body=%s",
                         name, s["state"], body)
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

        # Transition to active/closed to match source state
        if s["state"] == "active":
            client.post(f"/rest/agile/1.0/sprint/{target_id}",
                        body={"state": "active"})
            target_state = "active"
        elif s["state"] == "closed":
            # Must activate first, then close
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
            "note": "completeDate set to now, not original" if s["state"] == "closed" else "",
        })
        time.sleep(SLEEP_BETWEEN_WRITES)

    write_csv(cfg.output_dir / "build" / "sprint_id_mapping.csv", mapping,
              ["source_id", "source_name", "source_state",
               "target_id", "target_state", "action", "note"])
    return mapping


def cmd_build(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    out = cfg.output_dir / "build"
    out.mkdir(parents=True, exist_ok=True)

    logging.info("=== BUILD phase: target %s ===", cfg.target_key)

    target = get_project_or_none(client, cfg.target_key)
    source_meta = read_json(cfg.output_dir / "extract" / "01_project_meta.json")

    if target is None:
        if not cfg.create_target:
            raise SystemExit(
                f"Target project {cfg.target_key} not found. "
                f"Create it via UI (recommended, to assign schemes), "
                f"or re-run with --create-target --target-lead <accountId>."
            )
        target = create_target_project(client, cfg, source_meta)
    else:
        logging.info("Target project %s exists (id=%s)", target["key"], target.get("id"))

    target_id = str(target["id"])

    build_versions(client, cfg, target_id)
    build_components(client, cfg)
    build_sprints(client, cfg)

    logging.info("Build complete. Mappings in %s", out)


# ---------------------------------------------------------------------------
# VALIDATE phase
# ---------------------------------------------------------------------------

def cmd_validate(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    out = cfg.output_dir / "validate"
    out.mkdir(parents=True, exist_ok=True)

    baseline_path = cfg.output_dir / "extract" / "11_inventory_baseline.json"
    if not baseline_path.exists():
        raise SystemExit("Baseline inventory not found. Run 'extract' first.")
    baseline = read_json(baseline_path)

    target_inv = extract_inventory_counts(client, cfg.target_key)
    write_json(out / "target_inventory.json", target_inv)

    delta = {
        "total": {"source": baseline["total"], "target": target_inv["total"],
                  "diff": target_inv["total"] - baseline["total"]},
        "byType": _dict_diff(baseline["byType"], target_inv["byType"]),
        "byStatus": _dict_diff(baseline["byStatus"], target_inv["byStatus"]),
        "byResolution": _dict_diff(baseline["byResolution"], target_inv["byResolution"]),
        "withAttachments": {"source": baseline["withAttachments"],
                            "target": target_inv["withAttachments"]},
        "withComments": {"source": baseline["withComments"],
                         "target": target_inv["withComments"]},
        "withFixVersion": {"source": baseline["withFixVersion"],
                           "target": target_inv["withFixVersion"]},
        "withComponents": {"source": baseline["withComponents"],
                           "target": target_inv["withComponents"]},
        "withSprint": {"source": baseline["withSprint"],
                       "target": target_inv["withSprint"]},
    }
    write_json(out / "validation_delta.json", delta)

    # Human-readable summary
    lines = ["category,source,target,diff"]
    for k in ("total", "withAttachments", "withComments",
              "withFixVersion", "withComponents", "withSprint"):
        s = delta[k].get("source", 0)
        t = delta[k].get("target", 0)
        lines.append(f"{k},{s},{t},{t - s}")
    (out / "validation_summary.csv").write_text("\n".join(lines), encoding="utf-8")
    logging.info("Validation complete. See %s", out)


def _dict_diff(src: Dict[str, int], tgt: Dict[str, int]) -> Dict[str, Dict[str, int]]:
    keys = set(src) | set(tgt)
    return {k: {"source": src.get(k, 0), "target": tgt.get(k, 0),
                "diff": tgt.get(k, 0) - src.get(k, 0)} for k in sorted(keys)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Jira TMP -> CMP migration toolkit")
    p.add_argument("--base-url", help="Jira Cloud base URL (e.g. https://acme.atlassian.net). "
                                       "Or set JIRA_BASE_URL env.")
    p.add_argument("--email", help="Atlassian account email. Or set JIRA_EMAIL env.")
    p.add_argument("--output-dir", default="./out", help="Artifacts output directory.")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="Extract source TMP data.")
    p_ext.add_argument("--source-key", required=True)

    p_stamp = sub.add_parser("stamp-keys", help="Stamp issue keys into a text custom field.")
    p_stamp.add_argument("--source-key", required=True)
    p_stamp.add_argument("--key-field", default="customfield_25199")
    p_stamp.add_argument("--dry-run", action="store_true")

    p_build = sub.add_parser("build", help="Recreate versions/components/sprints in target.")
    p_build.add_argument("--target-key", required=True)
    p_build.add_argument("--target-board-id", type=int,
                         help="Board ID on target CMP for sprint creation.")
    p_build.add_argument("--create-target", action="store_true",
                         help="Create the target project if it doesn't exist.")
    p_build.add_argument("--target-lead", help="Lead accountId (required with --create-target).")
    p_build.add_argument("--target-template",
                         default="com.pyxis.greenhopper.jira:gh-simplified-scrum-classic",
                         help="Project template when --create-target.")
    p_build.add_argument("--dry-run", action="store_true")

    p_val = sub.add_parser("validate", help="Compare baseline vs target inventory.")
    p_val.add_argument("--source-key", required=True,
                       help="(for label only; baseline is read from extract artifacts)")
    p_val.add_argument("--target-key", required=True)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir).resolve()
    setup_logging(output_dir, args.verbose)
    cfg = Config.from_args(args)
    try:
        if args.cmd == "extract":
            cmd_extract(cfg)
        elif args.cmd == "stamp-keys":
            cmd_stamp_keys(cfg)
        elif args.cmd == "build":
            cmd_build(cfg)
        elif args.cmd == "validate":
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
