#!/usr/bin/env python3
"""
Jira TMP -> CMP Migration Toolkit (Enhanced with full field catalog using /editmeta)

Subcommands:
  doctor         Verify auth + project access.
  extract        Pull source TMP config + full field catalog (with options).
  stamp-keys     Write source keys into a custom text field.
  copy-fields    Copy field values from source to target using mapping CSV.
  build          Recreate versions/components/sprints in target.
  update-sprints Replace source sprint IDs with target sprint IDs after bulk move.
  export-sprint-csv  Emit a semicolon-delimited CSV for Jira's External Import
                 feature to re-attach sprint membership by sprint id (the
                 reliable fallback when closed sprints block update-sprints).
  validate       Compare baseline counts after move.

Uses /rest/api/3/issue/{key}/editmeta to retrieve allowed values for select fields.

Note on Jira Cloud's /rest/api/3/search/jql endpoint:
  - It does NOT hydrate any fields by default; only `id` (and `self`/`key`) come back
    unless `fields` is explicitly requested in the POST body.
  - The literal field name "sprint" is a JQL alias, not a real field id. When you
    need the sprint values on an issue, you must request the actual custom field
    id (e.g. customfield_10020). The toolkit discovers this id at runtime via
    /rest/api/3/field and caches it on the client.
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

# Load .env if python-dotenv available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 30
PAGE_SIZE_SEARCH = 100
PAGE_SIZE_GENERIC = 50
SLEEP_BETWEEN_WRITES = 0.10
SEP = "=" * 70

# Schema key Atlassian uses for the Sprint custom field
SPRINT_SCHEMA_CUSTOM = "com.pyxis.greenhopper.jira:gh-sprint"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(output_dir: Path, verbose: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "migration.log"
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

# ---------------------------------------------------------------------------
# Config
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
    target_lead_account_id: Optional[str] = None
    target_template: str = "com.pyxis.greenhopper.jira:gh-simplified-scrum-classic"

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        base = (getattr(args, "base_url", "") or os.environ.get("JIRA_BASE_URL", "")).rstrip("/")
        email = getattr(args, "email", "") or os.environ.get("JIRA_EMAIL", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        missing = []
        if not base: missing.append("JIRA_BASE_URL")
        if not email: missing.append("JIRA_EMAIL")
        if not token: missing.append("JIRA_API_TOKEN")
        if missing:
            raise SystemExit(f"\nMissing credentials: {', '.join(missing)}\n")
        output_dir = Path(getattr(args, "output_dir", None) or os.environ.get("JIRA_OUTPUT_DIR", "./out")).resolve()
        source_key = getattr(args, "source_key", "") or os.environ.get("JIRA_SOURCE_KEY", "")
        target_key = getattr(args, "target_key", "") or os.environ.get("JIRA_TARGET_KEY", "")
        key_field_id = getattr(args, "key_field", None) or os.environ.get("JIRA_KEY_FIELD", "customfield_25199")
        return cls(
            base_url=base, email=email, api_token=token,
            source_key=source_key, target_key=target_key,
            key_field_id=key_field_id, output_dir=output_dir,
            dry_run=getattr(args, "dry_run", False),
            create_target=getattr(args, "create_target", False),
            target_board_id=getattr(args, "target_board_id", None) or _to_int_or_none(os.environ.get("JIRA_TARGET_BOARD_ID")),
            target_lead_account_id=getattr(args, "target_lead", None) or os.environ.get("JIRA_TARGET_LEAD"),
            target_template=getattr(args, "target_template", None) or os.environ.get("JIRA_TARGET_TEMPLATE", "com.pyxis.greenhopper.jira:gh-simplified-scrum-classic"),
        )

def _to_int_or_none(v: Optional[str]) -> Optional[int]:
    try:
        return int(v) if v else None
    except (ValueError, TypeError):
        return None

# ---------------------------------------------------------------------------
# Jira Client (enhanced with editmeta and field endpoints)
# ---------------------------------------------------------------------------
class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url
        creds = f"{email}:{api_token}".encode("utf-8")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": "Basic " + base64.b64encode(creds).decode("ascii"),
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        retry = Retry(total=6, backoff_factor=1.5, status_forcelist=[429,500,502,503,504],
                      allowed_methods=["GET","POST","PUT","DELETE"], respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        # Caches
        self._all_fields_cache: Optional[List[Dict]] = None
        self._sprint_field_id: Optional[str] = None

    def _req(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            logging.error("%s %s -> %s: %s", method, url, resp.status_code, (resp.text or "")[:500])
            resp.raise_for_status()
        return resp

    def get(self, path: str, params: Optional[Dict] = None) -> Any:
        r = self._req("GET", path, params=params)
        return r.json() if r.content else None

    def post(self, path: str, body: Optional[Dict] = None, params: Optional[Dict] = None) -> Any:
        r = self._req("POST", path, json=body or {}, params=params)
        return r.json() if r.content else None

    def put(self, path: str, body: Optional[Dict] = None, params: Optional[Dict] = None) -> Any:
        r = self._req("PUT", path, json=body or {}, params=params)
        return r.json() if r.content else None

    def myself(self) -> Dict:
        return self.get("/rest/api/3/myself")

    def project(self, key: str) -> Dict:
        return self.get(f"/rest/api/3/project/{key}", params={"expand": "description,lead"})

    def all_fields(self) -> List[Dict]:
        if self._all_fields_cache is None:
            self._all_fields_cache = self.get("/rest/api/3/field") or []
        return self._all_fields_cache

    def sprint_field_id(self) -> str:
        """Resolve the Sprint custom field id (e.g. customfield_10020) at runtime.

        The new /search/jql endpoint and the issue PUT payload both require the
        real custom-field id; the literal token "sprint" is only a JQL alias.
        """
        if self._sprint_field_id:
            return self._sprint_field_id
        for f in self.all_fields():
            schema = f.get("schema") or {}
            if schema.get("custom") == SPRINT_SCHEMA_CUSTOM:
                self._sprint_field_id = f["id"]
                logging.debug("Resolved sprint field id: %s", self._sprint_field_id)
                return self._sprint_field_id
        raise SystemExit(
            "Could not resolve Sprint custom field id from /rest/api/3/field. "
            "Make sure the Jira Software (Agile) feature is enabled on this instance."
        )

    def editmeta(self, issue_key: str) -> Dict:
        """Get edit metadata for an issue, including allowedValues for fields."""
        return self.get(f"/rest/api/3/issue/{issue_key}/editmeta") or {}

    def sprint(self, sprint_id: int) -> Dict:
        """Fetch a single sprint by id (includes state, dates, goal)."""
        return self.get(f"/rest/agile/1.0/sprint/{sprint_id}") or {}

    def transition_sprint(self, sprint_id: int, target_state: str, verify: bool = True) -> Dict:
        """Transition a sprint to `target_state` ('active' or 'closed').

        Atlassian rules (Cloud):
          - future -> active : requires startDate + endDate set on the sprint.
          - active -> closed : sets completeDate to *now* (cannot be set manually,
            so re-closing a previously-closed sprint will overwrite its original
            completion timestamp — there is no way around this).
          - Only one sprint per board may be 'active' unless the board has
            parallel sprints enabled. Reopening a closed sprint while another
            sprint is already active will fail with 400.

        Uses POST /rest/agile/1.0/sprint/{id} (partial update).
        """
        if target_state not in ("active", "closed", "future"):
            raise ValueError(f"Invalid target_state: {target_state}")
        result = self.post(f"/rest/agile/1.0/sprint/{sprint_id}", body={"state": target_state})
        if verify:
            current = self.sprint(sprint_id).get("state")
            if current != target_state:
                raise RuntimeError(
                    f"Sprint {sprint_id} did not transition to '{target_state}' "
                    f"(current state: '{current}')"
                )
        return result or {}

    def issue(self, issue_key: str, fields: Optional[List[str]] = None) -> Dict:
        params = {"fields": ",".join(fields)} if fields else {}
        return self.get(f"/rest/api/3/issue/{issue_key}", params=params)

    def paged(self, path: str, params: Optional[Dict] = None, values_key: str = "values", page_size: int = 50) -> Iterable[Dict]:
        params = dict(params or {})
        params.setdefault("maxResults", page_size)
        start = 0
        while True:
            params["startAt"] = start
            payload = self.get(path, params=params) or {}
            items = payload.get(values_key, payload if isinstance(payload, list) else [])
            for item in items:
                yield item
            if not items or payload.get("isLast") or (payload.get("total") is not None and start + len(items) >= payload["total"]):
                break
            start += len(items)

    def search_jql(self, jql: str, fields: List[str], max_results: int = PAGE_SIZE_SEARCH) -> Iterable[Dict]:
        """Paginate using POST /rest/api/3/search/jql (supports nextPageToken).

        IMPORTANT: The new endpoint does NOT hydrate any fields by default — it
        returns only id (and self/key). The `fields` parameter MUST be passed
        in the JSON body for any field data to come back. To shield callers
        from the resulting "no `fields` key on the issue" surprise, we always
        normalize each issue dict to include a `fields` mapping (empty if the
        server returned nothing).
        """
        body = {"jql": jql, "fields": fields, "maxResults": max_results, "fieldsByKeys": False}
        token = None
        while True:
            if token:
                body["nextPageToken"] = token
            payload = self.post("/rest/api/3/search/jql", body=body) or {}
            for issue in payload.get("issues", []):
                # Defensive normalization: the new endpoint may omit `fields`
                # entirely (e.g. when the only requested field is unknown, or
                # when the issue has no values for the requested fields).
                if "fields" not in issue or issue["fields"] is None:
                    issue["fields"] = {}
                yield issue
            if payload.get("isLast") or not payload.get("nextPageToken"):
                break
            token = payload["nextPageToken"]

    def approximate_count(self, jql: str) -> int:
        payload = self.post("/rest/api/3/search/approximate-count", body={"jql": jql})
        return int((payload or {}).get("count", 0))

# ---------------------------------------------------------------------------
# Project Inspector: uses editmeta to extract field options per issue type
# ---------------------------------------------------------------------------
class ProjectInspector:
    def __init__(self, client: JiraClient, project_key: str):
        self.client = client
        self.project_key = project_key
        self.project_id = None
        self._load_project()

    def _load_project(self):
        proj = self.client.project(self.project_key)
        self.project_id = proj["id"]

    def get_project_scoped_fields(self) -> List[Dict[str, Any]]:
        """Return all fields where scope.project.id == self.project_id."""
        scoped = []
        for f in self.client.all_fields():
            scope = f.get("scope")
            if scope and scope.get("type") == "PROJECT" and str(scope.get("project", {}).get("id")) == str(self.project_id):
                scoped.append(f)
        return scoped

    def get_issue_types(self) -> List[Dict[str, Any]]:
        """Retrieve issue types used in the project via a sample issue search."""
        types_seen = {}
        for issue in self.client.search_jql(f'project = "{self.project_key}"', fields=["issuetype"], max_results=100):
            it = issue["fields"].get("issuetype")
            if not it:
                continue
            types_seen[it["id"]] = {"id": it["id"], "name": it["name"], "subtask": it.get("subtask", False)}
        return list(types_seen.values())

    def get_sample_issue_for_type(self, issue_type_id: str) -> Optional[str]:
        """Return the key of any issue of given type in the project."""
        for issue in self.client.search_jql(f'project = "{self.project_key}" AND issuetype = {issue_type_id}', fields=["summary"], max_results=1):
            return issue["key"]
        return None

    def build_field_catalog(self) -> Dict[str, Dict[str, Any]]:
        """Build catalog of all fields scoped to project, with allowedValues from editmeta."""
        scoped_fields = self.get_project_scoped_fields()
        catalog = {}
        for f in scoped_fields:
            fid = f["id"]
            catalog[fid] = {
                "fieldId": fid,
                "name": f.get("name"),
                "schemaType": f.get("schema", {}).get("type"),
                "custom": f.get("custom", False),
                "required": f.get("required", False),
                "allowedValues": [],
            }

        issue_types = self.get_issue_types()
        for it in issue_types:
            sample_key = self.get_sample_issue_for_type(it["id"])
            if not sample_key:
                logging.warning("No sample issue found for type %s", it["name"])
                continue
            try:
                editmeta = self.client.editmeta(sample_key)
                fields_meta = editmeta.get("fields", {})
                for fid, meta in fields_meta.items():
                    if fid in catalog:
                        allowed = meta.get("allowedValues")
                        if allowed and isinstance(allowed, list):
                            existing = catalog[fid]["allowedValues"]
                            existing_values = {str(v.get("value", v)) for v in existing}
                            for v in allowed:
                                val_str = str(v.get("value", v))
                                if val_str not in existing_values:
                                    existing.append(v)
                                    existing_values.add(val_str)
            except Exception as e:
                logging.warning("Failed to get editmeta for %s: %s", sample_key, e)

        return catalog

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def preflight(client: JiraClient, source_key: str = "", target_key: str = "",
              source_must_exist: bool = True, target_must_exist: bool = False) -> Dict[str, Any]:
    logging.info(SEP)
    logging.info("Preflight")
    logging.info(SEP)
    try:
        me = client.myself()
    except Exception as e:
        raise SystemExit(f"Authentication failed: {e}")
    logging.info("Authenticated: %s <%s>", me.get("displayName"), me.get("emailAddress"))
    if source_key:
        _check_project(client, source_key, "source", source_must_exist)
    if target_key:
        _check_project(client, target_key, "target", target_must_exist)
    logging.info(SEP)
    return me

def _check_project(client: JiraClient, key: str, label: str, must_exist: bool) -> Optional[Dict]:
    try:
        proj = client.project(key)
    except requests.HTTPError as e:
        if e.response and e.response.status_code == 404 and not must_exist:
            logging.info("%s project '%s' does not exist (OK)", label.capitalize(), key)
            return None
        raise
    logging.info("%s project '%s' OK (name=%s, style=%s)", label.capitalize(), key, proj.get("name"), proj.get("style"))
    return proj

# ---------------------------------------------------------------------------
# EXTRACT phase (enhanced with ProjectInspector)
# ---------------------------------------------------------------------------
def extract_project_meta(client: JiraClient, key: str) -> Dict:
    proj = client.project(key)
    return {
        "id": proj["id"], "key": proj["key"], "name": proj["name"],
        "projectTypeKey": proj.get("projectTypeKey"), "style": proj.get("style"),
        "description": proj.get("description"),
        "leadAccountId": (proj.get("lead") or {}).get("accountId"),
        "assigneeType": proj.get("assigneeType"),
    }

def extract_issues_aggregate(client: JiraClient, key: str) -> Dict:
    types = {}
    type_status_pairs = {}
    by_type = {}
    by_status = {}
    by_resolution = {}
    total = 0
    fields = ["issuetype", "status", "resolution"]
    for issue in client.search_jql(f'project = "{key}"', fields=fields):
        total += 1
        f = issue["fields"]
        it = f.get("issuetype")
        st = f.get("status")
        if not it or not st:
            continue
        if it["id"] not in types:
            types[it["id"]] = {"id": it["id"], "name": it["name"], "subtask": it.get("subtask", False)}
        pair = f"{it['name']}::{st['name']}"
        if pair not in type_status_pairs:
            type_status_pairs[pair] = {"issueType": it["name"], "statusName": st["name"], "statusId": st["id"]}
        by_type[it["name"]] = by_type.get(it["name"], 0) + 1
        by_status[st["name"]] = by_status.get(st["name"], 0) + 1
        res = f.get("resolution")
        res_name = res["name"] if res else "<unresolved>"
        by_resolution[res_name] = by_resolution.get(res_name, 0) + 1
        if total % 500 == 0:
            logging.info("  scanned %d issues...", total)
    return {
        "total": total,
        "types": list(types.values()),
        "statuses": list(type_status_pairs.values()),
        "byType": by_type, "byStatus": by_status, "byResolution": by_resolution,
    }

def extract_versions(client: JiraClient, key: str) -> List[Dict]:
    return [{"id": v["id"], "name": v["name"], "description": v.get("description", ""),
             "startDate": v.get("startDate", ""), "releaseDate": v.get("releaseDate", ""),
             "released": v.get("released", False), "archived": v.get("archived", False)}
            for v in (client.get(f"/rest/api/3/project/{key}/versions") or [])]

def extract_components(client: JiraClient, key: str) -> List[Dict]:
    return [{"id": c["id"], "name": c["name"], "description": c.get("description", ""),
             "leadAccountId": (c.get("lead") or {}).get("accountId", "")}
            for c in (client.get(f"/rest/api/3/project/{key}/components") or [])]

def find_board(client: JiraClient, key: str) -> Optional[Dict]:
    boards = list(client.paged("/rest/agile/1.0/board", params={"projectKeyOrId": key}, page_size=50))
    scrum = [b for b in boards if b.get("type") == "scrum"]
    return (scrum or boards)[0] if boards else None

def extract_sprints(client: JiraClient, board_id: int) -> List[Dict]:
    raw = list(client.paged(f"/rest/agile/1.0/board/{board_id}/sprint", params={"state": "closed,active,future"}, page_size=50))
    return [{"id": s["id"], "name": s["name"], "state": s["state"],
             "startDate": s.get("startDate", ""), "endDate": s.get("endDate", ""),
             "goal": s.get("goal", "")} for s in raw]

def extract_boolean_buckets(client: JiraClient, key: str) -> Dict[str, int]:
    base = f'project = "{key}"'
    return {
        "withAttachments": client.approximate_count(f"{base} AND attachments is not EMPTY"),
        "withComments": client.approximate_count(f"{base} AND comment is not EMPTY"),
        "withFixVersion": client.approximate_count(f"{base} AND fixVersion is not EMPTY"),
        "withComponents": client.approximate_count(f"{base} AND component is not EMPTY"),
        "withSprint": client.approximate_count(f"{base} AND sprint is not EMPTY"),
    }

def cmd_extract(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, source_key=cfg.source_key, source_must_exist=True)
    out = cfg.output_dir / "extract"
    out.mkdir(parents=True, exist_ok=True)
    logging.info("EXTRACT: source=%s", cfg.source_key)

    write_json(out / "01_project_meta.json", extract_project_meta(client, cfg.source_key))

    agg = extract_issues_aggregate(client, cfg.source_key)
    write_json(out / "02_issue_types.json", agg["types"])
    write_json(out / "03_statuses.json", agg["statuses"])

    write_json(out / "04_versions.json", extract_versions(client, cfg.source_key))
    write_json(out / "05_components.json", extract_components(client, cfg.source_key))

    board = find_board(client, cfg.source_key)
    if board:
        write_json(out / "06_source_board.json", board)
        write_json(out / "07_sprints.json", extract_sprints(client, board["id"]))
        write_json(out / "08_source_board_config.json", client.get(f"/rest/agile/1.0/board/{board['id']}/configuration"))

    role_map = client.get(f"/rest/api/3/project/{cfg.source_key}/role") or {}
    write_json(out / "09_roles.json", role_map)

    buckets = extract_boolean_buckets(client, cfg.source_key)
    baseline = {
        "projectKey": cfg.source_key,
        "extractedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": agg["total"],
        "byType": agg["byType"],
        "byStatus": agg["byStatus"],
        "byResolution": agg["byResolution"],
        **buckets,
    }
    write_json(out / "11_inventory_baseline.json", baseline)

    logging.info("Building field catalog (scoped fields + editmeta options)...")
    inspector = ProjectInspector(client, cfg.source_key)
    catalog = inspector.build_field_catalog()
    write_json(out / "12_source_field_catalog.json", catalog)
    csv_rows = [{
        "fieldId": fid,
        "name": info["name"],
        "schemaType": info["schemaType"],
        "custom": info["custom"],
        "required": info["required"],
        "allowedValues": json.dumps(info["allowedValues"]) if info["allowedValues"] else "",
    } for fid, info in catalog.items()]
    write_csv(out / "12_source_field_catalog.csv", csv_rows, ["fieldId", "name", "schemaType", "custom", "required", "allowedValues"])

    logging.info("EXTRACT complete. Total issues: %d. Catalog has %d fields.", agg["total"], len(catalog))

# ---------------------------------------------------------------------------
# Helper IO
# ---------------------------------------------------------------------------
def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logging.info("Wrote %s", path)

def write_csv(path: Path, rows: List[Dict], fieldnames: List[str], delimiter: str = ",") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)
    logging.info("Wrote %s (%d rows)", path, len(rows))

def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file. If `default` is provided, return it when the file is
    missing instead of raising."""
    if default is not None and not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))

