import json
import logging

logger = logging.getLogger(__name__)

class AskUserTool:
    async def execute(self, content, ctx):
        """
        ask_user: the agent poses a multiple-choice question to the user to get a
        decision/clarification. This is a pure UI-control marker — no subprocess,
        no filesystem. It returns an `ask_user` payload that the agent loop turns
        into an `ask_user` SSE event and then ENDS the turn, so the chat waits for
        the user's selection (their choice arrives as the next message).
        """
        question, options, multi = "", [], False
        raw = (content or "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict):
            question = str(parsed.get("question", "")).strip()
            multi = bool(parsed.get("multi") or parsed.get("multiSelect"))
            for opt in (parsed.get("options") or []):
                if isinstance(opt, dict):
                    label = str(opt.get("label", "")).strip()
                    descr = str(opt.get("description", "")).strip()
                elif isinstance(opt, str):
                    label, descr = opt.strip(), ""
                else:
                    continue
                if label:
                    options.append({"label": label, "description": descr})
        else:
            question = raw

        if not question or len(options) < 2:
            return "ask_user: invalid", {
                "error": (
                    "ask_user needs a non-empty `question` and at least 2 `options` "
                    "(each an object with a `label`, optional `description`)."
                ),
                "exit_code": 1,
            }

        options = options[:6]  # keep the choice list sane
        desc = f"ask_user: {question[:80]}"
        labels = ", ".join(o["label"] for o in options)
        result = {
            "ask_user": {"question": question, "options": options, "multi": multi},
            "output": f"Asked the user: {question}\nOptions: {labels}\nAwaiting their selection.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s (%d options, multi=%s)", desc, len(options), multi)
        return desc, result

class LoadToolsTool:
    async def execute(self, content, ctx):
        """
        load_tools: pull one or more real tool schemas into scope for the rest
        of this turn (and persist them on the session for future turns) after
        the model picks a name out of the always-injected tool catalog (see
        src/tool_index.py:build_tool_catalog). Pure local marker — the actual
        `_relevant_tools` union + session persistence happens in agent_loop.py
        right after this returns; this handler only validates the requested
        names against everything that's known to exist.
        """
        raw = (content or "").strip()
        names = []
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict):
            names = parsed.get("names") or ([parsed["name"]] if parsed.get("name") else [])
        elif isinstance(parsed, list):
            names = parsed
        if not names and raw and not raw.startswith("{") and not raw.startswith("["):
            names = [n.strip() for n in raw.split(",")]

        names = [str(n).strip() for n in names if str(n).strip()]
        if not names:
            return "load_tools: invalid", {
                "error": "load_tools needs `names` (a list of exact tool names from the catalog).",
                "exit_code": 1,
            }

        from src.tool_policy import known_tool_names
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        known = known_tool_names() | set(BUILTIN_TOOL_DESCRIPTIONS.keys())
        try:
            from src.tool_utils import get_mcp_manager
            mcp_mgr = get_mcp_manager()
            if mcp_mgr is not None:
                mcp_text = mcp_mgr.get_tool_descriptions_for_prompt({})
                for line in (mcp_text or "").strip().split("\n"):
                    line = line.strip()
                    if line.startswith("- ") and ":" in line:
                        known.add(line[2:].split(":", 1)[0].strip())
        except Exception:
            pass

        valid = [n for n in names if n in known]
        invalid = [n for n in names if n not in known]

        desc = f"load_tools: {', '.join(valid) or 'none'}"
        if invalid:
            desc += f" (unknown: {', '.join(invalid)})"
        result = {
            "loaded": valid,
            "output": (
                (f"Loaded: {', '.join(valid)}. " if valid else "")
                + (f"Not found in catalog (check exact spelling): {', '.join(invalid)}." if invalid else "")
            ).strip(),
            "exit_code": 0 if valid else 1,
        }
        if not valid:
            result["error"] = result["output"]
        logger.info("Tool executed: %s", desc)
        return desc, result


class UpdatePlanTool:
    async def execute(self, content, ctx):
        """
        update_plan: the agent writes back to the active plan — tick an item done
        or revise steps (e.g. when the user asks to change something). Pure UI
        marker: returns a `plan_update` payload the agent loop turns into a
        `plan_update` SSE event; the frontend replaces the stored plan and refreshes
        the docked plan window. Does NOT end the turn.
        """
        raw = (content or "").strip()
        plan = ""
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict) and parsed.get("plan"):
            plan = str(parsed.get("plan", "")).strip()
        else:
            plan = raw

        if not plan:
            return "update_plan: invalid", {
                "error": "update_plan needs a non-empty `plan` (the full updated checklist as markdown).",
                "exit_code": 1,
            }

        plan = plan[:8192]
        done = plan.count("- [x]") + plan.count("- [X]")
        total = done + plan.count("- [ ]")
        desc = f"update_plan: {done}/{total} done" if total else "update_plan"
        result = {
            "plan_update": {"plan": plan},
            "output": f"Plan updated ({done}/{total} steps complete)." if total else "Plan updated.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s", desc)
        return desc, result