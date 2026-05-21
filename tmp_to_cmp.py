#!/usr/bin/env python3
"""
Jira TMP -> CMP Migration Toolkit (Enhanced with full field catalog using /editmeta)

Subcommands:
  doctor         Verify auth + project access.
  extract        Pull source TMP config + full field catalog (with options).
  stamp-keys     Write source keys into a custom text field.
  copy-fields    Copy field values from source to target using mapping CSV.
  build          Recreate versions/components/sprints in target.
  validate       Compare baseline counts after move.

Uses /rest/api/3/issue/{key}/editmeta to retrieve allowed values for select fields.
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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

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

    def project_search(self, query: str) -> Dict:
        return self.get("/rest/api/3/project/search", params={"query": query}) or {}

    def all_fields(self) -> List[Dict]:
        return self.get("/rest/api/3/field") or []

    def editmeta(self, issue_key: str) -> Dict:
        """Get edit metadata for an issue, including allowedValues for fields."""
        return self.get(f"/rest/api/3/issue/{issue_key}/editmeta") or {}

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
        """Paginate using POST /rest/api/3/search/jql (supports nextPageToken)."""
        body = {"jql": jql, "fields": fields, "maxResults": max_results, "fieldsByKeys": False}
        token = None
        while True:
            if token:
                body["nextPageToken"] = token
            payload = self.post("/rest/api/3/search/jql", body=body) or {}
            for issue in payload.get("issues", []):
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
        all_fields = self.client.all_fields()
        scoped = []
        for f in all_fields:
            scope = f.get("scope")
            if scope and scope.get("type") == "PROJECT" and str(scope.get("project", {}).get("id")) == str(self.project_id):
                scoped.append(f)
        return scoped

    def get_issue_types(self) -> List[Dict[str, Any]]:
        """Retrieve issue types used in the project via a sample issue search."""
        # Fetch one issue per issue type by scanning a few issues
        types_seen = {}
        for issue in self.client.search_jql(f'project = "{self.project_key}"', fields=["issuetype"], max_results=100):
            it = issue["fields"]["issuetype"]
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
        # Map field id -> catalog entry
        catalog = {}
        for f in scoped_fields:
            fid = f["id"]
            catalog[fid] = {
                "fieldId": fid,
                "name": f.get("name"),
                "schemaType": f.get("schema", {}).get("type"),
                "custom": f.get("custom", False),
                "required": f.get("required", False),
                "allowedValues": [],  # will fill later
            }

        # For each issue type, fetch editmeta on a sample issue to get allowedValues
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
                        if allowed:
                            # Merge allowed values (avoid duplicates across types)
                            existing = catalog[fid]["allowedValues"]
                            if isinstance(allowed, list):
                                # Store as list of value strings or objects
                                existing_values = {str(v.get("value", v)) for v in existing}
                                for v in allowed:
                                    val_str = str(v.get("value", v))
                                    if val_str not in existing_values:
                                        existing.append(v)
                                        existing_values.add(val_str)
                            catalog[fid]["allowedValues"] = existing
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
        _check_project(client, source_key, "source", me, source_must_exist)
    if target_key:
        _check_project(client, target_key, "target", me, target_must_exist)
    logging.info(SEP)
    return me

def _check_project(client: JiraClient, key: str, label: str, me: Dict, must_exist: bool) -> Optional[Dict]:
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
        it = issue["fields"]["issuetype"]
        st = issue["fields"]["status"]
        if it["id"] not in types:
            types[it["id"]] = {"id": it["id"], "name": it["name"], "subtask": it.get("subtask", False)}
        pair = f"{it['name']}::{st['name']}"
        if pair not in type_status_pairs:
            type_status_pairs[pair] = {"issueType": it["name"], "statusName": st["name"], "statusId": st["id"]}
        by_type[it["name"]] = by_type.get(it["name"], 0) + 1
        by_status[st["name"]] = by_status.get(st["name"], 0) + 1
        res = issue["fields"].get("resolution")
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

    # 1. Project meta
    write_json(out / "01_project_meta.json", extract_project_meta(client, cfg.source_key))

    # 2. Issues aggregate (types/statuses)
    agg = extract_issues_aggregate(client, cfg.source_key)
    write_json(out / "02_issue_types.json", agg["types"])
    write_json(out / "03_statuses.json", agg["statuses"])

    # 3. Versions & Components
    write_json(out / "04_versions.json", extract_versions(client, cfg.source_key))
    write_json(out / "05_components.json", extract_components(client, cfg.source_key))

    # 4. Board & Sprints
    board = find_board(client, cfg.source_key)
    if board:
        write_json(out / "06_source_board.json", board)
        write_json(out / "07_sprints.json", extract_sprints(client, board["id"]))
        write_json(out / "08_source_board_config.json", client.get(f"/rest/agile/1.0/board/{board['id']}/configuration"))

    # 5. Roles
    role_map = client.get(f"/rest/api/3/project/{cfg.source_key}/role") or {}
    write_json(out / "09_roles.json", role_map)

    # 6. Boolean buckets
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

    # 7. Full field catalog (using ProjectInspector)
    logging.info("Building field catalog (scoped fields + editmeta options)...")
    inspector = ProjectInspector(client, cfg.source_key)
    catalog = inspector.build_field_catalog()
    write_json(out / "12_source_field_catalog.json", catalog)
    # Also save as CSV for easier viewing
    csv_rows = []
    for fid, info in catalog.items():
        csv_rows.append({
            "fieldId": fid,
            "name": info["name"],
            "schemaType": info["schemaType"],
            "custom": info["custom"],
            "required": info["required"],
            "allowedValues": json.dumps(info["allowedValues"]) if info["allowedValues"] else "",
        })
    write_csv(out / "12_source_field_catalog.csv", csv_rows, ["fieldId", "name", "schemaType", "custom", "required", "allowedValues"])

    logging.info("EXTRACT complete. Total issues: %d. Catalog has %d fields.", agg["total"], len(catalog))

# ---------------------------------------------------------------------------
# Helper IO
# ---------------------------------------------------------------------------
def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    logging.info("Wrote %s", path)

def write_csv(path: Path, rows: List[Dict], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logging.info("Wrote %s (%d rows)", path, len(rows))

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def confirm(prompt: str, dry_run: bool) -> bool:
    if dry_run:
        logging.info("[DRY-RUN] %s", prompt)
        return False
    return input(prompt + " [y/N]: ").strip().lower() == "y"

# ---------------------------------------------------------------------------
# STAMP-KEYS
# ---------------------------------------------------------------------------
def precheck_key_field(client: JiraClient, field_id: str) -> None:
    fields = client.all_fields()
    found = next((f for f in fields if f["id"] == field_id), None)
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
# COPY-FIELDS (simplified version)
# ---------------------------------------------------------------------------
def cmd_copy_fields(cfg: Config) -> None:
    mapping_file = cfg.output_dir / "mapping" / "field_mapping.csv"
    if not mapping_file.exists():
        raise SystemExit(f"Mapping file not found: {mapping_file}\nCreate CSV with columns: source_field_id,target_field_id,transform")
    mappings = []
    with mapping_file.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("source_field_id") and row.get("target_field_id"):
                mappings.append(row)

    client_src = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    client_tgt = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client_src, source_key=cfg.source_key, source_must_exist=True)
    preflight(client_tgt, target_key=cfg.target_key, target_must_exist=True)

    out = cfg.output_dir / "copy_fields"
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / "copied.txt"
    done = set(checkpoint.read_text().splitlines()) if checkpoint.exists() else set()

    target_issues = list(client_tgt.search_jql(f'project = "{cfg.target_key}"', fields=[cfg.key_field_id]))
    total = len(target_issues)
    logging.info("Found %d target issues", total)

    failures = []
    processed = 0
    for issue in target_issues:
        target_key = issue["key"]
        if target_key in done:
            continue
        stamped = issue["fields"].get(cfg.key_field_id)
        if not stamped:
            logging.warning("No stamped source key on %s, skipping", target_key)
            continue
        try:
            source_issue = client_src.issue(stamped, fields=[m["source_field_id"] for m in mappings])
        except Exception:
            failures.append({"target": target_key, "source": stamped, "error": "Source not found"})
            continue

        update = {}
        for m in mappings:
            src_val = source_issue.get("fields", {}).get(m["source_field_id"])
            if src_val is not None:
                transform = m.get("transform", "copy").lower()
                if transform == "copy":
                    update[m["target_field_id"]] = src_val
                elif transform == "append_prefix":
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
                client_tgt.put(f"/rest/api/3/issue/{target_key}", body={"fields": update}, params={"notifyUsers": "false"})
                with checkpoint.open("a") as ckpt:
                    ckpt.write(target_key + "\n")
                time.sleep(SLEEP_BETWEEN_WRITES)
            if processed % 50 == 0:
                logging.info("Copied fields for %d / %d", processed, total)
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
            client.post(f"/rest/agile/1.0/sprint/{target_id}", body={"state": "active"})
            target_state = "active"
        elif s["state"] == "closed":
            client.post(f"/rest/agile/1.0/sprint/{target_id}", body={"state": "active"})
            client.post(f"/rest/agile/1.0/sprint/{target_id}", body={"state": "closed"})
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
# VALIDATE
# ---------------------------------------------------------------------------
def cmd_validate(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key, target_must_exist=True)
    baseline = read_json(cfg.output_dir / "extract" / "11_inventory_baseline.json")
    agg = extract_issues_aggregate(client, cfg.target_key)
    buckets = extract_boolean_buckets(client, cfg.target_key)
    target_inv = {"projectKey": cfg.target_key, "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "total": agg["total"], "byType": agg["byType"], "byStatus": agg["byStatus"],
                  "byResolution": agg["byResolution"], **buckets}
    out = cfg.output_dir / "validate"
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "target_inventory.json", target_inv)
    delta = {"total": _diff(baseline["total"], target_inv["total"]),
             "withAttachments": _diff(baseline["withAttachments"], target_inv["withAttachments"]),
             "withComments": _diff(baseline["withComments"], target_inv["withComments"]),
             "withFixVersion": _diff(baseline["withFixVersion"], target_inv["withFixVersion"]),
             "withComponents": _diff(baseline["withComponents"], target_inv["withComponents"]),
             "withSprint": _diff(baseline["withSprint"], target_inv["withSprint"])}
    write_json(out / "validation_delta.json", delta)
    non_zero = [k for k in delta if delta[k]["diff"] != 0]
    if non_zero:
        logging.warning("Validation differences: %s", non_zero)
    else:
        logging.info("Validation: all counts match.")
    logging.info("VALIDATE complete.")

def _diff(src: int, tgt: int) -> Dict[str, int]:
    return {"source": src, "target": tgt, "diff": tgt - src}

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
    logging.info("Doctor done.")

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

    pv = sub.add_parser("validate")
    pv.add_argument("--target-key", required=False)

    return p

COMMANDS = {
    "doctor": cmd_doctor,
    "extract": cmd_extract,
    "stamp-keys": cmd_stamp_keys,
    "copy-fields": cmd_copy_fields,
    "build": cmd_build,
    "validate": cmd_validate,
}

def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(Path(args.output_dir).resolve(), args.verbose)
    cfg = Config.from_args(args)
    try:
        COMMANDS[args.cmd](cfg)
    except SystemExit:
        raise
    except Exception as e:
        logging.exception("Unhandled error")
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())