def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read a CSV with DictReader. Returns [] if the file doesn't exist."""
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))

def load_sprint_id_map(path: Path) -> Dict[str, str]:
    """Load `build/sprint_id_mapping.csv` as {source_id: target_id}, filtered
    to rows whose `action` is `created` or `existing`. Used by update-sprints,
    validate, and report.
    """
    return {
        row["source_id"]: row["target_id"]
        for row in read_csv_rows(path)
        if row.get("action") in ("created", "existing")
    }

def load_field_mappings(path: Path) -> List[Dict[str, str]]:
    """Load `mapping/field_mapping.csv` keeping only rows that have both
    source_field_id and target_field_id populated."""
    return [
        row for row in read_csv_rows(path)
        if row.get("source_field_id") and row.get("target_field_id")
    ]

def confirm(prompt: str, dry_run: bool) -> bool:
    if dry_run:
        logging.info("[DRY-RUN] %s", prompt)
        return False
    return input(prompt + " [y/N]: ").strip().lower() == "y"

# ---------------------------------------------------------------------------
# STAMP-KEYS
# ---------------------------------------------------------------------------
def precheck_key_field(client: JiraClient, field_id: str) -> None:
    found = next((f for f in client.all_fields() if f["id"] == field_id), None)
    if not found:
        raise SystemExit(f"Key field {field_id} not found.")
    schema_type = found.get("schema", {}).get("type")
    if schema_type not in ("string", "any"):
        raise SystemExit(f"Key field {field_id} must be a text field (type string/any).")

def stamp_test_first_issue(client: JiraClient, source_key: str, field_id: str, dry_run: bool) -> str:
    first = next(client.search_jql(f'project = "{source_key}" ORDER BY created ASC', fields=["summary"]), None)
    if not first:
        raise SystemExit(f"No issues in {source_key}")
    issue_key = first["key"]
    logging.info("Test stamp on %s", issue_key)
    if dry_run:
        return issue_key
    client.put(f"/rest/api/3/issue/{issue_key}", body={"fields": {field_id: issue_key}}, params={"notifyUsers": "false"})
    check = client.get(f"/rest/api/3/issue/{issue_key}", params={"fields": field_id})
    if check.get("fields", {}).get(field_id) != issue_key:
        raise SystemExit(f"Test stamp failed: field not editable on issue type.")
    logging.info("Test stamp verified.")
    return issue_key

def cmd_stamp_keys(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, source_key=cfg.source_key, source_must_exist=True)
    out = cfg.output_dir / "stamp"
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / "stamped.txt"
    logging.info("STAMP-KEYS: %s -> %s", cfg.source_key, cfg.key_field_id)
    precheck_key_field(client, cfg.key_field_id)
    stamp_test_first_issue(client, cfg.source_key, cfg.key_field_id, cfg.dry_run)

    done = set(checkpoint.read_text().splitlines()) if checkpoint.exists() else set()
    total = client.approximate_count(f'project = "{cfg.source_key}"')
    if not cfg.dry_run and not confirm(f"Stamp ~{total} issues? Continue?", cfg.dry_run):
        return

    failures = []
    processed = 0
    with checkpoint.open("a") as ckpt:
        for issue in client.search_jql(f'project = "{cfg.source_key}"', fields=["summary"]):
            key = issue["key"]
            if key in done:
                continue
            processed += 1
            try:
                if not cfg.dry_run:
                    client.put(f"/rest/api/3/issue/{key}", body={"fields": {cfg.key_field_id: key}}, params={"notifyUsers": "false"})
                    ckpt.write(key + "\n")
                    ckpt.flush()
                    time.sleep(SLEEP_BETWEEN_WRITES)
                if processed % 100 == 0:
                    logging.info("Stamped %d / ~%d", processed, total)
            except Exception as e:
                failures.append({"key": key, "error": str(e)[:300]})
                logging.warning("Failed to stamp %s: %s", key, e)
    if failures:
        write_csv(out / "failures.csv", failures, ["key", "error"])
    logging.info("STAMP-KEYS complete. Processed %d issues.", processed)

