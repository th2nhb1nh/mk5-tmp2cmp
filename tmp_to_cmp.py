#!/usr/bin/env python3
"""
Jira TMP -> CMP Migration Toolkit (Phase 3: extract & rebuild)

Subcommands:
  doctor         Verify auth + project access. Run this first.
  extract        Pull source TMP project config + inventory + field catalog.
  stamp-keys     Write each source issue's current key into a designated text
                 custom field (e.g. customfield_25199). Survives bulk move.
  copy-fields    Copy field values from source to target issues using mapping.
  build          Recreate versions, components, and sprints in the target CMP.
  validate       Compare source baseline counts against target post-move counts.

Auth: Atlassian Cloud API token + email (HTTP Basic).
Idempotent. Every phase can be re-run; checkpoints + name-based dedup ensure
nothing is duplicated.
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
from typing import Any, Dict, Iterable, List, Optional, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Load .env if python-dotenv is available
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
        base = (getattr(args, "base_url", "") or 
                os.environ.get("JIRA_BASE_URL", "")).rstrip("/")
        email = (getattr(args, "email", "") or 
                 os.environ.get("JIRA_EMAIL", ""))
        token = os.environ.get("JIRA_API_TOKEN", "")
        
        missing = []
        if not base: missing.append("JIRA_BASE_URL")
        if not email: missing.append("JIRA_EMAIL")
        if not token: missing.append("JIRA_API_TOKEN")
        if missing:
            raise SystemExit(
                f"\nMissing credentials: {', '.join(missing)}\n"
                f"Set them in .env file or pass via CLI.\n"
            )
        
        output_dir = Path(getattr(args, "output_dir", None) or 
                         os.environ.get("JIRA_OUTPUT_DIR", "./out")).resolve()
        source_key = getattr(args, "source_key", "") or os.environ.get("JIRA_SOURCE_KEY", "")
        target_key = getattr(args, "target_key", "") or os.environ.get("JIRA_TARGET_KEY", "")
        key_field_id = (getattr(args, "key_field", None) or 
                       os.environ.get("JIRA_KEY_FIELD", "customfield_25199"))
        
        return cls(
            base_url=base,
            email=email,
            api_token=token,
            source_key=source_key,
            target_key=target_key,
            key_field_id=key_field_id,
            output_dir=output_dir,
            dry_run=getattr(args, "dry_run", False),
            create_target=getattr(args, "create_target", False),
            target_board_id=(getattr(args, "target_board_id", None) or
                            _to_int_or_none(os.environ.get("JIRA_TARGET_BOARD_ID"))),
            target_lead_account_id=(getattr(args, "target_lead", None) or
                                   os.environ.get("JIRA_TARGET_LEAD")),
            target_template=(getattr(args, "target_template", None) or
                           os.environ.get("JIRA_TARGET_TEMPLATE", 
                                        "com.pyxis.greenhopper.jira:gh-simplified-scrum-classic")),
        )

def _to_int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value else None
    except (ValueError, TypeError):
        return None

# ---------------------------------------------------------------------------
# Jira REST client (unchanged except for added methods for field values)
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
        retry = Retry(total=6, backoff_factor=1.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST", "PUT", "DELETE"],
                      respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _req(self, method: str, path: str, **kwargs) -> requests.Response:
        original = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        resp = self.session.request(method, original, **kwargs)
        if resp.status_code >= 400:
            final = resp.url
            redirect_note = f" (redirected to {final})" if final != original else ""
            req_body = ""
            if kwargs.get("json") is not None:
                try:
                    req_body = " body=" + json.dumps(kwargs["json"])[:400]
                except Exception:
                    pass
            logging.error("%s %s%s -> %s%s: %s",
                          method, original, redirect_note,
                          resp.status_code, req_body, (resp.text or "")[:500])
            resp.raise_for_status()
        return resp

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._req("GET", path, params=params)
        return r.json() if r.content else None

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._req("POST", path, json=body or {}, params=params)
        return r.json() if r.content else None

    def put(self, path: str, body: Optional[Dict[str, Any]] = None,
            params: Optional[Dict[str, Any]] = None) -> Any:
        r = self._req("PUT", path, json=body or {}, params=params)
        return r.json() if r.content else None

    def myself(self) -> Dict[str, Any]:
        return self.get("/rest/api/3/myself")

    def project(self, key: str) -> Dict[str, Any]:
        return self.get(f"/rest/api/3/project/{key}",
                        params={"expand": "description,lead"})

    def project_search(self, query: str) -> Dict[str, Any]:
        return self.get("/rest/api/3/project/search", params={"query": query}) or {}

    def fields(self) -> List[Dict[str, Any]]:
        return self.get("/rest/api/3/field") or []

    def paged(self, path: str, params: Optional[Dict[str, Any]] = None,
              values_key: str = "values", page_size: int = PAGE_SIZE_GENERIC) -> Iterable[Dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("maxResults", page_size)
        start = 0
        while True:
            params["startAt"] = start
            payload = self.get(path, params=params) or {}
            items = payload.get(values_key, payload if isinstance(payload, list) else [])
            for item in items:
                yield item
            if not items:
                break
            start += len(items)
            if payload.get("isLast") is True:
                break
            total = payload.get("total")
            if total is not None and start >= total:
                break

    def search_jql(self, jql: str, fields: List[str],
                   expand: Optional[str] = None,
                   fields_by_keys: bool = False,
                   properties: Optional[List[str]] = None
                   ) -> Iterable[Dict[str, Any]]:
        if isinstance(expand, (list, tuple)):
            expand = ",".join(expand)
        body_base: Dict[str, Any] = {
            "jql": jql,
            "fields": list(fields),
            "maxResults": PAGE_SIZE_SEARCH,
            "fieldsByKeys": bool(fields_by_keys),
        }
        if expand:
            body_base["expand"] = expand
        if properties:
            body_base["properties"] = list(properties)
        token: Optional[str] = None
        while True:
            body = dict(body_base)
            if token:
                body["nextPageToken"] = token
            payload = self.post("/rest/api/3/search/jql", body=body) or {}
            for issue in payload.get("issues", []):
                yield issue
            if payload.get("isLast", True) or not payload.get("nextPageToken"):
                break
            token = payload["nextPageToken"]

    def approximate_count(self, jql: str) -> int:
        payload = self.post("/rest/api/3/search/approximate-count", body={"jql": jql})
        return int((payload or {}).get("count", 0))

    # === ADDED: Helper to get field allowed values (for select lists etc.) ===
    def get_field_contexts(self, field_id: str) -> List[Dict[str, Any]]:
        """Get custom field contexts with options."""
        return list(self.paged(f"/rest/api/3/field/{field_id}/context", page_size=50))

    def get_field_options(self, field_id: str, context_id: int) -> List[Dict[str, Any]]:
        return list(self.paged(f"/rest/api/3/field/{field_id}/context/{context_id}/option", page_size=100))

# ---------------------------------------------------------------------------
# Preflight (unchanged)
# ---------------------------------------------------------------------------
def preflight(client: JiraClient, source_key: str = "", target_key: str = "",
              source_must_exist: bool = True, target_must_exist: bool = False) -> Dict[str, Any]:
    logging.info(SEP)
    logging.info("Preflight")
    logging.info(SEP)
    try:
        me = client.myself()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        raise SystemExit(_auth_error_message(code, client.base_url))
    except requests.RequestException as e:
        raise SystemExit(f"\nCould not reach {client.base_url}: {e}\n")
    logging.info("Authentication successful")
    logging.info("  User:      %s", me.get("displayName"))
    logging.info("  Email:     %s", me.get("emailAddress"))
    logging.info("  AccountId: %s", me.get("accountId"))
    if source_key:
        _check_project(client, source_key, "source", me, must_exist=source_must_exist)
    if target_key:
        _check_project(client, target_key, "target", me, must_exist=target_must_exist)
    logging.info(SEP)
    return me

def _check_project(client: JiraClient, key: str, label: str,
                   me: Dict[str, Any], must_exist: bool) -> Optional[Dict[str, Any]]:
    try:
        proj = client.project(key)
    except requests.HTTPError as e:
        if e.response and e.response.status_code == 404:
            if not must_exist:
                logging.info("%s project '%s' does not exist yet (OK).", label.capitalize(), key)
                return None
            hint = _disambiguate_404(client, key)
            raise SystemExit(_project_error_message(label, key, hint, me, client.base_url))
        raise
    style = proj.get("style")
    warn = ""
    if label == "source" and style != "next-gen":
        warn = "  WARNING: source style is not 'next-gen' (not a TMP)."
    logging.info("%s project '%s' OK", label.capitalize(), key)
    logging.info("  Name:  %s", proj.get("name"))
    logging.info("  Style: %s", style)
    if warn:
        logging.warning(warn)
    return proj

def _disambiguate_404(client: JiraClient, key: str) -> str:
    try:
        results = client.project_search(key)
        values = results.get("values", [])
        case_matches = [p for p in values if p["key"].upper() == key.upper()]
        if case_matches:
            actual = case_matches[0]["key"]
            if actual != key:
                return f"Project key case mismatch. Use '{actual}', not '{key}'."
            return "Project EXISTS but user lacks BROWSE permission."
        sample = ", ".join(p["key"] for p in values[:5]) or "(none)"
        return f"No project with key '{key}' visible. Accessible keys: {sample}"
    except Exception as ex:
        return f"Could not disambiguate: {ex}"

def _auth_error_message(code: Any, base_url: str) -> str:
    return f"\n{SEP}\nAUTHENTICATION FAILED ({code})\n{SEP}\n  Site: {base_url}\n"

def _project_error_message(label: str, key: str, hint: str,
                           me: Dict[str, Any], base_url: str) -> str:
    return f"\n{SEP}\n{label.upper()} PROJECT '{key}' NOT ACCESSIBLE\n{SEP}\n  {hint}\n"

# ---------------------------------------------------------------------------
# IO helpers (unchanged)
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
        writer.writerows(rows)
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
# EXTRACT phase (existing + extended field catalog)
# ---------------------------------------------------------------------------
def extract_project_meta(client: JiraClient, key: str) -> Dict[str, Any]:
    proj = client.project(key)
    return {
        "id": proj["id"], "key": proj["key"], "name": proj["name"],
        "projectTypeKey": proj.get("projectTypeKey"), "style": proj.get("style"),
        "description": proj.get("description"),
        "leadAccountId": (proj.get("lead") or {}).get("accountId"),
        "leadDisplayName": (proj.get("lead") or {}).get("displayName"),
        "assigneeType": proj.get("assigneeType"),
    }

def extract_issues_aggregate(client: JiraClient, key: str) -> Dict[str, Any]:
    types: Dict[str, Dict[str, Any]] = {}
    type_status_pairs: Dict[str, Dict[str, Any]] = {}
    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_resolution: Dict[str, int] = {}
    total = 0
    fields = ["issuetype", "status", "resolution"]
    for issue in client.search_jql(f'project = "{key}"', fields=fields):
        total += 1
        it = issue["fields"]["issuetype"]
        st = issue["fields"]["status"]
        if it["id"] not in types:
            types[it["id"]] = {"id": it["id"], "name": it["name"],
                               "subtask": it.get("subtask", False),
                               "hierarchyLevel": it.get("hierarchyLevel")}
        pair_key = f"{it['name']}::{st['name']}"
        if pair_key not in type_status_pairs:
            type_status_pairs[pair_key] = {
                "issueType": it["name"], "statusName": st["name"],
                "statusId": st["id"],
                "statusCategory": (st.get("statusCategory") or {}).get("key"),
            }
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

def extract_boolean_buckets(client: JiraClient, key: str) -> Dict[str, int]:
    base = f'project = "{key}"'
    return {
        "withAttachments": client.approximate_count(f"{base} AND attachments is not EMPTY"),
        "withComments":    client.approximate_count(f"{base} AND comment is not EMPTY"),
        "withFixVersion":  client.approximate_count(f"{base} AND fixVersion is not EMPTY"),
        "withComponents":  client.approximate_count(f"{base} AND component is not EMPTY"),
        "withSprint":      client.approximate_count(f"{base} AND sprint is not EMPTY"),
    }

def extract_versions(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    versions = client.get(f"/rest/api/3/project/{key}/versions") or []
    return [{"sequence": i, "id": v["id"], "name": v["name"],
             "description": v.get("description", ""),
             "startDate": v.get("startDate", ""), "releaseDate": v.get("releaseDate", ""),
             "released": v.get("released", False), "archived": v.get("archived", False)}
            for i, v in enumerate(versions)]

def extract_components(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    comps = client.get(f"/rest/api/3/project/{key}/components") or []
    return [{"id": c["id"], "name": c["name"], "description": c.get("description", ""),
             "leadAccountId": (c.get("lead") or {}).get("accountId", ""),
             "assigneeType": c.get("assigneeType", "PROJECT_DEFAULT")}
            for c in comps]

def find_source_board(client: JiraClient, key: str) -> Optional[Dict[str, Any]]:
    boards = list(client.paged("/rest/agile/1.0/board",
                               params={"projectKeyOrId": key}, page_size=50))
    if not boards:
        return None
    scrum = [b for b in boards if b.get("type") == "scrum"]
    return (scrum or boards)[0]

def extract_sprints(client: JiraClient, board_id: int) -> List[Dict[str, Any]]:
    raw = list(client.paged(f"/rest/agile/1.0/board/{board_id}/sprint",
                            params={"state": "closed,active,future"}, page_size=50))
    order = {"closed": 0, "active": 1, "future": 2}
    raw.sort(key=lambda s: (order.get(s.get("state", "future"), 3),
                            s.get("completeDate") or s.get("startDate") or ""))
    return [{"id": s["id"], "name": s["name"], "state": s["state"],
             "startDate": s.get("startDate", ""), "endDate": s.get("endDate", ""),
             "completeDate": s.get("completeDate", ""), "goal": s.get("goal", ""),
             "originBoardId": s.get("originBoardId")} for s in raw]

def extract_roles(client: JiraClient, key: str) -> Dict[str, Any]:
    role_map = client.get(f"/rest/api/3/project/{key}/role") or {}
    out: Dict[str, Any] = {}
    for role_name, role_url in role_map.items():
        detail = client.get(role_url) or {}
        out[role_name] = {"id": detail.get("id"),
            "actors": [{"displayName": a.get("displayName"), "type": a.get("type"),
                        "accountId": (a.get("actorUser") or {}).get("accountId"),
                        "groupName": (a.get("actorGroup") or {}).get("name")}
                       for a in detail.get("actors", [])]}
    return out

def extract_custom_fields_in_use(client: JiraClient, key: str,
                                 sample_size: int = 500) -> List[Dict[str, Any]]:
    catalog = {f["id"]: f for f in client.fields()}
    seen: Dict[str, Dict[str, Any]] = {}
    scanned = 0
    for issue in client.search_jql(f'project = "{key}"', fields=["*all"], expand="names"):
        scanned += 1
        names = issue.get("names") or {}
        for fid, value in issue["fields"].items():
            if value in (None, "", [], {}):
                continue
            if not fid.startswith("customfield_") or fid in seen:
                continue
            cat = catalog.get(fid, {})
            seen[fid] = {
                "fieldId": fid,
                "displayName": names.get(fid) or cat.get("name", ""),
                "schemaType": (cat.get("schema") or {}).get("type", ""),
                "schemaCustom": (cat.get("schema") or {}).get("custom", ""),
                "exampleValue": str(value)[:200],
            }
        if scanned >= sample_size:
            break
    return list(seen.values())

# === ADDED: Comprehensive field catalog for source project ===
def extract_full_field_catalog(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    """
    Scan all issues (or a large sample) and collect every field that appears,
    including system fields. For each field, provide:
    - fieldId (e.g., summary, customfield_12345)
    - displayName (human readable)
    - schemaType (string, number, option, etc.)
    - custom (if custom field)
    - allowedValues (list of options for select/multi-select)
    - isRequired (if known, best effort)
    - sampleValue (first non-empty)
    """
    all_fields = {f["id"]: f for f in client.fields()}
    field_usage: Dict[str, Dict[str, Any]] = {}
    
    # We'll scan up to 1000 issues to capture most fields
    for issue in client.search_jql(f'project = "{key}"', fields=["*all"], expand="names"):
        names = issue.get("names") or {}
        for field_id, value in issue["fields"].items():
            if value in (None, "", [], {}):
                continue
            if field_id in field_usage:
                continue  # already captured
            
            meta = all_fields.get(field_id, {})
            schema = meta.get("schema", {})
            schema_type = schema.get("type", "unknown")
            is_custom = field_id.startswith("customfield_")
            
            # Try to fetch allowed values for select/radio/checkbox fields
            allowed_values = []
            if is_custom and schema_type in ("option", "option-with-child", "array"):
                try:
                    contexts = client.get_field_contexts(field_id)
                    for ctx in contexts[:1]:  # first context only for simplicity
                        opts = client.get_field_options(field_id, ctx["id"])
                        allowed_values = [{"value": o["value"], "disabled": o.get("disabled", False)} 
                                          for o in opts]
                except Exception as e:
                    logging.debug("Could not fetch options for %s: %s", field_id, e)
            
            field_usage[field_id] = {
                "fieldId": field_id,
                "displayName": names.get(field_id) or meta.get("name", field_id),
                "schemaType": schema_type,
                "custom": is_custom,
                "required": meta.get("required", False),
                "allowedValues": allowed_values,
                "sampleValue": str(value)[:500] if value else "",
            }
            if len(field_usage) % 50 == 0:
                logging.info("  cataloged %d distinct fields...", len(field_usage))
    
    return list(field_usage.values())

def cmd_extract(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, source_key=cfg.source_key, source_must_exist=True)
    out = cfg.output_dir / "extract"
    out.mkdir(parents=True, exist_ok=True)
    logging.info("EXTRACT: source=%s output=%s", cfg.source_key, out)

    # 1. Project metadata
    write_json(out / "01_project_meta.json", extract_project_meta(client, cfg.source_key))

    # 2+3+11: issues aggregate
    logging.info("Scanning all issues (single pass)...")
    agg = extract_issues_aggregate(client, cfg.source_key)
    write_json(out / "02_issue_types.json", agg["types"])
    write_csv(out / "02_issue_types.csv", agg["types"], ["id", "name", "subtask", "hierarchyLevel"])
    write_json(out / "03_statuses.json", agg["statuses"])
    write_csv(out / "03_statuses.csv", agg["statuses"], ["issueType", "statusName", "statusId", "statusCategory"])

    # 4. Versions
    versions = extract_versions(client, cfg.source_key)
    write_json(out / "04_versions.json", versions)
    write_csv(out / "04_versions.csv", versions, ["sequence", "id", "name", "description", "startDate", "releaseDate", "released", "archived"])

    # 5. Components
    components = extract_components(client, cfg.source_key)
    write_json(out / "05_components.json", components)
    write_csv(out / "05_components.csv", components, ["id", "name", "description", "leadAccountId", "assigneeType"])

    # 6-8. Board + sprints
    board = find_source_board(client, cfg.source_key)
    if board:
        write_json(out / "06_source_board.json", board)
        sprints = extract_sprints(client, board["id"])
        write_json(out / "07_sprints.json", sprints)
        write_csv(out / "07_sprints.csv", sprints, ["id", "name", "state", "startDate", "endDate", "completeDate", "goal", "originBoardId"])
        board_cfg = client.get(f"/rest/agile/1.0/board/{board['id']}/configuration")
        write_json(out / "08_source_board_config.json", board_cfg)
    else:
        logging.warning("No board on source; skipping sprint extract.")

    # 9. Roles
    write_json(out / "09_roles.json", extract_roles(client, cfg.source_key))

    # 10. Custom fields in use (lightweight)
    logging.info("Sampling custom fields in use...")
    fields_in_use = extract_custom_fields_in_use(client, cfg.source_key)
    write_json(out / "10_custom_fields_in_use.json", fields_in_use)
    write_csv(out / "10_custom_fields_in_use.csv", fields_in_use, ["fieldId", "displayName", "schemaType", "schemaCustom", "exampleValue"])

    # 11. Inventory baseline
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

    # === ADDED: 12. Full field catalog (including system fields and allowed values) ===
    logging.info("Building full field catalog (scanning issues for all fields)...")
    field_catalog = extract_full_field_catalog(client, cfg.source_key)
    write_json(out / "12_source_field_catalog.json", field_catalog)
    write_csv(out / "12_source_field_catalog.csv", field_catalog,
              ["fieldId", "displayName", "schemaType", "custom", "required", "allowedValues", "sampleValue"])

    logging.info("EXTRACT complete. %d issues scanned. Artifacts in %s", agg["total"], out)

# ---------------------------------------------------------------------------
# STAMP-KEYS phase (unchanged)
# ---------------------------------------------------------------------------
def precheck_key_field(client: JiraClient, field_id: str) -> Dict[str, Any]:
    found = next((f for f in client.fields() if f.get("id") == field_id), None)
    if not found:
        raise SystemExit(f"Field {field_id} not found.")
    schema_type = (found.get("schema") or {}).get("type")
    if schema_type not in ("string", "any"):
        raise SystemExit(f"Field {field_id} must be a text field (type string/any).")
    logging.info("Key field OK: %s = '%s'", field_id, found.get("name"))
    return found

def stamp_test_first_issue(client: JiraClient, source_key: str, field_id: str, dry_run: bool) -> str:
    first = next(client.search_jql(f'project = "{source_key}" ORDER BY created ASC', fields=["summary"]), None)
    if not first:
        raise SystemExit(f"No issues in {source_key}.")
    issue_key = first["key"]
    logging.info("Test stamp on %s", issue_key)
    if dry_run:
        logging.info("[DRY-RUN] Would stamp %s into %s", issue_key, field_id)
        return issue_key
    client.put(f"/rest/api/3/issue/{issue_key}", body={"fields": {field_id: issue_key}}, params={"notifyUsers": "false"})
    check = client.get(f"/rest/api/3/issue/{issue_key}", params={"fields": field_id})
    actual = (check or {}).get("fields", {}).get(field_id)
    if actual != issue_key:
        raise SystemExit(f"Test stamp failed. Field {field_id} not editable on issue type.")
    logging.info("Test stamp verified.")
    return issue_key

def cmd_stamp_keys(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, source_key=cfg.source_key, source_must_exist=True)
    out = cfg.output_dir / "stamp"
    out.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out / "stamped.txt"
    failures_path = out / "stamp_failures.csv"
    logging.info("STAMP-KEYS: %s -> %s", cfg.source_key, cfg.key_field_id)
    precheck_key_field(client, cfg.key_field_id)
    stamp_test_first_issue(client, cfg.source_key, cfg.key_field_id, cfg.dry_run)

    done = set(checkpoint_path.read_text(encoding="utf-8").splitlines()) if checkpoint_path.exists() else set()
    total = client.approximate_count(f'project = "{cfg.source_key}"')
    logging.info("Approximate total issues: %d", total)
    if not cfg.dry_run and not confirm(f"Stamp ~{total} issues? Continue?", cfg.dry_run):
        return

    failures = []
    processed = 0
    with checkpoint_path.open("a", encoding="utf-8") as ckpt:
        for issue in client.search_jql(f'project = "{cfg.source_key}"', fields=["summary"]):
            key = issue["key"]
            if key in done:
                continue
            processed += 1
            try:
                if cfg.dry_run:
                    logging.debug("[DRY-RUN] stamp %s", key)
                else:
                    client.put(f"/rest/api/3/issue/{key}", body={"fields": {cfg.key_field_id: key}}, params={"notifyUsers": "false"})
                    ckpt.write(key + "\n")
                    ckpt.flush()
                    time.sleep(SLEEP_BETWEEN_WRITES)
                if processed % 100 == 0:
                    logging.info("Stamped %d / ~%d ...", processed, total)
            except requests.HTTPError as e:
                failures.append({"key": key, "error": str(e)[:300]})
                logging.warning("Failed to stamp %s: %s", key, e)
    if failures:
        write_csv(failures_path, failures, ["key", "error"])
    logging.info("STAMP-KEYS complete. Processed %d issues.", processed)

# ---------------------------------------------------------------------------
# NEW: COPY-FIELDS command - copy values from source to target using mapping
# ---------------------------------------------------------------------------
def load_field_mapping(mapping_path: Path) -> List[Dict[str, str]]:
    """Load CSV mapping with columns: source_field_id, target_field_id, transform, notes"""
    if not mapping_path.exists():
        raise SystemExit(f"Mapping file not found: {mapping_path}")
    with mapping_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)

def get_source_issue_by_stamped_key(client: JiraClient, source_key: str, stamped_value: str) -> Optional[Dict[str, Any]]:
    """Find source issue where the stamped key field equals given value."""
    jql = f'"{source_key}" = "{stamped_value}"'  # field name is project key? Actually we need field ID.
    # Better: search using the key field ID. We'll pass field_id.
    # We'll restructure: pass key_field_id and search.
    return None  # placeholder, implemented inside cmd_copy_fields

def cmd_copy_fields(cfg: Config) -> None:
    """
    Copy field values from source issues to target issues after bulk move.
    Uses a mapping CSV file to define source->target field relationships.
    Relies on the stamped key field to link source and target issues.
    """
    client_src = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    client_tgt = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client_src, source_key=cfg.source_key, source_must_exist=True)
    preflight(client_tgt, target_key=cfg.target_key, target_must_exist=True)

    mapping_file = cfg.output_dir / "mapping" / "field_mapping.csv"
    if not mapping_file.exists():
        raise SystemExit(
            f"Mapping file not found: {mapping_file}\n"
            f"Create a CSV with columns: source_field_id,target_field_id,transform,notes\n"
            f"Example:\n"
            f"customfield_10010,priority,copy,\n"
            f"customfield_10012,customfield_25100,copy,\n"
        )
    mappings = load_field_mapping(mapping_file)
    logging.info("Loaded %d field mappings", len(mappings))

    out = cfg.output_dir / "copy_fields"
    out.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out / "copied.txt"
    failures_path = out / "copy_failures.csv"

    # Get all target issues (they have the stamped source key in key_field_id)
    target_issues = list(client_tgt.search_jql(f'project = "{cfg.target_key}"', fields=[cfg.key_field_id, "summary"]))
    total = len(target_issues)
    logging.info("Found %d issues in target project", total)

    done = set(checkpoint_path.read_text(encoding="utf-8").splitlines()) if checkpoint_path.exists() else set()
    failures = []
    processed = 0

    for issue in target_issues:
        target_key = issue["key"]
        if target_key in done:
            continue
        stamped_source_key = issue["fields"].get(cfg.key_field_id)
        if not stamped_source_key:
            logging.warning("Target issue %s has no stamped source key, skipping", target_key)
            continue

        # Retrieve source issue using its original key
        try:
            source_issue = client_src.get(f"/rest/api/3/issue/{stamped_source_key}", params={"fields": ",".join([m["source_field_id"] for m in mappings])})
        except requests.HTTPError:
            logging.warning("Source issue %s not found, skipping", stamped_source_key)
            failures.append({"target_key": target_key, "source_key": stamped_source_key, "error": "Source issue not accessible"})
            continue

        # Build update payload
        update_fields = {}
        for m in mappings:
            src_field = m["source_field_id"]
            tgt_field = m["target_field_id"]
            transform = m.get("transform", "copy").strip().lower()
            src_value = source_issue.get("fields", {}).get(src_field)
            if src_value is None:
                continue  # no value to copy
            if transform == "copy":
                update_fields[tgt_field] = src_value
            elif transform == "append_prefix":
                update_fields[tgt_field] = f"Original: {src_value}" if src_value else src_value
            elif transform == "append_suffix":
                update_fields[tgt_field] = f"{src_value} (migrated)"
            else:
                logging.warning("Unknown transform '%s' for mapping %s->%s, using copy", transform, src_field, tgt_field)
                update_fields[tgt_field] = src_value

        if not update_fields:
            continue

        processed += 1
        try:
            if cfg.dry_run:
                logging.info("[DRY-RUN] Would update %s with %s", target_key, update_fields)
            else:
                client_tgt.put(f"/rest/api/3/issue/{target_key}", body={"fields": update_fields}, params={"notifyUsers": "false"})
                checkpoint_path.write_text(target_key + "\n", encoding="utf-8", append=True)
                time.sleep(SLEEP_BETWEEN_WRITES)
            if processed % 50 == 0:
                logging.info("Copied fields for %d / %d issues", processed, total)
        except requests.HTTPError as e:
            failures.append({"target_key": target_key, "source_key": stamped_source_key, "error": str(e)[:300]})
            logging.warning("Failed to copy fields to %s: %s", target_key, e)

    if failures:
        write_csv(failures_path, failures, ["target_key", "source_key", "error"])
    logging.info("COPY-FIELDS complete. Updated %d issues.", processed)

# ---------------------------------------------------------------------------
# BUILD phase (unchanged)
# ---------------------------------------------------------------------------
def get_project_or_none(client: JiraClient, key: str) -> Optional[Dict[str, Any]]:
    try:
        return client.project(key)
    except requests.HTTPError as e:
        if e.response and e.response.status_code == 404:
            return None
        raise

def create_target_project(client: JiraClient, cfg: Config, source_meta: Dict[str, Any]) -> Dict[str, Any]:
    if not cfg.target_lead_account_id:
        raise SystemExit("--target-lead required with --create-target.")
    body = {
        "key": cfg.target_key,
        "name": source_meta["name"] + " (CMP)",
        "projectTypeKey": "software",
        "projectTemplateKey": cfg.target_template,
        "leadAccountId": cfg.target_lead_account_id,
        "assigneeType": source_meta.get("assigneeType") or "PROJECT_LEAD",
        "description": source_meta.get("description") or "",
    }
    logging.info("Creating target CMP %s ...", cfg.target_key)
    if cfg.dry_run:
        return {"id": "0", "key": cfg.target_key}
    return client.post("/rest/api/3/project", body=body)

def build_versions(client: JiraClient, cfg: Config, target_project_id: str) -> None:
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
        body = {"name": name, "projectId": int(target_project_id)}
        for opt in ("description", "startDate", "releaseDate"):
            if v.get(opt): body[opt] = v[opt]
        if cfg.dry_run:
            mapping.append({"source_id": v["id"], "source_name": name, "target_id": "(dry-run)", "action": "would-create"})
            continue
        created = client.post("/rest/api/3/version", body=body)
        target_id = created["id"]
        if v.get("released") or v.get("archived"):
            patch = {}
            if v.get("released"): patch["released"] = True
            if v.get("archived"): patch["archived"] = True
            client.put(f"/rest/api/3/version/{target_id}", body=patch)
        mapping.append({"source_id": v["id"], "source_name": name, "target_id": target_id, "action": "created"})
        time.sleep(SLEEP_BETWEEN_WRITES)
    write_csv(cfg.output_dir / "build" / "version_id_mapping.csv", mapping, ["source_id", "source_name", "target_id", "action"])

def build_components(client: JiraClient, cfg: Config) -> None:
    src = cfg.output_dir / "extract" / "05_components.json"
    if not src.exists():
        return
    components = read_json(src)
    existing = {c["name"]: c for c in (client.get(f"/rest/api/3/project/{cfg.target_key}/components") or [])}
    mapping = []
    for c in components:
        name = c["name"]
        if name in existing:
            mapping.append({"source_id": c["id"], "source_name": name, "target_id": existing[name]["id"], "action": "existing"})
            continue
        body = {"name": name, "project": cfg.target_key}
        if c.get("description"): body["description"] = c["description"]
        if c.get("leadAccountId"): body["leadAccountId"] = c["leadAccountId"]
        if c.get("assigneeType"): body["assigneeType"] = c["assigneeType"]
        if cfg.dry_run:
            mapping.append({"source_id": c["id"], "source_name": name, "target_id": "(dry-run)", "action": "would-create"})
            continue
        created = client.post("/rest/api/3/component", body=body)
        mapping.append({"source_id": c["id"], "source_name": name, "target_id": created["id"], "action": "created"})
        time.sleep(SLEEP_BETWEEN_WRITES)
    write_csv(cfg.output_dir / "build" / "component_id_mapping.csv", mapping, ["source_id", "source_name", "target_id", "action"])

def build_sprints(client: JiraClient, cfg: Config) -> None:
    if not cfg.target_board_id:
        logging.warning("--target-board-id not provided; skipping sprints.")
        return
    src = cfg.output_dir / "extract" / "07_sprints.json"
    if not src.exists():
        return
    sprints = read_json(src)
    try:
        existing_by_name = {s["name"]: s for s in client.paged(f"/rest/agile/1.0/board/{cfg.target_board_id}/sprint", params={"state": "closed,active,future"}, page_size=50)}
    except requests.HTTPError:
        existing_by_name = {}
    mapping = []
    for s in sprints:
        name = s["name"]
        if name in existing_by_name:
            mapping.append({"source_id": s["id"], "source_name": name, "source_state": s["state"],
                            "target_id": existing_by_name[name]["id"], "target_state": existing_by_name[name]["state"],
                            "action": "existing", "note": "completeDate cannot be backdated"})
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
                        "target_id": target_id, "target_state": target_state, "action": "created",
                        "note": "completeDate set to now" if s["state"] == "closed" else ""})
        time.sleep(SLEEP_BETWEEN_WRITES)
    write_csv(cfg.output_dir / "build" / "sprint_id_mapping.csv", mapping,
              ["source_id", "source_name", "source_state", "target_id", "target_state", "action", "note"])

def cmd_build(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key, target_must_exist=not cfg.create_target)
    out = cfg.output_dir / "build"
    out.mkdir(parents=True, exist_ok=True)
    logging.info("BUILD: target=%s", cfg.target_key)
    target = get_project_or_none(client, cfg.target_key)
    meta_path = cfg.output_dir / "extract" / "01_project_meta.json"
    if not meta_path.exists():
        raise SystemExit("Run 'extract' first.")
    source_meta = read_json(meta_path)
    if target is None:
        if not cfg.create_target:
            raise SystemExit(f"Target {cfg.target_key} not found. Use --create-target.")
        target = create_target_project(client, cfg, source_meta)
    target_id = str(target["id"])
    build_versions(client, cfg, target_id)
    build_components(client, cfg)
    build_sprints(client, cfg)
    logging.info("BUILD complete. Mappings in %s", out)

# ---------------------------------------------------------------------------
# VALIDATE phase (unchanged)
# ---------------------------------------------------------------------------
def cmd_validate(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key, target_must_exist=True)
    baseline_path = cfg.output_dir / "extract" / "11_inventory_baseline.json"
    if not baseline_path.exists():
        raise SystemExit("Run 'extract' first.")
    baseline = read_json(baseline_path)
    out = cfg.output_dir / "validate"
    out.mkdir(parents=True, exist_ok=True)
    agg = extract_issues_aggregate(client, cfg.target_key)
    buckets = extract_boolean_buckets(client, cfg.target_key)
    target_inv = {"projectKey": cfg.target_key, "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "total": agg["total"], "byType": agg["byType"], "byStatus": agg["byStatus"],
                  "byResolution": agg["byResolution"], **buckets}
    write_json(out / "target_inventory.json", target_inv)
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
    write_json(out / "validation_delta.json", delta)
    lines = ["category,source,target,diff"]
    for k in ("total", "withAttachments", "withComments", "withFixVersion", "withComponents", "withSprint"):
        d = delta[k]
        lines.append(f"{k},{d['source']},{d['target']},{d['diff']}")
    (out / "validation_summary.csv").write_text("\n".join(lines), encoding="utf-8")
    non_zero = [k for k in ("total", "withAttachments", "withComments", "withFixVersion", "withComponents", "withSprint") if delta[k]["diff"] != 0]
    if non_zero:
        logging.warning("Validation DIFFS found: %s", ", ".join(non_zero))
    else:
        logging.info("Validation: all top-level counts match.")
    logging.info("VALIDATE complete. See %s", out)

def _diff(src: int, tgt: int) -> Dict[str, int]:
    return {"source": src, "target": tgt, "diff": tgt - src}
def _dict_diff(src: Dict[str, int], tgt: Dict[str, int]) -> Dict[str, Dict[str, int]]:
    return {k: _diff(src.get(k, 0), tgt.get(k, 0)) for k in sorted(set(src) | set(tgt))}

# ---------------------------------------------------------------------------
# DOCTOR phase (unchanged)
# ---------------------------------------------------------------------------
def cmd_doctor(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, source_key=cfg.source_key, target_key=cfg.target_key,
              source_must_exist=bool(cfg.source_key), target_must_exist=False)
    for label, key in [("source", cfg.source_key), ("target", cfg.target_key)]:
        if not key: continue
        try:
            boards = list(client.paged("/rest/agile/1.0/board", params={"projectKeyOrId": key}, page_size=50))
        except requests.HTTPError:
            continue
        logging.info("Boards on %s '%s':", label, key)
        if not boards: logging.info("  (none)")
        for b in boards:
            logging.info("  id=%s  type=%-6s  name=%s", b["id"], b.get("type", "?"), b.get("name", "?"))
    if cfg.key_field_id:
        try:
            precheck_key_field(client, cfg.key_field_id)
        except SystemExit as e:
            logging.warning("Key field check: %s", e)
    logging.info("Doctor done.")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Jira TMP -> CMP migration toolkit")
    p.add_argument("--base-url", help="(or JIRA_BASE_URL)")
    p.add_argument("--email", help="(or JIRA_EMAIL)")
    p.add_argument("--output-dir", default="./out", help="(or JIRA_OUTPUT_DIR)")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("doctor", help="Verify auth + project access.")
    pd.add_argument("--source-key", help="(or JIRA_SOURCE_KEY)")
    pd.add_argument("--target-key", help="(or JIRA_TARGET_KEY)")
    pd.add_argument("--key-field", help="(or JIRA_KEY_FIELD)")

    pe = sub.add_parser("extract", help="Extract source TMP data + field catalog.")
    pe.add_argument("--source-key", help="(or JIRA_SOURCE_KEY)", required=False)

    ps = sub.add_parser("stamp-keys", help="Stamp issue keys into a custom field.")
    ps.add_argument("--source-key", help="(or JIRA_SOURCE_KEY)", required=False)
    ps.add_argument("--key-field", help="(or JIRA_KEY_FIELD)")
    ps.add_argument("--dry-run", action="store_true")

    pc = sub.add_parser("copy-fields", help="Copy field values from source to target using mapping CSV.")
    pc.add_argument("--source-key", help="(or JIRA_SOURCE_KEY)", required=False)
    pc.add_argument("--target-key", help="(or JIRA_TARGET_KEY)", required=False)
    pc.add_argument("--key-field", help="(or JIRA_KEY_FIELD)")
    pc.add_argument("--dry-run", action="store_true")

    pb = sub.add_parser("build", help="Recreate versions/components/sprints in target.")
    pb.add_argument("--target-key", help="(or JIRA_TARGET_KEY)", required=False)
    pb.add_argument("--target-board-id", type=int, help="(or JIRA_TARGET_BOARD_ID)")
    pb.add_argument("--create-target", action="store_true")
    pb.add_argument("--target-lead", help="(or JIRA_TARGET_LEAD)")
    pb.add_argument("--target-template", help="(or JIRA_TARGET_TEMPLATE)")
    pb.add_argument("--dry-run", action="store_true")

    pv = sub.add_parser("validate", help="Compare baseline vs target inventory.")
    pv.add_argument("--target-key", help="(or JIRA_TARGET_KEY)", required=False)

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
    except Exception:
        logging.exception("Unhandled error")
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())