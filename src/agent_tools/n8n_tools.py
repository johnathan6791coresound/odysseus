"""n8n workflow automation agent tool (manage_n8n).

Talks to the n8n public REST API (v1) through the same integrations.py
credential store used by api_call — the n8n integration row (preset "n8n")
holds the base_url + encrypted API key. Kept as its own tool rather than
routed through generic api_call because:
  - create/update need a validated workflow shape (name/nodes/connections),
    not a raw passthrough body.
  - trigger is NOT an API-key-authenticated call at all — n8n's public API
    has no "run now" endpoint, so triggering means POSTing to the
    workflow's webhook path on the same host, unauthenticated.
"""
import json
import logging
from typing import Optional, Dict

import httpx

from src.tool_utils import _parse_tool_args
from src.integrations import load_integrations, execute_api_call
from src.url_safety import check_outbound_url

logger = logging.getLogger(__name__)


def _find_n8n_integration() -> Optional[Dict]:
    for item in load_integrations():
        if item.get("preset") == "n8n" and item.get("enabled", True):
            return item
    return None


async def _n8n_list_workflows_raw(integration: Dict, limit: int = 250) -> Dict:
    """Fetch the workflow list directly, bypassing execute_api_call's
    human-readable formatting — its 12KB truncation drops entire workflows
    (each one's full node/code payload is often >12KB by itself), which is
    fine for a chat reply but useless for a picker that needs every id/name/
    webhook-node fact intact."""
    url = f"{integration['base_url'].rstrip('/')}/api/v1/workflows"
    headers = {"X-N8N-API-KEY": integration.get("api_key", "")}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers, params={"limit": limit})
    resp.raise_for_status()
    return resp.json()


async def _n8n_get_workflow_raw(integration: Dict, workflow_id: str) -> Dict:
    """Fetch a workflow's full JSON definition directly (not through
    execute_api_call, whose text formatting/truncation is meant for
    human-readable tool output, not for programmatic node inspection)."""
    url = f"{integration['base_url'].rstrip('/')}/api/v1/workflows/{workflow_id}"
    headers = {"X-N8N-API-KEY": integration.get("api_key", "")}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _extract_webhook_info(workflow: Dict) -> Optional[Dict]:
    """Return {"path", "method"} for the workflow's first Webhook trigger
    node, or None if it has none (e.g. manual-trigger-only workflows can't
    be hit over HTTP at all). n8n's webhook node defaults to GET when
    httpMethod isn't set — that's easy to miss when building/importing a
    workflow, and firing the wrong method 404s ("not registered for POST
    requests") instead of actually running it."""
    for node in workflow.get("nodes", []) or []:
        if node.get("type") == "n8n-nodes-base.webhook":
            params = node.get("parameters") or {}
            path = params.get("path")
            if path:
                return {"path": path, "method": (params.get("httpMethod") or "GET").upper()}
    return None


def _extract_webhook_path(workflow: Dict) -> Optional[str]:
    """Back-compat helper for callers that only need the path (e.g. the
    has_webhook flag in the task-dialog workflow picker)."""
    info = _extract_webhook_info(workflow)
    return info["path"] if info else None


async def _resolve_trigger_path(integration: Dict, args: Dict) -> Dict:
    """Resolve the webhook path + HTTP method to call for action='trigger'.
    Accepts either an explicit webhook_path (method defaults to POST, since
    a caller giving a raw path is choosing it deliberately and POST is the
    common case for a manually-specified path) or a workflow_id to look up —
    the latter is what scheduled n8n_trigger tasks use, since a task should
    reference a workflow by its stable ID, not a path the workflow's owner
    might rename. Looking up by workflow_id also resolves the REAL configured
    method (GET/POST/etc.) from the node, instead of guessing."""
    path = args.get("webhook_path", "")
    if path:
        return {"path": path, "method": (args.get("method") or "POST").upper()}

    wid = args.get("workflow_id", "")
    if not wid:
        return {"error": "webhook_path or workflow_id is required", "exit_code": 1}

    try:
        workflow = await _n8n_get_workflow_raw(integration, wid)
    except httpx.HTTPStatusError as exc:
        return {"error": f"Could not fetch workflow {wid}: HTTP {exc.response.status_code}", "exit_code": 1}
    except httpx.RequestError as exc:
        return {"error": f"Could not fetch workflow {wid}: {exc}", "exit_code": 1}

    info = _extract_webhook_info(workflow)
    if not info:
        name = workflow.get("name", wid)
        return {
            "error": f"Workflow '{name}' ({wid}) has no Webhook trigger node — it can't be triggered "
                     "over HTTP. Trigger it manually in the n8n UI, or add a Webhook node.",
            "exit_code": 1,
        }
    return info