# ---------------------------------------------------------------------------
# COPY-FIELDS
# ---------------------------------------------------------------------------
def cmd_copy_fields(cfg: Config) -> None:
    mapping_file = cfg.output_dir / "mapping" / "field_mapping.csv"
    if not mapping_file.exists():
        raise SystemExit(f"Mapping file not found: {mapping_file}\nCreate CSV with columns: source_field_id,target_field_id,transform")
    mappings = load_field_mappings(mapping_file)

    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, source_key=cfg.source_key, target_key=cfg.target_key,
              source_must_exist=True, target_must_exist=True)

    out = cfg.output_dir / "copy_fields"
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / "copied.txt"
    done = set(checkpoint.read_text().splitlines()) if checkpoint.exists() else set()

    total = client.approximate_count(f'project = "{cfg.target_key}" AND "{cfg.key_field_id}" IS NOT EMPTY')
    logging.info("Approx %d target issues with stamped source key", total)

    failures = []
    processed = 0
    src_field_ids = [m["source_field_id"] for m in mappings]
    for issue in client.search_jql(
        f'project = "{cfg.target_key}" AND "{cfg.key_field_id}" IS NOT EMPTY',
        fields=[cfg.key_field_id],
    ):
        target_key = issue["key"]
        if target_key in done:
            continue
        stamped = issue["fields"].get(cfg.key_field_id)
        if not stamped:
            logging.warning("No stamped source key on %s, skipping", target_key)
            continue
        try:
            source_issue = client.issue(stamped, fields=src_field_ids)
        except Exception:
            failures.append({"target": target_key, "source": stamped, "error": "Source not found"})
            continue

        update = {}
        for m in mappings:
            src_val = source_issue.get("fields", {}).get(m["source_field_id"])
            if src_val is None:
                continue
            transform = m.get("transform", "copy").lower()
            if transform == "append_prefix":
                update[m["target_field_id"]] = f"Original: {src_val}"
            elif transform == "append_suffix":
                update[m["target_field_id"]] = f"{src_val} (migrated)"
            else:
                update[m["target_field_id"]] = src_val
        if not update:
            continue
        processed += 1
        try:
            if not cfg.dry_run:
                client.put(f"/rest/api/3/issue/{target_key}", body={"fields": update}, params={"notifyUsers": "false"})
                with checkpoint.open("a") as ckpt:
                    ckpt.write(target_key + "\n")
                time.sleep(SLEEP_BETWEEN_WRITES)
            if processed % 50 == 0:
                logging.info("Copied fields for %d / ~%d", processed, total)
        except Exception as e:
            failures.append({"target": target_key, "source": stamped, "error": str(e)[:300]})
    if failures:
        write_csv(out / "failures.csv", failures, ["target", "source", "error"])
    logging.info("COPY-FIELDS complete. Updated %d issues.", processed)

# ---------------------------------------------------------------------------
# BUILD
# ---------------------------------------------------------------------------
def get_project_or_none(client: JiraClient, key: str) -> Optional[Dict]:
    try:
        return client.project(key)
    except requests.HTTPError as e:
        if e.response and e.response.status_code == 404:
            return None
        raise

def create_target_project(client: JiraClient, cfg: Config, source_meta: Dict) -> Dict:
    if not cfg.target_lead_account_id:
        raise SystemExit("--target-lead required")
    body = {
        "key": cfg.target_key,
        "name": source_meta["name"] + " (CMP)",
        "projectTypeKey": "software",
        "projectTemplateKey": cfg.target_template,
        "leadAccountId": cfg.target_lead_account_id,
        "description": source_meta.get("description", ""),
    }
    if cfg.dry_run:
        return {"id": "0", "key": cfg.target_key}
    return client.post("/rest/api/3/project", body=body)

def build_versions(client: JiraClient, cfg: Config, target_id: str) -> None:
    src = cfg.output_dir / "extract" / "04_versions.json"
    if not src.exists():
        return
    versions = read_json(src)
    existing = {v["name"]: v for v in (client.get(f"/rest/api/3/project/{cfg.target_key}/versions") or [])}
    mapping = []
    for v in versions:
        name = v["name"]
        if name in existing:
            mapping.append({"source_id": v["id"], "source_name": name, "target_id": existing[name]["id"], "action": "existing"})
            continue
        body = {"name": name, "projectId": int(target_id)}
        for opt in ("description", "startDate", "releaseDate"):
            if v.get(opt):
                body[opt] = v[opt]
        if cfg.dry_run:
            mapping.append({"source_id": v["id"], "source_name": name, "target_id": "(dry-run)", "action": "would-create"})
            continue
        created = client.post("/rest/api/3/version", body=body)
        if v.get("released") or v.get("archived"):
            patch = {}
            if v.get("released"): patch["released"] = True
            if v.get("archived"): patch["archived"] = True
            client.put(f"/rest/api/3/version/{created['id']}", body=patch)
        mapping.append({"source_id": v["id"], "source_name": name, "target_id": created["id"], "action": "created"})
        time.sleep(SLEEP_BETWEEN_WRITES)
    write_csv(cfg.output_dir / "build" / "version_id_mapping.csv", mapping, ["source_id", "source_name", "target_id", "action"])

def build_components(client: JiraClient, cfg: Config) -> None:
    src = cfg.output_dir / "extract" / "05_components.json"
    if not src.exists():
        return
    comps = read_json(src)
    existing = {c["name"]: c for c in (client.get(f"/rest/api/3/project/{cfg.target_key}/components") or [])}
    mapping = []
    for c in comps:
        name = c["name"]
        if name in existing:
            mapping.append({"source_id": c["id"], "source_name": name, "target_id": existing[name]["id"], "action": "existing"})
            continue
        body = {"name": name, "project": cfg.target_key}
        if c.get("description"): body["description"] = c["description"]
        if c.get("leadAccountId"): body["leadAccountId"] = c["leadAccountId"]
        if cfg.dry_run:
            mapping.append({"source_id": c["id"], "source_name": name, "target_id": "(dry-run)", "action": "would-create"})
            continue
        created = client.post("/rest/api/3/component", body=body)
        mapping.append({"source_id": c["id"], "source_name": name, "target_id": created["id"], "action": "created"})
        time.sleep(SLEEP_BETWEEN_WRITES)
    write_csv(cfg.output_dir / "build" / "component_id_mapping.csv", mapping, ["source_id", "source_name", "target_id", "action"])

def build_sprints(client: JiraClient, cfg: Config) -> None:
    if not cfg.target_board_id:
        logging.warning("No target board id, skipping sprints")
        return
    src = cfg.output_dir / "extract" / "07_sprints.json"
    if not src.exists():
        return
    sprints = read_json(src)
    existing = {s["name"]: s for s in client.paged(f"/rest/agile/1.0/board/{cfg.target_board_id}/sprint", params={"state": "closed,active,future"})}
    mapping = []
    for s in sprints:
        name = s["name"]
        if name in existing:
            mapping.append({"source_id": s["id"], "source_name": name, "source_state": s["state"],
                            "target_id": existing[name]["id"], "target_state": existing[name]["state"], "action": "existing"})
            continue
        body = {"name": name, "originBoardId": cfg.target_board_id}
        for opt in ("startDate", "endDate", "goal"):
            if s.get(opt): body[opt] = s[opt]
        if cfg.dry_run:
            mapping.append({"source_id": s["id"], "source_name": name, "source_state": s["state"],
                            "target_id": "(dry-run)", "target_state": "future", "action": "would-create"})
            continue
        created = client.post("/rest/agile/1.0/sprint", body=body)
        target_id = created["id"]
        target_state = "future"
        if s["state"] == "active":
            client.transition_sprint(target_id, "active")
            target_state = "active"
        elif s["state"] == "closed":
            client.transition_sprint(target_id, "active")
            client.transition_sprint(target_id, "closed")
            target_state = "closed"
        mapping.append({"source_id": s["id"], "source_name": name, "source_state": s["state"],
                        "target_id": target_id, "target_state": target_state, "action": "created"})
        time.sleep(SLEEP_BETWEEN_WRITES)
    write_csv(cfg.output_dir / "build" / "sprint_id_mapping.csv", mapping,
              ["source_id", "source_name", "source_state", "target_id", "target_state", "action"])

def cmd_build(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key, target_must_exist=not cfg.create_target)
    out = cfg.output_dir / "build"
    out.mkdir(parents=True, exist_ok=True)
    target = get_project_or_none(client, cfg.target_key)
    source_meta = read_json(cfg.output_dir / "extract" / "01_project_meta.json")
    if target is None:
        if not cfg.create_target:
            raise SystemExit(f"Target {cfg.target_key} not found. Use --create-target.")
        target = create_target_project(client, cfg, source_meta)
    build_versions(client, cfg, str(target["id"]))
    build_components(client, cfg)
    build_sprints(client, cfg)
    logging.info("BUILD complete. Mappings in %s", out)

# ---------------------------------------------------------------------------
# UPDATE SPRINTS
# ---------------------------------------------------------------------------
def _build_sprint_update_payload(sprint_field_id: str, target_sprint_ids: List[int]) -> Dict:
    """Build the editIssue payload for the Sprint custom field.

    Quirk: although the Sprint field's schema is `type: array`, the editIssue
    endpoint's `fields` shortcut rejects an array with "Number value expected
    as the Sprint id." — it wants a bare number for a single sprint.
    For multiple sprints, the only accepted shape is the `update` operations
    syntax with `set`.
    """
    if len(target_sprint_ids) == 1:
        return {"fields": {sprint_field_id: target_sprint_ids[0]}}
    return {"update": {sprint_field_id: [{"set": target_sprint_ids}]}}


def _plan_sprint_updates(
    client: JiraClient,
    target_key: str,
    sprint_field_id: str,
    sprint_map: Dict[str, str],
    done: set,
) -> Dict[str, Any]:
    """Scan target project once; produce an execution plan.

    Returns:
      {
        "issues": { issue_key: [target_sprint_id_int, ...] },  # ordered, deduped
        "open_issues":     [issue_key, ...],  # all target sprints non-closed
        "closed_buckets":  { closed_sprint_id_int: [issue_key, ...] },
        "multi_closed":    [issue_key, ...],  # in >1 closed sprint -> needs manual care
        "missing_map":     [(issue_key, [unmapped_source_sprint_id, ...]), ...],
      }
    """
    # Current state per target sprint id (re-queried at runtime, not from CSV,
    # because a prior partial run may have changed states).
    target_sprint_ids_all = {int(v) for v in sprint_map.values() if v.isdigit()}
    sprint_state: Dict[int, str] = {}
    for sid in target_sprint_ids_all:
        try:
            sprint_state[sid] = client.sprint(sid).get("state", "future")
        except Exception as e:
            logging.warning("Could not fetch state for target sprint %s: %s", sid, e)
            sprint_state[sid] = "unknown"

    plan: Dict[str, Any] = {
        "issues": {},
        "open_issues": [],
        "closed_buckets": {},
        "multi_closed": [],
        "missing_map": [],
        "sprint_state": sprint_state,
    }

    jql = f'project = "{target_key}" AND sprint is not EMPTY'
    for issue in client.search_jql(jql, fields=[sprint_field_id]):
        key = issue["key"]
        if key in done:
            continue
        sprint_field = issue["fields"].get(sprint_field_id)
        if not sprint_field:
            continue
        sprint_objs = sprint_field if isinstance(sprint_field, list) else [sprint_field]
        source_sprint_ids = [str(s.get("id")) for s in sprint_objs if isinstance(s, dict) and s.get("id") is not None]

        target_ids: List[int] = []
        unmapped: List[str] = []
        for sid in source_sprint_ids:
            tid = sprint_map.get(sid)
            if tid:
                tid_int = int(tid)
                if tid_int not in target_ids:  # dedupe preserving order
                    target_ids.append(tid_int)
            else:
                unmapped.append(sid)
        if unmapped:
            plan["missing_map"].append((key, unmapped))
        if not target_ids:
            continue

        plan["issues"][key] = target_ids
        closed_ids = [tid for tid in target_ids if sprint_state.get(tid) == "closed"]
        if not closed_ids:
            plan["open_issues"].append(key)
        elif len(closed_ids) > 1:
            # Issue spans multiple closed target sprints — we cannot reopen
            # more than one at a time on a non-parallel-sprint board.
            plan["multi_closed"].append(key)
        else:
            plan["closed_buckets"].setdefault(closed_ids[0], []).append(key)
    return plan


