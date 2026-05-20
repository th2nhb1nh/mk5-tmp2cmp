#!/usr/bin/env python3
"""
Jira TMP -> CMP Migration Toolkit (Phase 3: extract & rebuild)

Subcommands:
  doctor      Verify auth + project access. Run this first.
  extract     Pull source TMP project config + inventory to local artifacts.
  stamp-keys  Write each source issue's current key into a designated text
              custom field (e.g. customfield_25199). Survives bulk move.
  build       Recreate versions, components, and sprints in the target CMP
              from extracted artifacts. Outputs ID mapping CSVs.
  validate    Compare source baseline counts against target post-move counts.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Try to load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()  # This loads .env file from current directory
except ImportError:
    # python-dotenv not installed, continue without it
    pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 30
PAGE_SIZE_SEARCH = 100
PAGE_SIZE_GENERIC = 50
SLEEP_BETWEEN_WRITES = 0.10  # gentle rate-limit cushion
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
        # Priority: CLI args > environment variables
        base = (getattr(args, "base_url", "") or 
                os.environ.get("JIRA_BASE_URL", "")).rstrip("/")
        email = (getattr(args, "email", "") or 
                 os.environ.get("JIRA_EMAIL", ""))
        token = os.environ.get("JIRA_API_TOKEN", "")
        
        missing = []
        if not base:
            missing.append("JIRA_BASE_URL")
        if not email:
            missing.append("JIRA_EMAIL")
        if not token:
            missing.append("JIRA_API_TOKEN")
            
        if missing:
            raise SystemExit(
                f"\nMissing credentials: {', '.join(missing)}\n"
                f"Set them in .env file or pass via CLI arguments.\n"
                f"Example .env:\n"
                f'JIRA_BASE_URL="https://mysite.atlassian.net"\n'
                f'JIRA_EMAIL="your-email@example.com"\n'
                f'JIRA_API_TOKEN="your-api-token"\n'
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
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Jira REST client
# ---------------------------------------------------------------------------

class JiraClient:
    """Thin requests wrapper: basic auth, retry, pagination, original-URL logging."""

    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url
        creds = f"{email}:{api_token}".encode("utf-8")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": "Basic " + base64.b64encode(creds).decode("ascii"),
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
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # --- core request ----------------------------------------------------

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

    # --- convenience -----------------------------------------------------

    def myself(self) -> Dict[str, Any]:
        return self.get("/rest/api/3/myself")

    def project(self, key: str) -> Dict[str, Any]:
        return self.get(f"/rest/api/3/project/{key}",
                        params={"expand": "description,lead"})

    def project_search(self, query: str) -> Dict[str, Any]:
        return self.get("/rest/api/3/project/search", params={"query": query}) or {}

    def fields(self) -> List[Dict[str, Any]]:
        return self.get("/rest/api/3/field") or []

    # --- pagination ------------------------------------------------------

    def paged(self, path: str, params: Optional[Dict[str, Any]] = None,
              values_key: str = "values",
              page_size: int = PAGE_SIZE_GENERIC) -> Iterable[Dict[str, Any]]:
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
        """Iterate issues via POST /rest/api/3/search/jql."""
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


# ---------------------------------------------------------------------------
# Preflight (auth + project access)
# ---------------------------------------------------------------------------

def preflight(client: JiraClient,
              source_key: str = "",
              target_key: str = "",
              source_must_exist: bool = True,
              target_must_exist: bool = False) -> Dict[str, Any]:
    """Verify auth and project visibility. Raises SystemExit with actionable
    messages on failure. Returns the authenticated user dict."""
    logging.info(SEP)
    logging.info("Preflight")
    logging.info(SEP)

    # 1. Authentication
    try:
        me = client.myself()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        raise SystemExit(_auth_error_message(code, client.base_url))
    except requests.RequestException as e:
        raise SystemExit(
            f"\nCould not reach {client.base_url}: {e}\n"
            f"Check the URL and your network.\n"
        )

    logging.info("Authentication successful")
    logging.info("  User:      %s", me.get("displayName"))
    logging.info("  Email:     %s", me.get("emailAddress"))
    logging.info("  AccountId: %s", me.get("accountId"))
    logging.info("  Site:      %s", client.base_url)

    # 2. Project access
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
        code = e.response.status_code if e.response is not None else "?"
        if code == 404:
            if not must_exist:
                logging.info("%s project '%s' does not exist yet (OK for this phase).",
                             label.capitalize(), key)
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
    logging.info("  Type:  %s", proj.get("projectTypeKey"))
    if warn:
        logging.warning(warn)
    return proj


def _disambiguate_404(client: JiraClient, key: str) -> str:
    """Distinguish 'project does not exist' from 'no permission to see it'."""
    try:
        results = client.project_search(key)
        values = results.get("values", [])
        case_matches = [p for p in values if p["key"].upper() == key.upper()]
        if case_matches:
            actual = case_matches[0]["key"]
            if actual != key:
                return (f"Project key case mismatch. The API is case-sensitive. "
                        f"Use '{actual}', not '{key}'.")
            return ("Project EXISTS but the authenticated user lacks BROWSE "
                    "permission. Add this user to a project role with browse "
                    "rights, or use a token from a user who has access.")
        sample = ", ".join(p["key"] for p in values[:5]) or "(none)"
        return (f"No project with key '{key}' is visible. "
                f"Verify the key (case-sensitive). "
                f"Sample of accessible keys: {sample}")
    except Exception as ex:
        return f"Could not disambiguate (project search failed: {ex})."


def _auth_error_message(code: Any, base_url: str) -> str:
    return (
        f"\n{SEP}\n"
        f"AUTHENTICATION FAILED ({code})\n"
        f"{SEP}\n"
        f"  Site: {base_url}\n"
        f"\n"
        f"Likely causes:\n"
        f"  1. JIRA_API_TOKEN expired/revoked.\n"
        f"     Regenerate: https://id.atlassian.com/manage-profile/security/api-tokens\n"
        f"  2. JIRA_EMAIL doesn't match the token owner.\n"
        f"  3. JIRA_BASE_URL is wrong (e.g. typo, or token issued for a different site).\n"
        f"  4. The account is deactivated.\n"
        f"\n"
        f"Manual verification:\n"
        f"  curl -u \"$JIRA_EMAIL:$JIRA_API_TOKEN\" \"$JIRA_BASE_URL/rest/api/3/myself\"\n"
        f"{SEP}\n"
    )


def _project_error_message(label: str, key: str, hint: str,
                           me: Dict[str, Any], base_url: str) -> str:
    return (
        f"\n{SEP}\n"
        f"{label.upper()} PROJECT '{key}' NOT ACCESSIBLE\n"
        f"{SEP}\n"
        f"  {hint}\n"
        f"\n"
        f"  Authenticated as: {me.get('displayName')} <{me.get('emailAddress')}>\n"
        f"  AccountId:        {me.get('accountId')}\n"
        f"  Verify in UI:     {base_url}/browse/{key}\n"
        f"  List projects:    {base_url}/jira/projects\n"
        f"{SEP}\n"
    )


# ---------------------------------------------------------------------------
# IO helpers
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
# EXTRACT phase
# ---------------------------------------------------------------------------

def extract_project_meta(client: JiraClient, key: str) -> Dict[str, Any]:
    proj = client.project(key)
    return {
        "id": proj["id"],
        "key": proj["key"],
        "name": proj["name"],
        "projectTypeKey": proj.get("projectTypeKey"),
        "style": proj.get("style"),
        "description": proj.get("description"),
        "leadAccountId": (proj.get("lead") or {}).get("accountId"),
        "leadDisplayName": (proj.get("lead") or {}).get("displayName"),
        "assigneeType": proj.get("assigneeType"),
    }


def extract_issues_aggregate(client: JiraClient, key: str) -> Dict[str, Any]:
    """Single pass over all issues: distinct types, distinct (type,status)
    pairs, and counts grouped by type / status / resolution."""
    types: Dict[str, Dict[str, Any]] = {}
    type_status_pairs: Dict[str, Dict[str, Any]] = {}
    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    by_resolution: Dict[str, int] = {}
    total = 0

    fields = ["issuetype", "status", "resolution"]
    for issue in client.search_jql(f'project = "{key}"', fields=fields):
        total += 1
        f = issue["fields"]
        it = f["issuetype"]
        st = f["status"]

        if it["id"] not in types:
            types[it["id"]] = {
                "id": it["id"],
                "name": it["name"],
                "subtask": it.get("subtask", False),
                "hierarchyLevel": it.get("hierarchyLevel"),
            }
        pair_key = f"{it['name']}::{st['name']}"
        if pair_key not in type_status_pairs:
            type_status_pairs[pair_key] = {
                "issueType": it["name"],
                "statusName": st["name"],
                "statusId": st["id"],
                "statusCategory": (st.get("statusCategory") or {}).get("key"),
            }

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
        "byType": by_type,
        "byStatus": by_status,
        "byResolution": by_resolution,
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
    return [{
        "sequence": i,
        "id": v["id"],
        "name": v["name"],
        "description": v.get("description", ""),
        "startDate": v.get("startDate", ""),
        "releaseDate": v.get("releaseDate", ""),
        "released": v.get("released", False),
        "archived": v.get("archived", False),
        "overdue": v.get("overdue", False),
    } for i, v in enumerate(versions)]


def extract_components(client: JiraClient, key: str) -> List[Dict[str, Any]]:
    comps = client.get(f"/rest/api/3/project/{key}/components") or []
    return [{
        "id": c["id"],
        "name": c["name"],
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
    raw = list(client.paged(f"/rest/agile/1.0/board/{board_id}/sprint",
                            params={"state": "closed,active,future"}, page_size=50))
    order = {"closed": 0, "active": 1, "future": 2}
    raw.sort(key=lambda s: (order.get(s.get("state", "future"), 3),
                            s.get("completeDate") or s.get("startDate") or ""))
    return [{
        "id": s["id"],
        "name": s["name"],
        "state": s["state"],
        "startDate": s.get("startDate", ""),
        "endDate": s.get("endDate", ""),
        "completeDate": s.get("completeDate", ""),
        "goal": s.get("goal", ""),
        "originBoardId": s.get("originBoardId"),
    } for s in raw]


def extract_roles(client: JiraClient, key: str) -> Dict[str, Any]:
    role_map = client.get(f"/rest/api/3/project/{key}/role") or {}
    out: Dict[str, Any] = {}
    for role_name, role_url in role_map.items():
        detail = client.get(role_url) or {}
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


def extract_custom_fields_in_use(client: JiraClient, key: str,
                                 sample_size: int = 500) -> List[Dict[str, Any]]:
    catalog = {f["id"]: f for f in client.fields()}
    seen: Dict[str, Dict[str, Any]] = {}
    scanned = 0
    for issue in client.search_jql(f'project = "{key}"', fields=["*all"],
                                    expand="names"):
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


def cmd_extract(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, source_key=cfg.source_key, source_must_exist=True)

    out = cfg.output_dir / "extract"
    out.mkdir(parents=True, exist_ok=True)
    logging.info("EXTRACT: source=%s output=%s", cfg.source_key, out)

    # 1. Project metadata
    meta = extract_project_meta(client, cfg.source_key)
    write_json(out / "01_project_meta.json", meta)

    # 2 + 3 + 11 inventory in a single pass
    logging.info("Scanning all issues (single pass)...")
    agg = extract_issues_aggregate(client, cfg.source_key)
    write_json(out / "02_issue_types.json", agg["types"])
    write_csv(out / "02_issue_types.csv", agg["types"],
              ["id", "name", "subtask", "hierarchyLevel"])
    write_json(out / "03_statuses.json", agg["statuses"])
    write_csv(out / "03_statuses.csv", agg["statuses"],
              ["issueType", "statusName", "statusId", "statusCategory"])

    # 4. Versions
    versions = extract_versions(client, cfg.source_key)
    write_json(out / "04_versions.json", versions)
    write_csv(out / "04_versions.csv", versions,
              ["sequence", "id", "name", "description", "startDate",
               "releaseDate", "released", "archived"])

    # 5. Components
    components = extract_components(client, cfg.source_key)
    write_json(out / "05_components.json", components)
    write_csv(out / "05_components.csv", components,
              ["id", "name", "description", "leadAccountId",
               "assigneeType", "realAssigneeType"])

    # 6-8. Board + sprints + board config
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

    # 9. Roles
    write_json(out / "09_roles.json", extract_roles(client, cfg.source_key))

    # 10. Custom fields in use (separate pass with *all)
    logging.info("Sampling custom fields in use...")
    fields_in_use = extract_custom_fields_in_use(client, cfg.source_key)
    write_json(out / "10_custom_fields_in_use.json", fields_in_use)
    write_csv(out / "10_custom_fields_in_use.csv", fields_in_use,
              ["fieldId", "displayName", "schemaType", "schemaCustom", "exampleValue"])

    # 11. Inventory baseline (combine aggregate counts + boolean buckets)
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

    logging.info("EXTRACT complete. %d issues scanned. Artifacts in %s",
                 agg["total"], out)


# ---------------------------------------------------------------------------
# STAMP-KEYS phase
# ---------------------------------------------------------------------------

def precheck_key_field(client: JiraClient, field_id: str) -> Dict[str, Any]:
    found = next((f for f in client.fields() if f.get("id") == field_id), None)
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


def stamp_test_first_issue(client: JiraClient, source_key: str,
                           field_id: str, dry_run: bool) -> str:
    """Test on the oldest issue, then read back to confirm value persisted."""
    first = next(client.search_jql(f'project = "{source_key}" ORDER BY created ASC',
                                    fields=["summary"]), None)
    if not first:
        raise SystemExit(f"No issues found in source project {source_key}.")
    issue_key = first["key"]
    logging.info("Test stamp on %s", issue_key)
    if dry_run:
        logging.info("[DRY-RUN] Would stamp %s into %s", issue_key, field_id)
        return issue_key
    client.put(f"/rest/api/3/issue/{issue_key}",
               body={"fields": {field_id: issue_key}},
               params={"notifyUsers": "false"})
    check = client.get(f"/rest/api/3/issue/{issue_key}",
                       params={"fields": field_id})
    actual = (check or {}).get("fields", {}).get(field_id)
    if actual != issue_key:
        raise SystemExit(
            f"Test stamp did not persist on {issue_key}.\n"
            f"  Expected: '{issue_key}'\n"
            f"  Got:      '{actual}'\n"
            f"  Cause:    {field_id} is not on the EDIT screen for issue type "
            f"'{first['fields'].get('issuetype', {}).get('name', '?')}'.\n"
            f"  Fix:      Source project settings -> Issue types -> add the field, save.\n"
        )
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

    done = (set(checkpoint_path.read_text(encoding="utf-8").splitlines())
            if checkpoint_path.exists() else set())
    if done:
        logging.info("Resuming: %d issues already stamped (checkpoint).", len(done))

    total = client.approximate_count(f'project = "{cfg.source_key}"')
    logging.info("Approximate total issues: %d", total)

    if not cfg.dry_run and not confirm(
        f"About to stamp {cfg.key_field_id} on ~{total} issues in {cfg.source_key}. Continue?",
        cfg.dry_run,
    ):
        logging.info("Aborted by user.")
        return

    failures: List[Dict[str, str]] = []
    processed = 0
    with checkpoint_path.open("a", encoding="utf-8") as ckpt:
        for issue in client.search_jql(f'project = "{cfg.source_key}"',
                                        fields=["summary"]):
            key = issue["key"]
            if key in done:
                continue
            processed += 1
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
                if processed % 100 == 0:
                    logging.info("Stamped %d / ~%d ...", processed, total)
            except requests.HTTPError as e:
                failures.append({"key": key, "error": str(e)[:300]})
                logging.warning("Failed to stamp %s: %s", key, e)

    if failures:
        write_csv(failures_path, failures, ["key", "error"])
        logging.warning("%d failures recorded in %s", len(failures), failures_path)
    logging.info("STAMP-KEYS complete. Processed %d issues this run.", processed)


# ---------------------------------------------------------------------------
# BUILD phase
# ---------------------------------------------------------------------------

def get_project_or_none(client: JiraClient, key: str) -> Optional[Dict[str, Any]]:
    try:
        return client.project(key)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def create_target_project(client: JiraClient, cfg: Config,
                          source_meta: Dict[str, Any]) -> Dict[str, Any]:
    if not cfg.target_lead_account_id:
        raise SystemExit("--target-lead <accountId> is required with --create-target.")
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
        logging.info("[DRY-RUN] POST /rest/api/3/project body=%s", body)
        return {"id": "0", "key": cfg.target_key}
    return client.post("/rest/api/3/project", body=body)


def build_versions(client: JiraClient, cfg: Config,
                   target_project_id: str) -> None:
    src = cfg.output_dir / "extract" / "04_versions.json"
    if not src.exists():
        logging.warning("No versions extract; skipping.")
        return
    versions = read_json(src)
    existing = {v["name"]: v for v in
                (client.get(f"/rest/api/3/project/{cfg.target_key}/versions") or [])}

    mapping: List[Dict[str, Any]] = []
    for v in versions:
        name = v["name"]
        if name in existing:
            mapping.append({"source_id": v["id"], "source_name": name,
                            "target_id": existing[name]["id"], "action": "existing"})
            logging.info("Version exists: %s", name)
            continue

        body: Dict[str, Any] = {"name": name, "projectId": int(target_project_id)}
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


def build_components(client: JiraClient, cfg: Config) -> None:
    src = cfg.output_dir / "extract" / "05_components.json"
    if not src.exists():
        logging.warning("No components extract; skipping.")
        return
    components = read_json(src)
    existing = {c["name"]: c for c in
                (client.get(f"/rest/api/3/project/{cfg.target_key}/components") or [])}

    mapping: List[Dict[str, Any]] = []
    for c in components:
        name = c["name"]
        if name in existing:
            mapping.append({"source_id": c["id"], "source_name": name,
                            "target_id": existing[name]["id"], "action": "existing"})
            logging.info("Component exists: %s", name)
            continue
        body: Dict[str, Any] = {"name": name, "project": cfg.target_key}
        if c.get("description"):
            body["description"] = c["description"]
        if c.get("leadAccountId"):
            body["leadAccountId"] = c["leadAccountId"]
        if c.get("assigneeType"):
            body["assigneeType"] = c["assigneeType"]

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


def build_sprints(client: JiraClient, cfg: Config) -> None:
    if not cfg.target_board_id:
        logging.warning(
            "--target-board-id not provided; skipping sprints. "
            "Create a Scrum board on the target CMP, then re-run build with "
            "--target-board-id <id>."
        )
        return
    src = cfg.output_dir / "extract" / "07_sprints.json"
    if not src.exists():
        logging.warning("No sprints extract; skipping.")
        return
    sprints = read_json(src)

    try:
        existing_by_name = {s["name"]: s for s in client.paged(
            f"/rest/agile/1.0/board/{cfg.target_board_id}/sprint",
            params={"state": "closed,active,future"}, page_size=50)}
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
                "action": "existing",
                "note": "completeDate cannot be backdated via API",
            })
            continue

        body: Dict[str, Any] = {"name": name, "originBoardId": cfg.target_board_id}
        for opt in ("startDate", "endDate", "goal"):
            if s.get(opt):
                body[opt] = s[opt]

        if cfg.dry_run:
            logging.info("[DRY-RUN] Create sprint %s (target state %s)", name, s["state"])
            mapping.append({"source_id": s["id"], "source_name": name,
                            "source_state": s["state"],
                            "target_id": "(dry-run)", "target_state": "future",
                            "action": "would-create", "note": ""})
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


def cmd_build(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key,
              target_must_exist=not cfg.create_target)

    out = cfg.output_dir / "build"
    out.mkdir(parents=True, exist_ok=True)
    logging.info("BUILD: target=%s", cfg.target_key)

    target = get_project_or_none(client, cfg.target_key)
    meta_path = cfg.output_dir / "extract" / "01_project_meta.json"
    if not meta_path.exists():
        raise SystemExit("Source extract artifacts missing. Run 'extract' first.")
    source_meta = read_json(meta_path)

    if target is None:
        if not cfg.create_target:
            raise SystemExit(
                f"Target project {cfg.target_key} not found. "
                f"Create it in the UI (recommended), or re-run with "
                f"--create-target --target-lead <accountId>."
            )
        target = create_target_project(client, cfg, source_meta)

    target_id = str(target["id"])
    build_versions(client, cfg, target_id)
    build_components(client, cfg)
    build_sprints(client, cfg)
    logging.info("BUILD complete. Mappings in %s", out)


# ---------------------------------------------------------------------------
# VALIDATE phase
# ---------------------------------------------------------------------------

def cmd_validate(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client, target_key=cfg.target_key, target_must_exist=True)

    baseline_path = cfg.output_dir / "extract" / "11_inventory_baseline.json"
    if not baseline_path.exists():
        raise SystemExit("Baseline inventory not found. Run 'extract' first.")
    baseline = read_json(baseline_path)

    out = cfg.output_dir / "validate"
    out.mkdir(parents=True, exist_ok=True)

    logging.info("Scanning target project for current counts...")
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
    for k in ("total", "withAttachments", "withComments",
              "withFixVersion", "withComponents", "withSprint"):
        d = delta[k]
        lines.append(f"{k},{d['source']},{d['target']},{d['diff']}")
    (out / "validation_summary.csv").write_text("\n".join(lines), encoding="utf-8")

    non_zero = [k for k in ("total", "withAttachments", "withComments",
                            "withFixVersion", "withComponents", "withSprint")
                if delta[k]["diff"] != 0]
    if non_zero:
        logging.warning("Validation DIFFS found: %s", ", ".join(non_zero))
    else:
        logging.info("Validation: all top-level counts match.")
    logging.info("VALIDATE complete. See %s", out)


def _diff(src: int, tgt: int) -> Dict[str, int]:
    return {"source": src, "target": tgt, "diff": tgt - src}


def _dict_diff(src: Dict[str, int], tgt: Dict[str, int]) -> Dict[str, Dict[str, int]]:
    return {k: _diff(src.get(k, 0), tgt.get(k, 0))
            for k in sorted(set(src) | set(tgt))}


# ---------------------------------------------------------------------------
# DOCTOR phase
# ---------------------------------------------------------------------------

def cmd_doctor(cfg: Config) -> None:
    client = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    preflight(client,
              source_key=cfg.source_key,
              target_key=cfg.target_key,
              source_must_exist=bool(cfg.source_key),
              target_must_exist=False)

    # Bonus: list boards on each project, and check key field if specified
    for label, key in [("source", cfg.source_key), ("target", cfg.target_key)]:
        if not key:
            continue
        try:
            boards = list(client.paged("/rest/agile/1.0/board",
                                       params={"projectKeyOrId": key},
                                       page_size=50))
        except requests.HTTPError:
            continue
        logging.info("Boards on %s '%s':", label, key)
        if not boards:
            logging.info("  (none)")
        for b in boards:
            logging.info("  id=%s  type=%-6s  name=%s",
                         b["id"], b.get("type", "?"), b.get("name", "?"))

    if cfg.key_field_id:
        try:
            precheck_key_field(client, cfg.key_field_id)
        except SystemExit as e:
            logging.warning("Key field check: %s", e)

    logging.info("\nDoctor done. All checks above are green => safe to run extract.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Jira TMP -> CMP migration toolkit",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url", help="(or set JIRA_BASE_URL)")
    p.add_argument("--email", help="(or set JIRA_EMAIL)")
    p.add_argument("--output-dir", default="./out", help="(or set JIRA_OUTPUT_DIR)")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("doctor", help="Verify auth + project access.")
    pd.add_argument("--source-key", help="(or set JIRA_SOURCE_KEY)")
    pd.add_argument("--target-key", help="(or set JIRA_TARGET_KEY)")
    pd.add_argument("--key-field", help="(or set JIRA_KEY_FIELD, default customfield_25199)")

    pe = sub.add_parser("extract", help="Extract source TMP data.")
    pe.add_argument("--source-key", help="(or set JIRA_SOURCE_KEY)", required=False)

    ps = sub.add_parser("stamp-keys", help="Stamp issue keys into a text custom field.")
    ps.add_argument("--source-key", help="(or set JIRA_SOURCE_KEY)", required=False)
    ps.add_argument("--key-field", help="(or set JIRA_KEY_FIELD, default customfield_25199)")
    ps.add_argument("--dry-run", action="store_true")

    pb = sub.add_parser("build", help="Recreate versions/components/sprints in target.")
    pb.add_argument("--target-key", help="(or set JIRA_TARGET_KEY)", required=False)
    pb.add_argument("--target-board-id", type=int, help="(or set JIRA_TARGET_BOARD_ID)")
    pb.add_argument("--create-target", action="store_true")
    pb.add_argument("--target-lead", help="(or set JIRA_TARGET_LEAD)")
    pb.add_argument("--target-template", help="(or set JIRA_TARGET_TEMPLATE)")
    pb.add_argument("--dry-run", action="store_true")

    pv = sub.add_parser("validate", help="Compare baseline vs target inventory.")
    pv.add_argument("--target-key", help="(or set JIRA_TARGET_KEY)", required=False)

    return p


COMMANDS = {
    "doctor": cmd_doctor,
    "extract": cmd_extract,
    "stamp-keys": cmd_stamp_keys,
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