async def _n8n_webhook_trigger(base_url: str, path: str, method: str, test: bool, body: Optional[dict]) -> Dict:
    """Call an n8n webhook trigger — the only way to run a workflow on
    demand via HTTP. `test` hits /webhook-test/ (the workflow's manual test
    listener) instead of the always-on /webhook/ production path. `method`
    must match the webhook node's actual configured HTTP method — n8n
    rejects a mismatched verb with a 404, not a 405."""
    segment = "webhook-test" if test else "webhook"
    url = f"{base_url.rstrip('/')}/{segment}/{path.lstrip('/')}"

    ok, reason = check_outbound_url(url, block_private=False)
    if not ok:
        return {"error": f"URL rejected: {reason}", "exit_code": 1}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            if method == "GET":
                resp = await client.get(url, params=body or None)
            else:
                resp = await client.request(method, url, json=body or {})
        try:
            data = resp.json()
            formatted = json.dumps(data, indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            formatted = resp.text
        if len(formatted) > 8000:
            formatted = formatted[:8000] + f"\n... (truncated, {len(formatted)} chars total)"
        output = f"HTTP {resp.status_code}\n{formatted}"
        if resp.status_code >= 400:
            return {"error": output, "exit_code": 1}
        return {"output": output, "exit_code": 0}
    except httpx.TimeoutException:
        return {"error": "Webhook call timed out", "exit_code": 1}
    except httpx.RequestError as exc:
        return {"error": f"Webhook call failed: {exc}", "exit_code": 1}


async def do_manage_n8n(content: str, owner: Optional[str] = None) -> Dict:
    """Manage n8n workflows: list, get, create, update, delete, activate,
    deactivate, trigger (via webhook), list_executions, get_execution."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    integration = _find_n8n_integration()
    if not integration:
        return {
            "error": "No n8n integration is registered. Add one via manage_settings/integrations "
                     "(preset 'n8n') with the n8n instance's base URL and API key.",
            "exit_code": 1,
        }

    action = args.get("action", "list")

    if action == "list":
        params = {"limit": args.get("limit", 50)}
        if "active" in args:
            params["active"] = bool(args["active"])
        return await execute_api_call(integration["id"], "GET", "/api/v1/workflows", params=params)

    elif action == "get":
        wid = args.get("workflow_id", "")
        if not wid:
            return {"error": "workflow_id is required", "exit_code": 1}
        return await execute_api_call(integration["id"], "GET", f"/api/v1/workflows/{wid}")

    elif action == "create":
        name = args.get("name", "")
        nodes = args.get("nodes")
        connections = args.get("connections")
        if not name or nodes is None or connections is None:
            return {"error": "name, nodes, and connections are required to create a workflow", "exit_code": 1}
        body = {
            "name": name,
            "nodes": nodes,
            "connections": connections,
            "settings": args.get("settings", {}),
        }
        return await execute_api_call(integration["id"], "POST", "/api/v1/workflows", body=body)

    elif action == "update":
        wid = args.get("workflow_id", "")
        if not wid:
            return {"error": "workflow_id is required", "exit_code": 1}
        body = {}
        for key in ("name", "nodes", "connections", "settings"):
            if key in args:
                body[key] = args[key]
        if not body:
            return {"error": "at least one of name/nodes/connections/settings is required", "exit_code": 1}
        # n8n's PUT requires `name` in the body even when only nodes/connections
        # are actually changing — without it: 400 "request/body must have
        # required property 'name'". Backfill from the current workflow rather
        # than making every caller re-supply a name it isn't changing.
        if "name" not in body:
            try:
                current = await _n8n_get_workflow_raw(integration, wid)
                body["name"] = current.get("name")
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                return {"error": f"Could not fetch current name for {wid}: {exc}", "exit_code": 1}
        return await execute_api_call(integration["id"], "PUT", f"/api/v1/workflows/{wid}", body=body)

    elif action == "delete":
        wid = args.get("workflow_id", "")
        if not wid:
            return {"error": "workflow_id is required", "exit_code": 1}
        return await execute_api_call(integration["id"], "DELETE", f"/api/v1/workflows/{wid}")

    elif action in ("activate", "deactivate"):
        wid = args.get("workflow_id", "")
        if not wid:
            return {"error": "workflow_id is required", "exit_code": 1}
        return await execute_api_call(integration["id"], "POST", f"/api/v1/workflows/{wid}/{action}")

    elif action == "trigger":
        resolved = await _resolve_trigger_path(integration, args)
        if "error" in resolved:
            return resolved
        return await _n8n_webhook_trigger(
            integration["base_url"], resolved["path"], resolved["method"],
            bool(args.get("test", False)), args.get("body"),
        )

    elif action == "list_executions":
        params = {"limit": args.get("limit", 20)}
        if args.get("workflow_id"):
            params["workflowId"] = args["workflow_id"]
        if args.get("status"):
            params["status"] = args["status"]
        return await execute_api_call(integration["id"], "GET", "/api/v1/executions", params=params)

    elif action == "get_execution":
        eid = args.get("execution_id", "")
        if not eid:
            return {"error": "execution_id is required", "exit_code": 1}
        return await execute_api_call(integration["id"], "GET", f"/api/v1/executions/{eid}")

    else:
        return {"error": f"Unknown action: {action}", "exit_code": 1}