def _apply_sprint_update(
    client: JiraClient,
    issue_key: str,
    target_sprint_ids: List[int],
    sprint_field_id: str,
    checkpoint_handle,
    dry_run: bool,
) -> None:
    """Write a single issue's sprint field. Caller handles try/except + tally."""
    body = _build_sprint_update_payload(sprint_field_id, target_sprint_ids)
    if dry_run:
        logging.debug("[DRY-RUN] %s -> %s", issue_key, target_sprint_ids)
        return
    client.put(f"/rest/api/3/issue/{issue_key}", body=body, params={"notifyUsers": "false"})
    checkpoint_handle.write(issue_key + "\n")
    checkpoint_handle.flush()
    time.sleep(SLEEP_BETWEEN_WRITES)


def cmd_update_sprints(cfg: Config) -> None:
    """Replace source sprint IDs with target sprint IDs on all issues in target project.

    Execution model:
      1. Plan: scan the target project once, group issues by whether their
         target sprint set includes any *closed* sprints.
      2. Phase A (fast path): issues whose target sprints are all non-closed
         are updated directly.
      3. Phase B (closed-sprint cycle): for each closed target sprint that has
         pending issues:
           a. Reopen (closed -> active).
           b. Update all issues that map into it.
           c. Re-close (active -> closed).
         Only one closed sprint is reopened at a time, because Jira boards
         disallow more than one active sprint unless parallel sprints are
         explicitly enabled on the board.

    Known caveats:
      - /rest/api/3/search/jql returns no field data unless `fields` is set
        explicitly. "sprint" is a JQL alias, not a field id — we use the real
        custom field id (e.g. customfield_10020) resolved via /rest/api/3/field.
      - The Sprint field's editIssue payload is a long-standing quirk: a single
        sprint requires a bare integer under `fields`; multiple sprints
        require the `update`/`set` operations syntax.
      - Re-closing a sprint overwrites its `completeDate` to *now*. The
        original completion timestamp is unrecoverable. This is a deliberate
        trade-off accepted as the cost of historical sprint membership.
      - Issues that span more than one closed sprint are skipped and listed
        in `multi_closed_issues.csv` for manual handling (would require
        parallel sprints enabled on the board to process automatically).
    """
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key, target_must_exist=True)

    mapping_path = cfg.output_dir / "build" / "sprint_id_mapping.csv"
    if not mapping_path.exists():
        raise SystemExit(f"Sprint mapping not found: {mapping_path}. Run 'build' first.")

    sprint_map = load_sprint_id_map(mapping_path)
    if not sprint_map:
        logging.warning("No sprint mappings found. Nothing to update.")
        return

    sprint_field_id = client.sprint_field_id()
    logging.info("Using sprint field id: %s", sprint_field_id)

    out = cfg.output_dir / "update_sprints"
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / "updated.txt"
    done = set(checkpoint.read_text().splitlines()) if checkpoint.exists() else set()

    # ------ Plan ------
    logging.info("Planning sprint updates (scanning target project once)...")
    plan = _plan_sprint_updates(client, cfg.target_key, sprint_field_id, sprint_map, done)

    n_total = len(plan["issues"])
    n_open = len(plan["open_issues"])
    n_buckets = len(plan["closed_buckets"])
    n_closed_issues = sum(len(v) for v in plan["closed_buckets"].values())
    n_multi = len(plan["multi_closed"])
    logging.info(
        "Plan: %d issues need updating | %d direct | %d via reopened sprints across %d closed sprints | %d multi-closed (manual)",
        n_total, n_open, n_closed_issues, n_buckets, n_multi,
    )

    if plan["missing_map"]:
        write_csv(
            out / "missing_sprint_map.csv",
            [{"issue": k, "unmapped_source_sprint_ids": ",".join(ids)} for k, ids in plan["missing_map"]],
            ["issue", "unmapped_source_sprint_ids"],
        )
    if plan["multi_closed"]:
        write_csv(
            out / "multi_closed_issues.csv",
            [{"issue": k, "target_sprint_ids": ",".join(str(x) for x in plan["issues"][k])} for k in plan["multi_closed"]],
            ["issue", "target_sprint_ids"],
        )
        logging.warning(
            "%d issue(s) span multiple closed target sprints and were skipped. "
            "Enable parallel sprints on the target board and re-run, or update manually. "
            "See %s",
            n_multi, out / "multi_closed_issues.csv",
        )

    failures: List[Dict[str, str]] = []
    updated = 0

    # ------ Phase A: direct updates for issues with no closed target sprints ------
    if plan["open_issues"]:
        logging.info("Phase A: %d issues with non-closed target sprints only", n_open)
        with checkpoint.open("a") as ckpt:
            for key in plan["open_issues"]:
                try:
                    _apply_sprint_update(client, key, plan["issues"][key], sprint_field_id, ckpt, cfg.dry_run)
                    updated += 1
                    if updated % 50 == 0:
                        logging.info("  updated %d issues", updated)
                except Exception as e:
                    failures.append({"issue": key, "phase": "A", "error": str(e)[:300]})
                    logging.warning("Phase A: failed to update %s: %s", key, e)

    # ------ Phase B: reopen each closed sprint, update its issues, re-close ------
    sprint_cycle_log: List[Dict[str, Any]] = []
    if plan["closed_buckets"]:
        logging.info(
            "Phase B: %d closed sprint(s) to cycle (reopen -> update -> close). "
            "Note: each re-close stamps a new completeDate.",
            n_buckets,
        )

    for closed_sprint_id, issue_keys in plan["closed_buckets"].items():
        cycle_entry: Dict[str, Any] = {
            "sprint_id": closed_sprint_id,
            "issues_planned": len(issue_keys),
            "issues_updated": 0,
            "reopened": False,
            "reclosed": False,
            "error": "",
        }
        logging.info("Sprint %s: reopening for %d issue update(s)...", closed_sprint_id, len(issue_keys))

        # 1. Reopen
        try:
            if not cfg.dry_run:
                client.transition_sprint(closed_sprint_id, "active")
            cycle_entry["reopened"] = True
        except Exception as e:
            msg = str(e)
            cycle_entry["error"] = f"reopen failed: {msg[:200]}"
            logging.error(
                "Sprint %s: reopen failed (%s). Skipping its %d issue(s). "
                "If the message mentions an active sprint conflict, enable parallel sprints on the board.",
                closed_sprint_id, msg[:200], len(issue_keys),
            )
            for key in issue_keys:
                failures.append({"issue": key, "phase": "B-reopen", "error": msg[:300]})
            sprint_cycle_log.append(cycle_entry)
            continue

        # 2. Update issues
        sprint_updated = 0
        with checkpoint.open("a") as ckpt:
            for key in issue_keys:
                try:
                    _apply_sprint_update(client, key, plan["issues"][key], sprint_field_id, ckpt, cfg.dry_run)
                    updated += 1
                    sprint_updated += 1
                except Exception as e:
                    failures.append({"issue": key, "phase": "B-update", "error": str(e)[:300]})
                    logging.warning("Sprint %s: failed to update %s: %s", closed_sprint_id, key, e)
        cycle_entry["issues_updated"] = sprint_updated
        logging.info("Sprint %s: updated %d/%d issues", closed_sprint_id, sprint_updated, len(issue_keys))

        # 3. Re-close
        try:
            if not cfg.dry_run:
                client.transition_sprint(closed_sprint_id, "closed")
            cycle_entry["reclosed"] = True
        except Exception as e:
            msg = str(e)
            cycle_entry["error"] = (cycle_entry["error"] + " | " if cycle_entry["error"] else "") + f"re-close failed: {msg[:200]}"
            logging.error(
                "Sprint %s: re-close failed (%s). Sprint is left ACTIVE — close it manually.",
                closed_sprint_id, msg[:200],
            )
        sprint_cycle_log.append(cycle_entry)

    # ------ Outputs ------
    if sprint_cycle_log:
        write_csv(
            out / "sprint_cycle_log.csv",
            [{
                "sprint_id": c["sprint_id"],
                "issues_planned": c["issues_planned"],
                "issues_updated": c["issues_updated"],
                "reopened": c["reopened"],
                "reclosed": c["reclosed"],
                "error": c["error"],
            } for c in sprint_cycle_log],
            ["sprint_id", "issues_planned", "issues_updated", "reopened", "reclosed", "error"],
        )

    if failures:
        write_csv(out / "failures.csv", failures, ["issue", "phase", "error"])

    logging.info("UPDATE-SPRINTS complete. Updated %d issues. Failures: %d.", updated, len(failures))


# ---------------------------------------------------------------------------
# EXPORT-SPRINT-CSV (External Import fallback for closed sprints)
# ---------------------------------------------------------------------------
# Jira's built-in External System Import (Settings -> System -> External System
# Import -> CSV) sets the Sprint field by id without the reopen/re-close dance
# that `update-sprints` needs for closed sprints, so it is the most reliable way
# to re-attach historical sprint membership. This module produces the CSV that
# import consumes.
#
# Delimiter is a *semicolon* on purpose: the Sprint ID column may hold several
# comma-separated ids for issues that belonged to more than one sprint, so a
# comma cannot also be the field separator.
SPRINT_CSV_DELIMITER = ";"
SPRINT_CSV_HEADERS = [
    "Issue Key",
    "Assignee",
    "Summary",
    "Project name",
    "Project Key",
    "Project Type",
    "Sprint ID",
]


def _assignee_identifier(assignee: Optional[Dict]) -> str:
    """Pick the most import-friendly identifier for an issue's assignee.

    Jira's CSV importer matches users by email address or account id; email is
    more human-readable so we prefer it, falling back to account id, then
    display name. Returns "" for unassigned issues.
    """
    if not assignee:
        return ""
    return (
        assignee.get("emailAddress")
        or assignee.get("accountId")
        or assignee.get("displayName")
        or ""
    )


def _collect_sprint_csv_rows(
    client: JiraClient,
    target_key: str,
    sprint_field_id: str,
    sprint_map: Dict[str, str],
) -> tuple[List[Dict[str, str]], List[tuple]]:
    """Scan the target project once and build the External Import rows.

    Mirrors the source->target sprint id mapping in `_plan_sprint_updates`
    (deduped, order-preserving) but also captures the issue fields the importer
    needs (assignee, summary, project). Issues are read from the *target*
    project, where the Sprint field still references the *source* sprint ids
    after a bulk-move.

    Returns (rows, missing_map):
      - rows: dicts keyed by SPRINT_CSV_HEADERS, target sprint ids joined by ","
      - missing_map: [(issue_key, [unmapped_source_sprint_id, ...]), ...]
    """
    rows: List[Dict[str, str]] = []
    missing_map: List[tuple] = []
    fields = [sprint_field_id, "summary", "assignee", "project"]
    jql = f'project = "{target_key}" AND sprint is not EMPTY'
    for issue in client.search_jql(jql, fields=fields):
        f = issue["fields"]
        sprint_field = f.get(sprint_field_id)
        if not sprint_field:
            continue
        sprint_objs = sprint_field if isinstance(sprint_field, list) else [sprint_field]
        source_sprint_ids = [str(s.get("id")) for s in sprint_objs if isinstance(s, dict) and s.get("id") is not None]

        target_ids: List[str] = []
        unmapped: List[str] = []
        for sid in source_sprint_ids:
            tid = sprint_map.get(sid)
            if tid:
                if tid not in target_ids:  # dedupe preserving order
                    target_ids.append(tid)
            else:
                unmapped.append(sid)
        if unmapped:
            missing_map.append((issue["key"], unmapped))
        if not target_ids:
            continue

        project = f.get("project") or {}
        rows.append({
            "Issue Key": issue["key"],
            "Assignee": _assignee_identifier(f.get("assignee")),
            "Summary": f.get("summary") or "",
            "Project name": project.get("name", ""),
            "Project Key": project.get("key", ""),
            "Project Type": project.get("projectTypeKey", ""),
            "Sprint ID": ",".join(target_ids),
        })
    return rows, missing_map


