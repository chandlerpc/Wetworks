"""Coding brains: the model that decides what to change.

  local       free; the Ollama model on the MSI. Fine for small, well-described edits.
  claude-api  paid Claude through the API, inside the monthly budget cap.
  (Friday)    claude-code: Claude Code running headless on the Max plan.

Every brain returns the same shape, so the Code Splinter doesn't care which one ran:
  {"summary": "...", "edits": [{"path": "...", "content": "full new file"} | {"path": "...", "delete": true}]}
"""

import json
import re

SYSTEM = (
    "You are the Code cell in Wetworks, Patrick's multi-agent system. You make small, careful, "
    "working changes to a Python codebase. Follow the existing style. Add or update tests for "
    "behaviour you change. Never touch .github, .env files or secrets. Never invent APIs or files "
    "you haven't been shown; if you need a file you weren't given, say so in the summary instead."
)

PLAN_PROMPT = """Task:
{task}

Repository files:
{tree}

README (start):
{readme}

Which files do you need to read to do this task well? Reply with ONLY this JSON object:
{{"files": ["up to 8 paths from the list above"], "approach": "two or three sentences"}}"""

EDIT_PROMPT = """Task:
{task}

Approach: {approach}

Files (current contents):
{files}

{feedback}
Make the change. Reply with ONLY this JSON object:
{{"summary": "what you changed and why, for a pull request description",
  "edits": [{{"path": "relative/path.py", "content": "the COMPLETE new file content"}}]}}
Rules: at most {max_files} files; give complete file contents, never fragments or diffs; to delete a
file use {{"path": "...", "delete": true}}; keep unrelated code exactly as it is."""


def parse_json_obj(text: str) -> dict:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    for candidate in ([fence.group(1)] if fence else []) + [text] + [m.group(0) for m in [re.search(r"\{.*\}", text, re.S)] if m]:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return {}


def clean_edits(raw, max_files: int, max_chars: int = 80000) -> list[dict]:
    edits = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"].strip():
            continue
        if e.get("delete") is True:
            edits.append({"path": e["path"].strip(), "delete": True})
        elif isinstance(e.get("content"), str) and len(e["content"]) <= max_chars:
            edits.append({"path": e["path"].strip(), "content": e["content"]})
    return edits[:max_files]


class Brain:
    name = "brain"

    def __init__(self, llm, max_files: int = 6):
        self.llm = llm
        self.max_files = max_files

    def _ask(self, agent: str, prompt: str, max_tokens: int) -> str:
        raise NotImplementedError

    def plan(self, agent: str, task: str, tree: list[str], readme: str) -> dict:
        raw = self._ask(agent, PLAN_PROMPT.format(task=task, tree="\n".join(tree[:400]), readme=readme[:3000]), 600)
        data = parse_json_obj(raw)
        files = [f for f in data.get("files", []) if isinstance(f, str) and f in tree][:8]
        return {"files": files, "approach": str(data.get("approach", "")).strip()}

    def edit(self, agent: str, task: str, approach: str, files: dict[str, str], feedback: str = "") -> dict:
        shown = "\n\n".join(f"=== {p} ===\n{c}" for p, c in files.items()) or "(no files read)"
        fb = f"Your previous attempt failed its tests. Fix it. Test output:\n{feedback[-6000:]}\n" if feedback else ""
        raw = self._ask(agent, EDIT_PROMPT.format(task=task, approach=approach or "(none)", files=shown,
                                                  feedback=fb, max_files=self.max_files), 8000)
        data = parse_json_obj(raw)
        return {"summary": str(data.get("summary", "")).strip(), "edits": clean_edits(data.get("edits"), self.max_files)}


class LocalBrain(Brain):
    name = "local"

    def _ask(self, agent, prompt, max_tokens):
        return self.llm.ask_local(agent, prompt, system=SYSTEM, json_mode=True, max_tokens=max_tokens).text


class ClaudeApiBrain(Brain):
    name = "claude-api"

    def _ask(self, agent, prompt, max_tokens):
        # Non-essential: refused outright once the monthly budget cap is reached.
        return self.llm.ask(agent, prompt, tier="work", system=SYSTEM, max_tokens=max_tokens).text


BRAINS = {"local": LocalBrain, "claude-api": ClaudeApiBrain}


def make_brain(name: str, llm, max_files: int = 6) -> Brain:
    if name not in BRAINS:
        raise ValueError(f"Unknown code brain {name!r}; choose one of {', '.join(BRAINS)}")
    return BRAINS[name](llm, max_files=max_files)