def cmd_export_sprint_csv(cfg: Config) -> None:
    """Generate a semicolon-delimited CSV for Jira's External Import feature to
    re-attach sprint membership by *sprint id*.

    Why this exists: `update-sprints` rewrites the Sprint field via the REST
    API, which requires reopening closed sprints. That fails when a board
    disallows more than one active sprint, or when an issue spans several closed
    sprints (such issues are skipped and listed in `multi_closed_issues.csv`).
    Jira's built-in External System Import sets the Sprint field by id without
    any reopen/re-close cycle, so it is the reliable path for closed sprints.
    This command produces the CSV that import consumes — it performs no writes
    against Jira itself; the import is a manual UI step.

    Reads the same `build/sprint_id_mapping.csv` as `update-sprints`, scans the
    target project for every issue that still has a sprint, maps source sprint
    ids to target sprint ids, and writes one row per issue.
    """
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key, target_must_exist=True)

    mapping_path = cfg.output_dir / "build" / "sprint_id_mapping.csv"
    if not mapping_path.exists():
        raise SystemExit(f"Sprint mapping not found: {mapping_path}. Run 'build' first.")

    sprint_map = load_sprint_id_map(mapping_path)
    if not sprint_map:
        logging.warning("No sprint mappings found. Nothing to export.")
        return

    sprint_field_id = client.sprint_field_id()
    logging.info("Using sprint field id: %s", sprint_field_id)

    out = cfg.output_dir / "export_sprint_csv"
    out.mkdir(parents=True, exist_ok=True)

    logging.info("Scanning target project %s for issues with sprints...", cfg.target_key)
    rows, missing_map = _collect_sprint_csv_rows(client, cfg.target_key, sprint_field_id, sprint_map)

    csv_path = out / "sprint_import.csv"
    write_csv(csv_path, rows, SPRINT_CSV_HEADERS, delimiter=SPRINT_CSV_DELIMITER)

    if missing_map:
        write_csv(
            out / "missing_sprint_map.csv",
            [{"issue": k, "unmapped_source_sprint_ids": ",".join(ids)} for k, ids in missing_map],
            ["issue", "unmapped_source_sprint_ids"],
        )
        logging.warning(
            "%d issue(s) referenced source sprints with no target mapping and were "
            "omitted from the import CSV. See %s",
            len(missing_map), out / "missing_sprint_map.csv",
        )

    logging.info(
        "EXPORT-SPRINT-CSV complete. Wrote %d row(s) to %s (delimiter '%s'). "
        "Import via Jira Settings -> System -> External System Import -> CSV.",
        len(rows), csv_path, SPRINT_CSV_DELIMITER,
    )


# ---------------------------------------------------------------------------
# VALIDATE
# ---------------------------------------------------------------------------
def cmd_validate(cfg: Config) -> None:
    """
    Compare source baseline (extracted earlier) with current target project state.
    """
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key, target_must_exist=True)

    baseline_path = cfg.output_dir / "extract" / "11_inventory_baseline.json"
    if not baseline_path.exists():
        raise SystemExit("Baseline not found. Run 'extract' first.")
    baseline = read_json(baseline_path)

    out_dir = cfg.output_dir / "validate"
    out_dir.mkdir(parents=True, exist_ok=True)

    logging.info("Scanning target project %s...", cfg.target_key)
    agg = extract_issues_aggregate(client, cfg.target_key)
    buckets = extract_boolean_buckets(client, cfg.target_key)

    target_inv = {
        "projectKey": cfg.target_key,
        "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": agg["total"],
        "byType": agg["byType"],
        "byStatus": agg["byStatus"],
        "byResolution": agg["byResolution"],
        **buckets,
    }
    write_json(out_dir / "target_inventory.json", target_inv)

    delta = {
        "total": _diff(baseline["total"], target_inv["total"]),
        "byType": _dict_diff(baseline["byType"], target_inv["byType"]),
        "byStatus": _dict_diff(baseline["byStatus"], target_inv["byStatus"]),
        "byResolution": _dict_diff(baseline["byResolution"], target_inv["byResolution"]),
        "withAttachments": _diff(baseline["withAttachments"], target_inv["withAttachments"]),
        "withComments": _diff(baseline["withComments"], target_inv["withComments"]),
        "withFixVersion": _diff(baseline["withFixVersion"], target_inv["withFixVersion"]),
        "withComponents": _diff(baseline["withComponents"], target_inv["withComponents"]),
        "withSprint": _diff(baseline["withSprint"], target_inv["withSprint"]),
    }
    write_json(out_dir / "validation_delta.json", delta)

    summary_lines = ["category,source,target,diff"]
    for cat in ("total", "withAttachments", "withComments", "withFixVersion", "withComponents", "withSprint"):
        d = delta[cat]
        summary_lines.append(f"{cat},{d['source']},{d['target']},{d['diff']}")
    (out_dir / "validation_summary.csv").write_text("\n".join(summary_lines), encoding="utf-8")

    sprint_map_path = cfg.output_dir / "build" / "sprint_id_mapping.csv"
    if sprint_map_path.exists():
        sprint_map = load_sprint_id_map(sprint_map_path)
        if sprint_map:
            logging.info("Validating sprint membership...")
            mismatches = _validate_sprint_membership(client, cfg.source_key, cfg.target_key, sprint_map)
            if mismatches:
                write_json(out_dir / "sprint_mismatches.json", mismatches)
                logging.warning("Sprint membership mismatches found: %d sprints with count differences", len(mismatches))
            else:
                logging.info("Sprint membership validation passed (counts match for all mapped sprints).")
        else:
            logging.info("No active sprint mappings found, skipping sprint membership validation.")
    else:
        logging.info("No sprint mapping file found, skipping sprint membership validation.")

    field_mapping_path = cfg.output_dir / "mapping" / "field_mapping.csv"
    if field_mapping_path.exists():
        logging.info("Validating custom field values (sampling up to 50 issues)...")
        field_mismatches = _validate_field_values(client, cfg.key_field_id, field_mapping_path, cfg.target_key)
        if field_mismatches:
            write_json(out_dir / "field_mismatches.json", field_mismatches)
            logging.warning("Field value mismatches found in sample. See field_mismatches.json")
        else:
            logging.info("Field value sampling passed (sample values match).")
    else:
        logging.info("No field mapping CSV found, skipping custom field validation.")

    non_zero = [cat for cat in ("total", "withAttachments", "withComments", "withFixVersion", "withComponents", "withSprint")
                if delta[cat]["diff"] != 0]
    if non_zero:
        logging.warning("VALIDATION COMPLETE – Differences found in: %s", ", ".join(non_zero))
    else:
        logging.info("VALIDATION COMPLETE – All aggregate counts match. (Sprint/field checks logged separately)")
    logging.info("Outputs written to %s", out_dir)


def _validate_sprint_membership(client: JiraClient, src_key: str, tgt_key: str,
                                sprint_map: Dict[str, str]) -> List[Dict]:
    """For each mapped sprint, compare source vs target issue counts."""
    mismatches = []
    for src_id, tgt_id in sprint_map.items():
        src_count = client.approximate_count(f'project = "{src_key}" AND sprint = {src_id}')
        if src_count == 0:
            continue
        tgt_count = client.approximate_count(f'project = "{tgt_key}" AND sprint = {tgt_id}')
        if src_count != tgt_count:
            mismatches.append({
                "source_sprint_id": src_id,
                "target_sprint_id": tgt_id,
                "source_issue_count": src_count,
                "target_issue_count": tgt_count,
                "difference": tgt_count - src_count,
            })
    return mismatches


def _validate_field_values(client: JiraClient, key_field_id: str,
                           mapping_csv: Path, tgt_key: str) -> List[Dict]:
    """Sample target issues, fetch source via stamped key, compare mapped fields."""
    mappings = [(m["source_field_id"], m["target_field_id"]) for m in load_field_mappings(mapping_csv)]
    if not mappings:
        return []

    sample_jql = f'project = "{tgt_key}" AND "{key_field_id}" IS NOT EMPTY'
    target_field_ids = [tgt for _, tgt in mappings]
    source_field_ids = [src for src, _ in mappings]
    mismatches = []
    sampled = 0
    for issue in client.search_jql(sample_jql, fields=[key_field_id] + target_field_ids, max_results=50):
        tgt_key_issue = issue["key"]
        stamped = issue["fields"].get(key_field_id)
        if not stamped:
            continue
        try:
            src_issue = client.issue(stamped, fields=source_field_ids)
        except Exception as e:
            mismatches.append({"target_issue": tgt_key_issue, "source_key": stamped, "error": f"Cannot fetch source: {e}"})
            continue

        for src_field, tgt_field in mappings:
            src_val = src_issue.get("fields", {}).get(src_field)
            tgt_val = issue["fields"].get(tgt_field)
            if src_val != tgt_val:
                mismatches.append({
                    "target_issue": tgt_key_issue,
                    "source_issue": stamped,
                    "source_field": src_field,
                    "target_field": tgt_field,
                    "source_value": str(src_val)[:200],
                    "target_value": str(tgt_val)[:200],
                })
        sampled += 1
        if sampled >= 50:
            break
        if sampled % 20 == 0:
            logging.info("  sampled %d issues for field validation", sampled)
    return mismatches


def _diff(src: int, tgt: int) -> Dict[str, int]:
    return {"source": src, "target": tgt, "diff": tgt - src}


def _dict_diff(src: Dict[str, int], tgt: Dict[str, int]) -> Dict[str, Dict[str, int]]:
    all_keys = set(src.keys()) | set(tgt.keys())
    return {k: _diff(src.get(k, 0), tgt.get(k, 0)) for k in sorted(all_keys)}

# ---------------------------------------------------------------------------
# DOCTOR
# ---------------------------------------------------------------------------
def cmd_doctor(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, cfg.source_key, cfg.target_key,
              source_must_exist=bool(cfg.source_key), target_must_exist=False)
    if cfg.source_key:
        boards = list(client.paged("/rest/agile/1.0/board", params={"projectKeyOrId": cfg.source_key}))
        logging.info("Boards on source: %s", [(b["id"], b["name"]) for b in boards])
    if cfg.target_key:
        boards = list(client.paged("/rest/agile/1.0/board", params={"projectKeyOrId": cfg.target_key}))
        logging.info("Boards on target: %s", [(b["id"], b["name"]) for b in boards])
    if cfg.key_field_id:
        precheck_key_field(client, cfg.key_field_id)
    try:
        logging.info("Sprint custom field id: %s", client.sprint_field_id())
    except SystemExit as e:
        logging.warning("Sprint field resolution: %s", e)
    logging.info("Doctor done.")

# ---------------------------------------------------------------------------
# REPORT (post-migration Excel for project team)
# ---------------------------------------------------------------------------
# Color palette (kept narrow on purpose: neutral, professional, print-safe)
_RPT_FONT = "Calibri"
_RPT_HEADER_FILL = "1F4E78"      # deep blue, white text
_RPT_HEADER_FG = "FFFFFF"
_RPT_BAND_FILL = "F2F2F2"        # light grey for alternate rows
_RPT_OK_FILL = "E2EFDA"          # soft green
_RPT_WARN_FILL = "FFF2CC"        # soft amber
_RPT_BAD_FILL = "FBE5D6"         # soft red
_RPT_TITLE_FILL = "D9E1F2"       # pale blue for sheet titles
_RPT_BORDER_COLOR = "BFBFBF"


def _rpt_styles():
    """Lazily import openpyxl and return a styles bundle."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    thin = Side(border_style="thin", color=_RPT_BORDER_COLOR)
    return {
        "Font": Font,
        "Fill": PatternFill,
        "Align": Alignment,
        "Border": Border,
        "thin_border": Border(left=thin, right=thin, top=thin, bottom=thin),
        "title_font": Font(name=_RPT_FONT, size=14, bold=True, color="1F4E78"),
        "header_font": Font(name=_RPT_FONT, size=11, bold=True, color=_RPT_HEADER_FG),
        "header_fill": PatternFill("solid", start_color=_RPT_HEADER_FILL),
        "title_fill": PatternFill("solid", start_color=_RPT_TITLE_FILL),
        "band_fill": PatternFill("solid", start_color=_RPT_BAND_FILL),
        "ok_fill": PatternFill("solid", start_color=_RPT_OK_FILL),
        "warn_fill": PatternFill("solid", start_color=_RPT_WARN_FILL),
        "bad_fill": PatternFill("solid", start_color=_RPT_BAD_FILL),
        "body_font": Font(name=_RPT_FONT, size=11),
        "mono_font": Font(name="Consolas", size=10),
        "link_font": Font(name="Consolas", size=10, color="0563C1", underline="single"),
        "center": Alignment(horizontal="center", vertical="center"),
        "left": Alignment(horizontal="left", vertical="center", wrap_text=True),
        "right": Alignment(horizontal="right", vertical="center"),
    }


def _rpt_write_header_row(ws, row: int, headers: List[str], styles) -> None:
    for col_idx, val in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col_idx, value=val)
        cell.font = styles["header_font"]
        cell.fill = styles["header_fill"]
        cell.alignment = styles["center"]
        cell.border = styles["thin_border"]


def _rpt_write_data_rows(ws, start_row: int, rows: List[List[Any]], styles,
                        align_overrides: Optional[Dict[int, Any]] = None) -> int:
    """Write rows starting at start_row; return the row index AFTER the last written.
    align_overrides: {col_index_1based: alignment} to override default left/right.
    """
    align_overrides = align_overrides or {}
    for i, row_vals in enumerate(rows):
        r = start_row + i
        for col_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=r, column=col_idx, value=val)
            cell.font = styles["body_font"]
            cell.border = styles["thin_border"]
            if i % 2 == 1:
                cell.fill = styles["band_fill"]
            if col_idx in align_overrides:
                cell.alignment = align_overrides[col_idx]
            else:
                cell.alignment = styles["right"] if isinstance(val, (int, float)) else styles["left"]
    return start_row + len(rows)


def _rpt_title(ws, title: str, subtitle: str, styles, ncols: int = 6) -> int:
    """Render a banner title + subtitle. Returns the next row to write at."""
    ws.cell(row=1, column=1, value=title).font = styles["title_font"]
    ws.cell(row=1, column=1).fill = styles["title_fill"]
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    ws.cell(row=1, column=1).alignment = styles["left"]
    ws.row_dimensions[1].height = 26
    if subtitle:
        ws.cell(row=2, column=1, value=subtitle).font = _rpt_font(styles, italic=True, size=10, color="595959")
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
        ws.cell(row=2, column=1).alignment = styles["left"]
        return 4
    return 3


def _rpt_font(styles, **kwargs):
    """Build a Font with the report font family applied automatically."""
    return styles["Font"](name=_RPT_FONT, **kwargs)


def _rpt_autosize(ws, min_width: int = 10, max_width: int = 60) -> None:
    """Approximate auto-fit by scanning written cells. Ignores merged ranges."""
    widths: Dict[int, int] = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None or not hasattr(cell, "column"):
                continue
            try:
                length = len(str(cell.value))
            except Exception:
                continue
            widths[cell.column] = max(widths.get(cell.column, min_width), min(length + 2, max_width))
    from openpyxl.utils import get_column_letter
    for col_idx, w in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = w


def _build_report_workbook(cfg: Config, project_team_data: Dict[str, Any]) -> Path:
    """Assemble the workbook. All numeric totals/deltas use Excel formulas."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    styles = _rpt_styles()
    wb = Workbook()
    # The default sheet will be repurposed as Summary
    wb.remove(wb.active)

    # ------ 1. Summary ------
    _build_summary_sheet(wb, project_team_data, styles)
    # ------ 2. Issue Counts ------
    _build_issue_counts_sheet(wb, project_team_data, styles)
    # ------ 3. Sprint Reconciliation ------
    _build_sprint_reconciliation_sheet(wb, project_team_data, styles)
    # ------ 4. Versions ------
    _build_versions_sheet(wb, project_team_data, styles)
    # ------ 5. Components ------
    _build_components_sheet(wb, project_team_data, styles)
    # ------ 6. Content Buckets ------
    _build_content_buckets_sheet(wb, project_team_data, styles)
    # ------ 7. Issue Key Map ------
    _build_issue_key_map_sheet(wb, project_team_data, styles)
    # ------ 8. Exceptions ------
    _build_exceptions_sheet(wb, project_team_data, styles)
    # ------ 9. Methodology ------
    _build_methodology_sheet(wb, project_team_data, styles)

    out_dir = cfg.output_dir / "report"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "migration_report.xlsx"
    wb.save(out_path)
    return out_path


def _build_summary_sheet(wb, data: Dict[str, Any], styles) -> None:
    ws = wb.create_sheet("Summary")
    ws.sheet_view.showGridLines = False
    src = data["baseline"]
    tgt = data["target_inv"]
    proj_src = data["source_meta"]

    row = _rpt_title(
        ws,
        f"Migration Report — {proj_src.get('key')} → {data['target_key']}",
        f"Source: {proj_src.get('name')} ({proj_src.get('key')})   |   "
        f"Target: {data.get('target_name', '(name unavailable)')} ({data['target_key']})   |   "
        f"Generated: {data['generated_at']}",
        styles, ncols=4,
    )

    # Headline KPIs as a 2-col table with formulas
    _rpt_write_header_row(ws, row, ["Metric", "Source", "Target", "Status"], styles)
    row += 1

    # Each metric: source value, target value, status (formula)
    metrics = [
        ("Total issues", src["total"], tgt["total"]),
        ("Issues with attachments", src["withAttachments"], tgt["withAttachments"]),
        ("Issues with comments", src["withComments"], tgt["withComments"]),
        ("Issues with fixVersion", src["withFixVersion"], tgt["withFixVersion"]),
        ("Issues with components", src["withComponents"], tgt["withComponents"]),
        ("Issues with sprints", src["withSprint"], tgt["withSprint"]),
        ("Distinct issue types", len(src["byType"]), len(tgt["byType"])),
        ("Distinct statuses", len(src["byStatus"]), len(tgt["byStatus"])),
    ]
    start_row = row
    for i, (label, s_val, t_val) in enumerate(metrics):
        r = start_row + i
        ws.cell(row=r, column=1, value=label).font = styles["body_font"]
        ws.cell(row=r, column=2, value=s_val).font = styles["body_font"]
        ws.cell(row=r, column=3, value=t_val).font = styles["body_font"]
        # Status formula: OK if equal, warn if target >= 95% of source, else attention
        ws.cell(
            row=r,
            column=4,
            value=f'=IF(C{r}=B{r},"OK",IF(B{r}=0,"OK",IF(C{r}/B{r}>=0.95,"REVIEW","ATTENTION")))',
        ).font = styles["body_font"]
        for col in range(1, 5):
            c = ws.cell(row=r, column=col)
            c.border = styles["thin_border"]
            if i % 2 == 1:
                c.fill = styles["band_fill"]
            if col >= 2:
                c.alignment = styles["right"] if col < 4 else styles["center"]
            else:
                c.alignment = styles["left"]
    last_metric_row = start_row + len(metrics) - 1

    # Conditional formatting on the status column
    from openpyxl.formatting.rule import CellIsRule
    rng = f"D{start_row}:D{last_metric_row}"
    ws.conditional_formatting.add(
        rng, CellIsRule(operator="equal", formula=['"OK"'], stopIfTrue=False, fill=styles["ok_fill"])
    )
    ws.conditional_formatting.add(
        rng, CellIsRule(operator="equal", formula=['"REVIEW"'], stopIfTrue=False, fill=styles["warn_fill"])
    )
    ws.conditional_formatting.add(
        rng, CellIsRule(operator="equal", formula=['"ATTENTION"'], stopIfTrue=False, fill=styles["bad_fill"])
    )

    # Sprint cycle summary if available
    next_row = last_metric_row + 3
    cycles = data.get("sprint_cycle_log") or []
    if cycles:
        ws.cell(row=next_row, column=1, value="Sprint reopen/close cycle outcomes").font = _rpt_font(styles, bold=True, size=12)
        next_row += 1
        _rpt_write_header_row(ws, next_row, ["Closed sprint id", "Issues planned", "Issues updated", "Reopened", "Re-closed", "Notes"], styles)
        next_row += 1
        cycle_rows = []
        for c in cycles:
            cycle_rows.append([
                int(c.get("sprint_id", 0) or 0),
                int(c.get("issues_planned", 0) or 0),
                int(c.get("issues_updated", 0) or 0),
                str(c.get("reopened", "")),
                str(c.get("reclosed", "")),
                c.get("error", "") or "—",
            ])
        next_row = _rpt_write_data_rows(ws, next_row, cycle_rows, styles)

    # At-a-glance callouts
    callouts = data.get("callouts", [])
    if callouts:
        next_row += 2
        ws.cell(row=next_row, column=1, value="Items to be aware of").font = _rpt_font(styles, bold=True, size=12)
        next_row += 1
        _rpt_write_header_row(ws, next_row, ["#", "Topic", "Detail"], styles)
        next_row += 1
        rows = [[i + 1, c["topic"], c["detail"]] for i, c in enumerate(callouts)]
        next_row = _rpt_write_data_rows(ws, next_row, rows, styles)

    _rpt_autosize(ws)
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["D"].width = 14
    ws.freeze_panes = "A5"


def _build_issue_counts_sheet(wb, data: Dict[str, Any], styles) -> None:
    ws = wb.create_sheet("Issue Counts")
    ws.sheet_view.showGridLines = False
    src = data["baseline"]
    tgt = data["target_inv"]

    row = _rpt_title(
        ws,
        "Issue counts — source vs target",
        "Side-by-side comparison by issue type, status, and resolution. The Delta column is a formula; "
        "edit the source/target cells to see it recalculate.",
        styles, ncols=4,
    )

    sections = [
        ("By issue type", src["byType"], tgt["byType"]),
        ("By status", src["byStatus"], tgt["byStatus"]),
        ("By resolution", src["byResolution"], tgt["byResolution"]),
    ]
    for section_title, src_dict, tgt_dict in sections:
        ws.cell(row=row, column=1, value=section_title).font = _rpt_font(styles, bold=True, size=12)
        row += 1
        _rpt_write_header_row(ws, row, ["Name", "Source", "Target", "Delta"], styles)
        row += 1
        keys = sorted(set(src_dict.keys()) | set(tgt_dict.keys()))
        rows_data = []
        first_row = row
        for k in keys:
            rows_data.append([k, int(src_dict.get(k, 0)), int(tgt_dict.get(k, 0)), None])
        for i, vals in enumerate(rows_data):
            r = first_row + i
            for col_idx, val in enumerate(vals, start=1):
                cell = ws.cell(row=r, column=col_idx, value=val)
                cell.font = styles["body_font"]
                cell.border = styles["thin_border"]
                if i % 2 == 1:
                    cell.fill = styles["band_fill"]
                cell.alignment = styles["right"] if col_idx > 1 else styles["left"]
            ws.cell(row=r, column=4, value=f"=C{r}-B{r}")
        # Total row
        total_row = first_row + len(rows_data)
        ws.cell(row=total_row, column=1, value="Total").font = _rpt_font(styles, bold=True)
        ws.cell(row=total_row, column=2, value=f"=SUM(B{first_row}:B{total_row-1})").font = _rpt_font(styles, bold=True)
        ws.cell(row=total_row, column=3, value=f"=SUM(C{first_row}:C{total_row-1})").font = _rpt_font(styles, bold=True)
        ws.cell(row=total_row, column=4, value=f"=C{total_row}-B{total_row}").font = _rpt_font(styles, bold=True)
        for col in range(1, 5):
            c = ws.cell(row=total_row, column=col)
            c.border = styles["thin_border"]
            c.fill = styles["title_fill"]
            c.alignment = styles["right"] if col > 1 else styles["left"]
        row = total_row + 2

    _rpt_autosize(ws)
    ws.column_dimensions["A"].width = 32
    ws.freeze_panes = "A5"


def _build_sprint_reconciliation_sheet(wb, data: Dict[str, Any], styles) -> None:
    ws = wb.create_sheet("Sprints")
    ws.sheet_view.showGridLines = False

    row = _rpt_title(
        ws,
        "Sprint reconciliation",
        "Every source sprint, the target sprint it was rebuilt as, and the issue counts on both sides. "
        "Re-closed sprints will show 'completeDate updated to migration time' — see Methodology.",
        styles, ncols=8,
    )

    sprints_src = data.get("source_sprints", [])
    mapping = {row["source_id"]: row for row in data.get("sprint_mapping", [])}
    sprint_counts = data.get("sprint_counts", {})  # {"src": {id: n}, "tgt": {id: n}}
    src_counts = sprint_counts.get("src", {})
    tgt_counts = sprint_counts.get("tgt", {})

    _rpt_write_header_row(ws, row, [
        "Source sprint", "Source state", "Source issue count",
        "Target sprint", "Target state", "Target issue count",
        "Delta", "Notes",
    ], styles)
    row += 1
    first_data_row = row
    rows_data = []
    for s in sprints_src:
        src_id = str(s["id"])
        m = mapping.get(src_id, {})
        tgt_id = m.get("target_id", "")
        tgt_state = m.get("target_state", "")
        src_count = int(src_counts.get(src_id, 0))
        tgt_count = int(tgt_counts.get(tgt_id, 0)) if tgt_id else 0
        notes = []
        if not tgt_id:
            notes.append("No target sprint — verify build step")
        if s.get("state") == "closed":
            notes.append("Closed sprint cycled (completeDate refreshed)")
        rows_data.append([
            s["name"], s.get("state", ""), src_count,
            tgt_id or "—", tgt_state or "—", tgt_count,
            None,  # formula
            "; ".join(notes) or "—",
        ])
    for i, vals in enumerate(rows_data):
        r = first_data_row + i
        for col_idx, val in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=col_idx, value=val)
            cell.font = styles["body_font"]
            cell.border = styles["thin_border"]
            if i % 2 == 1:
                cell.fill = styles["band_fill"]
            cell.alignment = styles["right"] if col_idx in (3, 6, 7) else styles["left"]
        ws.cell(row=r, column=7, value=f"=F{r}-C{r}")

    # Conditional formatting: highlight non-zero Delta amber
    if rows_data:
        from openpyxl.formatting.rule import CellIsRule
        last_row = first_data_row + len(rows_data) - 1
        rng = f"G{first_data_row}:G{last_row}"
        ws.conditional_formatting.add(rng, CellIsRule(operator="notEqual", formula=["0"], fill=styles["warn_fill"]))

    _rpt_autosize(ws)
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["H"].width = 50
    ws.freeze_panes = "A5"


def _build_versions_sheet(wb, data: Dict[str, Any], styles) -> None:
    ws = wb.create_sheet("Versions")
    ws.sheet_view.showGridLines = False
    versions = data.get("source_versions", [])
    target_versions = {v["name"]: v for v in data.get("target_versions", [])}

    row = _rpt_title(ws, "Versions", "fixVersion / affectsVersion names — source vs target.", styles, ncols=7)
    _rpt_write_header_row(ws, row, ["Source name", "Released (src)", "Archived (src)", "Target name", "Released (tgt)", "Archived (tgt)", "Status"], styles)
    row += 1
    rows_data = []
    for v in versions:
        tv = target_versions.get(v["name"])
        present = bool(tv)
        rows_data.append([
            v["name"],
            "Yes" if v.get("released") else "No",
            "Yes" if v.get("archived") else "No",
            v["name"] if present else "—",
            ("Yes" if tv.get("released") else "No") if present else "—",
            ("Yes" if tv.get("archived") else "No") if present else "—",
            "Present" if present else "MISSING",
        ])
    first_data_row = row
    _rpt_write_data_rows(ws, row, rows_data, styles)
    if rows_data:
        from openpyxl.formatting.rule import CellIsRule
        last_row = first_data_row + len(rows_data) - 1
        rng = f"G{first_data_row}:G{last_row}"
        ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Present"'], fill=styles["ok_fill"]))
        ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"MISSING"'], fill=styles["bad_fill"]))
    _rpt_autosize(ws)
    ws.freeze_panes = "A5"


def _build_components_sheet(wb, data: Dict[str, Any], styles) -> None:
    ws = wb.create_sheet("Components")
    ws.sheet_view.showGridLines = False
    comps = data.get("source_components", [])
    target_comps = {c["name"]: c for c in data.get("target_components", [])}

    row = _rpt_title(ws, "Components", "Component names — source vs target.", styles, ncols=4)
    _rpt_write_header_row(ws, row, ["Source name", "Source description", "Target name", "Status"], styles)
    row += 1
    rows_data = []
    for c in comps:
        present = c["name"] in target_comps
        rows_data.append([
            c["name"],
            (c.get("description") or "")[:120],
            c["name"] if present else "—",
            "Present" if present else "MISSING",
        ])
    first_data_row = row
    _rpt_write_data_rows(ws, row, rows_data, styles)
    if rows_data:
        from openpyxl.formatting.rule import CellIsRule
        last_row = first_data_row + len(rows_data) - 1
        rng = f"D{first_data_row}:D{last_row}"
        ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"Present"'], fill=styles["ok_fill"]))
        ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"MISSING"'], fill=styles["bad_fill"]))
    _rpt_autosize(ws)
    ws.column_dimensions["B"].width = 50
    ws.freeze_panes = "A5"


def _build_content_buckets_sheet(wb, data: Dict[str, Any], styles) -> None:
    ws = wb.create_sheet("Content Buckets")
    ws.sheet_view.showGridLines = False
    src = data["baseline"]
    tgt = data["target_inv"]

    row = _rpt_title(
        ws,
        "Issue content presence",
        "How many issues had attachments, comments, fixVersion, components, and sprints set — source vs target.",
        styles, ncols=4,
    )
    _rpt_write_header_row(ws, row, ["Bucket", "Source count", "Target count", "Coverage %"], styles)
    row += 1
    buckets = [
        ("Attachments", src["withAttachments"], tgt["withAttachments"]),
        ("Comments", src["withComments"], tgt["withComments"]),
        ("Fix versions", src["withFixVersion"], tgt["withFixVersion"]),
        ("Components", src["withComponents"], tgt["withComponents"]),
        ("Sprints", src["withSprint"], tgt["withSprint"]),
    ]
    first_data_row = row
    for i, (name, s_val, t_val) in enumerate(buckets):
        r = first_data_row + i
        ws.cell(row=r, column=1, value=name)
        ws.cell(row=r, column=2, value=int(s_val))
        ws.cell(row=r, column=3, value=int(t_val))
        ws.cell(row=r, column=4, value=f'=IF(B{r}=0,"—",C{r}/B{r})')
        ws.cell(row=r, column=4).number_format = "0.0%"
        for col in range(1, 5):
            c = ws.cell(row=r, column=col)
            c.font = styles["body_font"]
            c.border = styles["thin_border"]
            if i % 2 == 1:
                c.fill = styles["band_fill"]
            c.alignment = styles["right"] if col > 1 else styles["left"]
    _rpt_autosize(ws)
    ws.freeze_panes = "A5"


def _build_issue_key_map_sheet(wb, data: Dict[str, Any], styles) -> None:
    """Full source-key -> target-key map. Uses append() in a tight loop
    (much faster than per-cell styling) and applies styles per column rather
    than per cell, so this scales to tens of thousands of rows.
    """
    ws = wb.create_sheet("Issue Key Map")
    ws.sheet_view.showGridLines = False
    pairs = data.get("issue_key_pairs", [])
    base_url = (data.get("base_url") or "").rstrip("/")
    n = len(pairs)
    row = _rpt_title(
        ws,
        "Issue key map",
        f"All {n:,} migrated issues — source key, target key, type, status, summary. "
        f"Source key is read from the stamped key field; target key is the live key in {data['target_key']} "
        f"(click to open the issue in Jira). "
        f"Use the filters in the header row to narrow by type or status.",
        styles, ncols=5,
    )

    headers = ["Source key", "Target key", "Type", "Status", "Summary"]
    _rpt_write_header_row(ws, row, headers, styles)
    header_row = row
    row += 1

    if n:
        first_data_row = row
        # Bulk append — far faster than ws.cell(...)=value for large datasets
        for p in pairs:
            ws.append([
                p.get("source"),
                p.get("target"),
                p.get("issuetype", ""),
                p.get("status", ""),
                (p.get("summary") or "")[:300],
            ])
        last_data_row = first_data_row + n - 1

        # Per-column font application. The Target key column also gets a
        # hyperlink to {base_url}/browse/{key} so users can click straight
        # through to the issue.
        mono = styles["mono_font"]
        link = styles["link_font"]
        body = styles["body_font"]
        for r in range(first_data_row, last_data_row + 1):
            ws.cell(row=r, column=1).font = mono
            tgt_cell = ws.cell(row=r, column=2)
            tgt_cell.font = link
            if base_url and tgt_cell.value:
                tgt_cell.hyperlink = f"{base_url}/browse/{tgt_cell.value}"
            ws.cell(row=r, column=3).font = body
            ws.cell(row=r, column=4).font = body
            ws.cell(row=r, column=5).font = body

        ws.auto_filter.ref = f"A{header_row}:E{last_data_row}"

    # Reasonable widths (skip _rpt_autosize on this sheet — it'd scan every
    # cell, which is O(rows * cols)).
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 80
    ws.freeze_panes = f"A{header_row + 1}"


def _build_exceptions_sheet(wb, data: Dict[str, Any], styles) -> None:
    ws = wb.create_sheet("Exceptions")
    ws.sheet_view.showGridLines = False
    row = _rpt_title(
        ws,
        "Exceptions and follow-ups",
        "Items that need a human eye. Empty sections below mean nothing was flagged.",
        styles, ncols=4,
    )

    sections = [
        ("Issues missing sprint mapping", data.get("missing_sprint_map", []), ["issue", "unmapped_source_sprint_ids"]),
        ("Issues spanning multiple closed sprints (skipped)", data.get("multi_closed_issues", []), ["issue", "target_sprint_ids"]),
        ("Sprint-update failures", data.get("update_sprint_failures", []), ["issue", "phase", "error"]),
        ("Field-copy failures", data.get("copy_field_failures", []), ["target", "source", "error"]),
        ("Sprint membership mismatches", data.get("sprint_mismatches", []),
            ["source_sprint_id", "target_sprint_id", "source_issue_count", "target_issue_count", "difference"]),
    ]
    for section_title, rows, cols in sections:
        ws.cell(row=row, column=1, value=section_title).font = _rpt_font(styles, bold=True, size=12)
        row += 1
        if not rows:
            ws.cell(row=row, column=1, value="None — no items flagged.").font = _rpt_font(styles, italic=True, color="595959")
            row += 2
            continue
        _rpt_write_header_row(ws, row, cols, styles)
        row += 1
        rows_data = [[r.get(c, "") for c in cols] for r in rows]
        row = _rpt_write_data_rows(ws, row, rows_data, styles)
        row += 1
    _rpt_autosize(ws)
    ws.freeze_panes = "A5"


def _build_methodology_sheet(wb, data: Dict[str, Any], styles) -> None:
    ws = wb.create_sheet("Methodology")
    ws.sheet_view.showGridLines = False
    row = _rpt_title(ws, "Methodology and known caveats", "Read this if anything in the other sheets surprises you.", styles, ncols=2)

    sections = [
        ("What this migration moved",
         "All issues in the source project were bulk-moved to the target project using Jira's native bulk-move "
         "operation. Issue history (status changes, comments, attachments, worklogs) travels with the move. Project "
         "configuration (workflows, screens, permissions) was rebuilt fresh on the target side from a CMP template "
         "and may not be byte-identical to the source TMP configuration."),
        ("How the counts in this report were obtained",
         "Source totals come from a baseline snapshot taken before the move. Target totals come from a live scan "
         "of the target project at report-generation time. The bulk-move operation is the only state change "
         "between those two measurements aside from any normal edits made by the team."),
        ("Why some sprints show a 'recent' completeDate",
         "Jira does not allow setting a sprint's completeDate manually. To re-attach historical sprint membership "
         "onto migrated issues, each closed sprint had to be temporarily reopened, the issue updates applied, then "
         "re-closed. Re-closing stamps a new completeDate of 'now'. The original close timestamp is unrecoverable "
         "but the sprint name, dates, and goal are preserved. Reports built off Velocity or Sprint Burndown will "
         "behave normally going forward; historical Velocity may need to be recomputed."),
        ("How to use the Issue Key Map",
         "The Issue Key Map sheet is the authoritative reference for where each migrated ticket landed. It contains "
         "every target issue that carries a stamped source key — i.e. every issue that came from the source project. "
         "Use the column filters in the header to narrow by type or status; use Ctrl+F to look up a specific source "
         "or target key. If you bookmarked or linked to a source-project URL, the Source key column tells you the "
         "new target key to replace it with."),
        ("Status definitions",
         "OK = counts match exactly. REVIEW = target is within 5% of source — expected for queries that depend on "
         "fields with eventual consistency (sprint, fixVersion). ATTENTION = larger gap; check the Exceptions sheet."),
    ]
    for title, body in sections:
        ws.cell(row=row, column=1, value=title).font = _rpt_font(styles, bold=True, size=12, color="1F4E78")
        row += 1
        ws.cell(row=row, column=1, value=body).font = styles["body_font"]
        ws.cell(row=row, column=1).alignment = styles["left"]
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        ws.row_dimensions[row].height = 60
        row += 2

    ws.column_dimensions["A"].width = 110


def _gather_report_data(client: JiraClient, cfg: Config) -> Dict[str, Any]:
    """Collect all data needed for the workbook. Sources:
       - extract/ snapshot
       - build/ mapping CSVs
       - update_sprints/ logs
       - validate/ outputs (optional, regenerated if missing)
       - live re-query for target inventory + sprint counts
    """
    ex = cfg.output_dir / "extract"
    bd = cfg.output_dir / "build"
    us = cfg.output_dir / "update_sprints"
    vd = cfg.output_dir / "validate"

    baseline = read_json(ex / "11_inventory_baseline.json", default={})
    if not baseline:
        raise SystemExit("Baseline missing — run 'extract' first.")
    source_meta = read_json(ex / "01_project_meta.json", default={})
    source_versions = read_json(ex / "04_versions.json", default=[])
    source_components = read_json(ex / "05_components.json", default=[])
    source_sprints = read_json(ex / "07_sprints.json", default=[])

    sprint_mapping = read_csv_rows(bd / "sprint_id_mapping.csv")
    version_mapping = read_csv_rows(bd / "version_id_mapping.csv")
    component_mapping = read_csv_rows(bd / "component_id_mapping.csv")

    missing_sprint_map = read_csv_rows(us / "missing_sprint_map.csv")
    multi_closed_issues = read_csv_rows(us / "multi_closed_issues.csv")
    update_sprint_failures = read_csv_rows(us / "failures.csv")
    sprint_cycle_log_rows = read_csv_rows(us / "sprint_cycle_log.csv")
    copy_field_failures = read_csv_rows(cfg.output_dir / "copy_fields" / "failures.csv")
    sprint_mismatches = read_json(vd / "sprint_mismatches.json", default=[])
    target_inv = read_json(vd / "target_inventory.json", default={})

    # Re-query target if validate hasn't been run (or to refresh)
    if not target_inv or not target_inv.get("total"):
        logging.info("Validate output not found — scanning target project live for inventory...")
        agg = extract_issues_aggregate(client, cfg.target_key)
        buckets = extract_boolean_buckets(client, cfg.target_key)
        target_inv = {
            "projectKey": cfg.target_key,
            "total": agg["total"],
            "byType": agg["byType"],
            "byStatus": agg["byStatus"],
            "byResolution": agg["byResolution"],
            **buckets,
        }

    # Target versions / components / project meta (live)
    target_versions = client.get(f"/rest/api/3/project/{cfg.target_key}/versions") or []
    target_components = client.get(f"/rest/api/3/project/{cfg.target_key}/components") or []
    try:
        target_proj = client.project(cfg.target_key)
        target_name = target_proj.get("name")
    except Exception:
        target_name = None

    # Sprint counts (per source and target sprint id)
    src_counts: Dict[str, int] = {}
    tgt_counts: Dict[str, int] = {}
    if sprint_mapping:
        logging.info("Counting issues per sprint (source + target)...")
        for m in sprint_mapping:
            if m.get("action") not in ("created", "existing"):
                continue
            src_id = m["source_id"]
            tgt_id = m["target_id"]
            try:
                src_counts[src_id] = client.approximate_count(
                    f'project = "{cfg.source_key}" AND sprint = {src_id}'
                )
            except Exception as e:
                logging.warning("Could not count source sprint %s: %s", src_id, e)
                src_counts[src_id] = 0
            try:
                tgt_counts[tgt_id] = client.approximate_count(
                    f'project = "{cfg.target_key}" AND sprint = {tgt_id}'
                )
            except Exception as e:
                logging.warning("Could not count target sprint %s: %s", tgt_id, e)
                tgt_counts[tgt_id] = 0

    # Full issue key map — every target issue that carries a stamped source key.
    # This sheet replaces the migration log as the single source of truth that
    # the project team can reference for "where did my ticket go?"
    issue_key_pairs: List[Dict[str, Any]] = []
    if cfg.key_field_id:
        expected = client.approximate_count(
            f'project = "{cfg.target_key}" AND "{cfg.key_field_id}" IS NOT EMPTY'
        )
        logging.info("Building full issue key map (~%d issues expected)...", expected)
        for issue in client.search_jql(
            f'project = "{cfg.target_key}" AND "{cfg.key_field_id}" IS NOT EMPTY ORDER BY created ASC',
            fields=[cfg.key_field_id, "summary", "status", "issuetype"],
        ):
            f = issue["fields"]
            issue_key_pairs.append({
                "source": f.get(cfg.key_field_id),
                "target": issue["key"],
                "summary": f.get("summary"),
                "issuetype": (f.get("issuetype") or {}).get("name", ""),
                "status": (f.get("status") or {}).get("name", ""),
            })
            if len(issue_key_pairs) % 1000 == 0:
                logging.info("  %d / ~%d issues mapped", len(issue_key_pairs), expected)
        logging.info("Issue key map complete: %d entries", len(issue_key_pairs))

    # Callouts: short list of "things worth noticing"
    callouts: List[Dict[str, str]] = []
    if any(c.get("reclosed") in ("False", False) for c in sprint_cycle_log_rows):
        callouts.append({
            "topic": "Sprint left active",
            "detail": "One or more closed sprints could not be re-closed automatically. See the Summary sheet's sprint cycle table.",
        })
    if multi_closed_issues:
        callouts.append({
            "topic": "Multi-closed-sprint issues skipped",
            "detail": f"{len(multi_closed_issues)} issue(s) span two or more closed sprints and were not updated. See Exceptions sheet.",
        })
    if missing_sprint_map:
        callouts.append({
            "topic": "Unmapped source sprints",
            "detail": f"{len(missing_sprint_map)} issue(s) referenced source sprints that have no target mapping. See Exceptions sheet.",
        })
    if any(int(r.get("source_count", 0) or 0) != int(target_inv.get("total", 0) or 0) for r in []):
        pass  # placeholder for future checks
    if baseline.get("total") != target_inv.get("total"):
        callouts.append({
            "topic": "Total issue count differs",
            "detail": f"Source had {baseline.get('total'):,} issues; target now has {target_inv.get('total'):,}. "
                      "Check Issue Counts sheet for breakdown.",
        })

    return {
        "target_key": cfg.target_key,
        "target_name": target_name,
        "base_url": cfg.base_url,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "baseline": baseline,
        "target_inv": target_inv,
        "source_meta": source_meta,
        "source_versions": source_versions,
        "source_components": source_components,
        "source_sprints": source_sprints,
        "target_versions": target_versions,
        "target_components": target_components,
        "sprint_mapping": sprint_mapping,
        "version_mapping": version_mapping,
        "component_mapping": component_mapping,
        "sprint_counts": {"src": src_counts, "tgt": tgt_counts},
        "missing_sprint_map": missing_sprint_map,
        "multi_closed_issues": multi_closed_issues,
        "update_sprint_failures": update_sprint_failures,
        "sprint_cycle_log": sprint_cycle_log_rows,
        "copy_field_failures": copy_field_failures,
        "sprint_mismatches": sprint_mismatches,
        "issue_key_pairs": issue_key_pairs,
        "callouts": callouts,
    }


def cmd_report(cfg: Config) -> None:
    """Generate the post-migration Excel report for the project team.

    Reads from extract/, build/, update_sprints/, and (optionally) validate/.
    Re-queries the target project for live inventory + sprint counts + issue
    key sample, so the report reflects current state rather than just the
    point-in-time snapshot.
    """
    if not cfg.source_key or not cfg.target_key:
        raise SystemExit("--source-key and --target-key are required for report.")
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, source_key=cfg.source_key, target_key=cfg.target_key,
              source_must_exist=False, target_must_exist=True)
    data = _gather_report_data(client, cfg)
    out_path = _build_report_workbook(cfg, data)
    logging.info("REPORT written: %s", out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Jira TMP Migration Toolkit")
    p.add_argument("--base-url", help="or JIRA_BASE_URL")
    p.add_argument("--email", help="or JIRA_EMAIL")
    p.add_argument("--output-dir", default="./out", help="or JIRA_OUTPUT_DIR")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("doctor")
    pd.add_argument("--source-key", help="or JIRA_SOURCE_KEY")
    pd.add_argument("--target-key", help="or JIRA_TARGET_KEY")
    pd.add_argument("--key-field", help="or JIRA_KEY_FIELD")

    pe = sub.add_parser("extract")
    pe.add_argument("--source-key", required=False)

    ps = sub.add_parser("stamp-keys")
    ps.add_argument("--source-key", required=False)
    ps.add_argument("--key-field")
    ps.add_argument("--dry-run", action="store_true")

    pc = sub.add_parser("copy-fields")
    pc.add_argument("--source-key", required=False)
    pc.add_argument("--target-key", required=False)
    pc.add_argument("--key-field")
    pc.add_argument("--dry-run", action="store_true")

    pb = sub.add_parser("build")
    pb.add_argument("--target-key", required=False)
    pb.add_argument("--target-board-id", type=int)
    pb.add_argument("--create-target", action="store_true")
    pb.add_argument("--target-lead")
    pb.add_argument("--target-template")
    pb.add_argument("--dry-run", action="store_true")

    pu = sub.add_parser("update-sprints", help="Update sprint field with mapped sprint IDs after bulk move.")
    pu.add_argument("--target-key", required=False)
    pu.add_argument("--dry-run", action="store_true")

    px = sub.add_parser("export-sprint-csv", help="Emit a semicolon-delimited CSV for Jira's External Import to set sprint membership by id.")
    px.add_argument("--target-key", required=False)

    pv = sub.add_parser("validate")
    pv.add_argument("--target-key", required=False)
    pv.add_argument("--key-field")

    pr = sub.add_parser("report", help="Generate post-migration Excel report for the project team.")
    pr.add_argument("--source-key", required=False)
    pr.add_argument("--target-key", required=False)
    pr.add_argument("--key-field")

    return p

COMMANDS = {
    "doctor": cmd_doctor,
    "extract": cmd_extract,
    "stamp-keys": cmd_stamp_keys,
    "copy-fields": cmd_copy_fields,
    "build": cmd_build,
    "update-sprints": cmd_update_sprints,
    "export-sprint-csv": cmd_export_sprint_csv,
    "validate": cmd_validate,
    "report": cmd_report,
}

def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(Path(args.output_dir).resolve(), args.verbose)
    cfg = Config.from_args(args)
    try:
        COMMANDS[args.cmd](cfg)
    except SystemExit:
        raise
    except Exception:
        logging.exception("Unhandled error")
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())