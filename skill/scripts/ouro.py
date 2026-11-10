#!/usr/bin/env python3
# Ouroboros (ouro) - Copyright (c) 2026 Chetas P. Licensed under the PolyForm Noncommercial License 1.0.0 (see LICENSE).
"""ouro - Ouroboros: goal -> task graph (DAG) -> loop until verified, with a local model and a live graph.

State lives in <workspace>/.ouro/ (state.json, graph.svg, graph.html, graph.md, log.jsonl).
Python 3.9+, standard library only.

Environment:
  OURO_HOME         install dir (default ~/.ouroboros); helper scripts are looked up in $OURO_HOME/bin
  OURO_OLLAMA_URL   Ollama endpoint (default http://127.0.0.1:11434)
  OURO_MODEL        planner model (default qwen2.5:3b)
  OURO_LAYA_GUARD   path to the optional laya-guard CLI (default: found on PATH or $OURO_HOME/bin)
  OURO_MAX_ITER     loop cap (default 30)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("OURO_HOME", str(Path.home() / ".ouroboros")))
BIN = HOME / "bin"
OLLAMA_URL = os.environ.get("OURO_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("OURO_MODEL", "qwen2.5:3b")
LAYA = os.environ.get("OURO_LAYA_GUARD") or shutil.which("laya-guard") or str(BIN / "laya-guard")


def helper(name: str) -> str | None:
    """Optional helper script (laya-serve, ollama-local) from $OURO_HOME/bin or PATH; None if absent."""
    p = BIN / name
    return str(p) if p.exists() else shutil.which(name)
DEFAULT_MAX_ITER = int(os.environ.get("OURO_MAX_ITER", "30"))
MAX_STALLS = 3
STATUS_COLOR = {
    "pending": ("#2b303b", "#8b93a5"),
    "in_progress": ("#4a3a10", "#ffb028"),
    "done": ("#12361f", "#3fb950"),
    "failed": ("#40161a", "#f85149"),
    "blocked": ("#2f1f45", "#a371f7"),
}

# --------------------------------------------------------------------------- state


def find_root(arg: str | None = None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("OURO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    cur = Path.cwd().resolve()
    for p in [cur, *cur.parents]:
        if (p / ".ouro" / "state.json").exists():
            return p
    return cur


def odir(root: Path) -> Path:
    d = root / ".ouro"
    d.mkdir(parents=True, exist_ok=True)
    git_exclude = root / ".git" / "info" / "exclude"
    try:
        if git_exclude.parent.is_dir():
            txt = git_exclude.read_text() if git_exclude.exists() else ""
            if ".ouro/" not in txt:
                git_exclude.write_text(txt.rstrip("\n") + "\n.ouro/\n")
    except OSError:
        pass
    return d


def load(root: Path) -> dict | None:
    p = root / ".ouro" / "state.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def save(root: Path, st: dict) -> None:
    d = odir(root)
    st["updated"] = time.time()
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(d / "state.json")
    render(root, st)


def log(root: Path, event: str, **kw) -> None:
    with (odir(root) / "log.jsonl").open("a") as f:
        f.write(json.dumps({"t": time.time(), "event": event, **kw}) + "\n")


def node_map(st: dict) -> dict:
    return {n["id"]: n for n in st["nodes"]}


def ready_nodes(st: dict) -> list:
    nm = node_map(st)
    return [
        n
        for n in st["nodes"]
        if n["status"] in ("pending", "failed") and all(nm[d]["status"] == "done" for d in n["deps"] if d in nm)
    ]


def all_done(st: dict) -> bool:
    return bool(st["nodes"]) and all(n["status"] == "done" for n in st["nodes"])


# --------------------------------------------------------------------------- safety

DENY = [
    r"\bgit\s+(push|commit|reset\s+--hard|clean\s+-[a-z]*f|rebase|merge)\b",
    r"\brm\s+-[a-z]*r[a-z]*f?\b",
    r"\bsudo\b",
    r"\b(curl|wget|nc|ncat|ssh|scp|rsync)\b",
    r"\b(kubectl|helm)\s+(apply|delete|rollout|upgrade)\b",
    r"\bdocker\s+(push|rm|system\s+prune)\b",
    r"\b(npm|yarn|pnpm)\s+publish\b",
    r"\b(terraform|pulumi)\s+(apply|destroy)\b",
    r">\s*/(etc|usr|System|Library)\b",
    r"\bdeploy\b",
]


def laya_check(text: str, kind: str, timeout: int = 60) -> dict:
    """Ask laya-guard (optional). Without it the deterministic denylist in safe_cmd is the only gate."""
    if not Path(LAYA).exists():
        return {"exit": 0, "decision": "allow", "reasons": ["laya-guard not installed: denylist only"]}
    try:
        p = subprocess.run(
            [LAYA, "check", "--kind", kind, "--json"],
            input=text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"decision": "error", "exit": -1, "reasons": [str(e)]}
    out: dict = {"exit": p.returncode, "decision": {0: "allow", 10: "review", 20: "block"}.get(p.returncode, "error")}
    try:
        j = json.loads(p.stdout[p.stdout.index("{") :])
        out["reasons"] = j.get("reasons", [])
        out["laya_risk"] = (j.get("laya") or {}).get("risk")
    except (ValueError, json.JSONDecodeError):
        out["reasons"] = [p.stderr.strip()[-200:]]
    return out


def safe_cmd(cmd: str) -> tuple[bool, str]:
    for pat in DENY:
        if re.search(pat, cmd, re.I):
            return False, f"denylist: {pat}"
    v = laya_check(cmd, "command", timeout=30)
    if v["decision"] != "allow":
        return False, f"laya-guard {v['decision']}: {'; '.join(v.get('reasons', []))[:160]}"
    return True, "ok"


# --------------------------------------------------------------------------- planner (local model)

PLANNER_SYSTEM = (
    "You are a senior engineering planner. Decompose the user's goal into 4-8 concrete work nodes forming a "
    "dependency graph (DAG). Rules: ids are short lowercase slugs; deps reference EARLIER ids only; each title is an "
    "imperative action under 80 characters; 'verify' is one shell command that exits 0 only if that node's outcome is "
    "true (a test, build or grep command) or an empty string if unknown; never include git commit/push, deploys, "
    "network calls or destructive commands. Also give 2-6 observable, testable acceptance criteria. Output JSON only."
)
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "acceptance": {"type": "array", "items": {"type": "string"}},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "deps": {"type": "array", "items": {"type": "string"}},
                    "verify": {"type": "string"},
                },
                "required": ["id", "title", "deps", "verify"],
            },
        },
    },
    "required": ["acceptance", "nodes"],
}


def model_up() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/version", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def ensure_model_server() -> bool:
    if model_up():
        return True
    starter = helper("ollama-local")  # optional wrapper; otherwise start Ollama yourself: `ollama serve`
    if starter:
        subprocess.run([starter, "start"], capture_output=True, timeout=60)
    return model_up()


SKIP_DIRS = {".git", "node_modules", ".ouro", "dist", "build", ".next", "__pycache__", ".venv", "venv", ".tools", "coverage", ".turbo"}


def repo_context(root: Path, max_entries: int = 70) -> str:
    """Compact workspace map so the small model plans against real paths instead of inventing them."""
    entries: list = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = Path(base).relative_to(root)
        depth = len(rel.parts)
        if depth > 3:
            dirs[:] = []
            continue
        for f in sorted(files)[:12]:
            if depth <= 2 or f in ("package.json", "pyproject.toml", "go.mod", "Cargo.toml"):
                entries.append(str(rel / f) if depth else f)
        if len(entries) >= max_entries:
            break
    scripts = ""
    pj = root / "package.json"
    if pj.exists():
        try:
            scripts = "package.json scripts: " + ", ".join(json.loads(pj.read_text()).get("scripts", {}).keys())
        except (OSError, ValueError):
            pass
    return "\n".join(entries[:max_entries]) + ("\n" + scripts if scripts else "")


def ask_model(goal: str, root: Path | None = None) -> dict | None:
    if not ensure_model_server():
        return None
    ctx = repo_context(root) if root else ""
    body = {
        "model": MODEL,
        "stream": False,
        "format": PLAN_SCHEMA,
        "keep_alive": "30m",
        "options": {"temperature": 0.2, "num_ctx": 8192},
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": "WORKSPACE FILES (only reference paths that appear here; say 'create' for new files):\n" + (ctx or "(empty workspace)") + "\n\nGOAL:\n" + goal[:6000]},
        ],
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    for attempt in (1, 2):  # cold model load or malformed JSON from a 3B model: one retry before falling back
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(json.loads(r.read())["message"]["content"])
        except (urllib.error.URLError, OSError, KeyError, ValueError, json.JSONDecodeError):
            if attempt == 1:
                time.sleep(3)
    return None


FALLBACK_NODES = [
    ("understand", "Understand the goal, constraints and the existing code involved", [], ""),
    ("design", "Design the smallest correct change and list risks", ["understand"], ""),
    ("implement", "Implement the change in minimal diffs", ["design"], ""),
    ("test", "Add or update tests covering the acceptance criteria", ["implement"], ""),
]


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:24] or "step"


def build_nodes(plan: dict | None) -> tuple[list, list, str]:
    raw = []
    source = "local-model"
    if plan and isinstance(plan.get("nodes"), list) and plan["nodes"]:
        for n in plan["nodes"][:9]:
            if isinstance(n, dict) and n.get("title"):
                raw.append((slug(str(n.get("id") or n["title"])), str(n["title"])[:100], list(n.get("deps") or []), str(n.get("verify") or "").strip()))
        acceptance = [str(a)[:160] for a in (plan.get("acceptance") or [])][:6]
    else:
        source = "fallback-template"
        raw = list(FALLBACK_NODES)
        acceptance = ["Goal behaviour works as described", "Tests for the change pass", "No unrelated files changed"]
    # Hard policy filter: standing rules are local-only, no commit/push/PR/merge/deploy. Drop such nodes in code.
    forbidden = re.compile(r"\b(deploy\w*|publish\w*|release|push\w*|commit\w*|merge\w*|pull request|open a pr|production)\b", re.I)
    raw = [r for r in raw if not forbidden.search(r[1]) and not forbidden.search(r[0])] or list(FALLBACK_NODES)
    seen: set = set()
    nodes: list = []
    for nid, title, deps, verify in raw:
        base, i = nid, 2
        while nid in seen:
            nid, i = f"{base}-{i}", i + 1
        deps = [slug(d) for d in deps if slug(d) in seen]  # earlier ids only -> acyclic by construction
        seen.add(nid)
        # The small model's command is only a HINT: it never runs automatically. The working agent adopts a real
        # command with `ouro set-verify`, which makes it trusted (still safety-checked before every run).
        nodes.append({"id": nid, "title": title, "deps": deps, "verify": None, "hint": verify or None, "status": "pending", "evidence": "", "attempts": 0, "kind": "work"})
    used = {d for n in nodes for d in n["deps"]}
    leaves = [n["id"] for n in nodes if n["id"] not in used] or [n["id"] for n in nodes]
    nodes.append({"id": "verify", "title": "Run verification loop: build, types, lint, tests; show acceptance criteria are met", "deps": leaves, "verify": None, "status": "pending", "evidence": "", "attempts": 0, "kind": "gate"})
    nodes.append({"id": "review", "title": "Self-review the diff, guard output with laya-guard, summarize (no commit/push)", "deps": ["verify"], "verify": None, "status": "pending", "evidence": "", "attempts": 0, "kind": "gate"})
    return nodes, acceptance, source


# --------------------------------------------------------------------------- graph render


def levels(st: dict) -> dict:
    nm = node_map(st)
    memo: dict = {}

    def lv(i: str) -> int:
        if i in memo:
            return memo[i]
        memo[i] = 0
        deps = [d for d in nm[i]["deps"] if d in nm]
        memo[i] = 1 + max(lv(d) for d in deps) if deps else 0
        return memo[i]

    return {n["id"]: lv(n["id"]) for n in st["nodes"]}


def render(root: Path, st: dict) -> None:
    d = odir(root)
    W, H, GX, GY, PAD = 250, 64, 70, 22, 30
    if not st["nodes"]:  # still planning: placeholder graph
        st = {**st, "nodes": [{"id": "planning", "title": "Planning the task graph with the local model...", "deps": [], "status": "in_progress", "kind": "work", "evidence": "", "attempts": 0, "verify": None}]}
    lvl = levels(st)
    cols: dict = {}
    for n in st["nodes"]:
        cols.setdefault(lvl[n["id"]], []).append(n["id"])
    pos = {}
    for l, ids in cols.items():
        for r, i in enumerate(ids):
            pos[i] = (PAD + l * (W + GX), 86 + r * (H + GY))
    width = PAD * 2 + (max(cols) + 1) * W + max(cols) * GX
    height = 86 + max(len(v) for v in cols.values()) * (H + GY) + PAD
    done = sum(1 for n in st["nodes"] if n["status"] == "done")
    total = len(st["nodes"])
    e = html.escape
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">']
    out.append('<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#586174"/></marker></defs>')
    out.append(f'<rect width="100%" height="100%" fill="#0d1017"/>')
    goal1 = textwrap.shorten(st["goal"].replace("\n", " "), 110)
    out.append(f'<text x="{PAD}" y="30" fill="#e6e9ef" font-size="16" font-weight="600">{e(goal1)}</text>')
    out.append(f'<text x="{PAD}" y="54" fill="#8b93a5" font-size="12">{e(st["status"].upper())} · {done}/{total} nodes · iteration {st["iteration"]}/{st["max_iterations"]} · planner: {e(st["planner"]["source"])}</text>')
    nm = node_map(st)
    for n in st["nodes"]:
        for dep in n["deps"]:
            if dep not in pos:
                continue
            x1, y1 = pos[dep][0] + W, pos[dep][1] + H / 2
            x2, y2 = pos[n["id"]][0], pos[n["id"]][1] + H / 2
            mx = (x1 + x2) / 2
            out.append(f'<path d="M{x1} {y1} C{mx} {y1} {mx} {y2} {x2-2} {y2}" fill="none" stroke="#586174" stroke-width="1.6" marker-end="url(#a)"/>')
    for n in st["nodes"]:
        x, y = pos[n["id"]]
        bg, fg = STATUS_COLOR.get(n["status"], STATUS_COLOR["pending"])
        out.append(f'<rect x="{x}" y="{y}" width="{W}" height="{H}" rx="10" fill="{bg}" stroke="{fg}" stroke-width="1.4"/>')
        out.append(f'<text x="{x+12}" y="{y+19}" fill="{fg}" font-size="10.5" font-weight="700">{e(n["id"].upper())} · {e(n["status"].replace("_"," "))}{" · gate" if n["kind"]=="gate" else ""}</text>')
        for k, line in enumerate(textwrap.wrap(n["title"], 34)[:2]):
            out.append(f'<text x="{x+12}" y="{y+37+k*15}" fill="#e6e9ef" font-size="12">{e(line)}</text>')
    out.append("</svg>")
    svg = "\n".join(out)
    (d / "graph.svg").write_text(svg)
    refresh = '<meta http-equiv="refresh" content="3">' if st["status"] in ("planning", "active") else ""
    (d / "graph.html").write_text(
        f'<!doctype html><meta charset="utf-8">{refresh}<title>Ouroboros</title><body style="margin:0;background:#0d1017;color:#e6e9ef">{svg}'
        f'<pre style="padding:16px 30px;color:#8b93a5;font:12px ui-monospace,monospace;white-space:pre-wrap">Acceptance:\n' + "\n".join(f"  - {e(a)}" for a in st.get("acceptance", [])) + "</pre></body>"
    )
    m = ["```mermaid", "flowchart LR"]
    for n in st["nodes"]:
        m.append(f'  {n["id"].replace("-","_")}["{n["id"]}: {n["title"][:48].replace(chr(34), chr(39))} ({n["status"]})"]:::{n["status"]}')
    for n in st["nodes"]:
        for dep in n["deps"]:
            m.append(f'  {dep.replace("-","_")} --> {n["id"].replace("-","_")}')
    for s, (bg, fg) in STATUS_COLOR.items():
        m.append(f"  classDef {s} fill:{bg},stroke:{fg},color:#e6e9ef")
    m.append("```")
    (d / "graph.md").write_text("\n".join(m) + "\n")


# --------------------------------------------------------------------------- commands


def new_run(root: Path, goal: str, max_iter: int, use_model: bool) -> dict:
    st = {
        "id": time.strftime("%Y%m%d-%H%M%S"),
        "goal": goal.strip(),
        "created": time.time(),
        "status": "planning",
        "iteration": 0,
        "max_iterations": max_iter,
        "stalls": 0,
        "sig": "",
        "acceptance": [],
        "nodes": [],
        "inbox": [],
        "planner": {"model": MODEL, "source": "pending"},
        "guard": {},
    }
    existing = load(root)
    if existing and existing.get("status") in ("planning", "active", "blocked"):
        hist = odir(root) / "history"
        hist.mkdir(exist_ok=True)
        (hist / f"{existing['id']}.json").write_text(json.dumps(existing, indent=2))
    save(root, st)
    v = laya_check(goal[:8000], "prompt")
    st["guard"] = {"decision": v["decision"], "reasons": v.get("reasons", []), "laya_risk": v.get("laya_risk")}
    if v["decision"] == "block":
        st["status"] = "blocked"
        st["block_reason"] = "laya-guard blocked the goal text: " + "; ".join(v.get("reasons", []))
        save(root, st)
        return st
    plan = ask_model(goal, root) if use_model else None
    st["nodes"], st["acceptance"], st["planner"]["source"] = build_nodes(plan)
    st["status"] = "active"
    save(root, st)
    log(root, "start", goal=goal[:200], source=st["planner"]["source"], guard=v["decision"])
    return st


def cmd_start(a) -> int:
    root = find_root(a.root)
    goal = Path(a.file).read_text() if a.file else " ".join(a.goal)
    if not goal.strip():
        print("goal is empty", file=sys.stderr)
        return 2
    st = load(root)
    if st and st["status"] in ("planning", "active") and not a.replace:
        print(f"an active run exists ({st['id']}); use --replace to start a new one, or `ouro add`/inbox to extend it")
        return 1
    st = new_run(root, goal, a.max, not a.no_model)
    if not a.quiet:
        print_status(root, st)
    return 0 if st["status"] == "active" else 3


def print_status(root: Path, st: dict) -> None:
    done = sum(1 for n in st["nodes"] if n["status"] == "done")
    print(f"GOAL: {st['goal'][:120]}")
    label = "GOAL_ACHIEVED" if st["status"] == "achieved" else st["status"].upper()
    print(f"STATE: {label}  nodes {done}/{len(st['nodes'])}  iteration {st['iteration']}/{st['max_iterations']}  planner={st['planner']['source']}")
    if st.get("block_reason"):
        print(f"BLOCKED: {st['block_reason']}")
    if st.get("guard", {}).get("decision") == "review":
        print(f"GUARD REVIEW: {st['guard']['reasons']} (ask the user before acting on this goal)")
    for n in st["nodes"]:
        mark = {"done": "x", "in_progress": ">", "failed": "!", "blocked": "#"}.get(n["status"], " ")
        dep = f" <- {','.join(n['deps'])}" if n["deps"] else ""
        extra = f"   verify: {n['verify']}" if n.get("verify") else (f"   (unverified; model hint: {n['hint']})" if n.get("hint") else "")
        print(f"  [{mark}] {n['id']}: {n['title']}{dep}{extra}")
    if st.get("acceptance"):
        print("ACCEPTANCE:")
        for x in st["acceptance"]:
            print(f"  - {x}")
    if st.get("inbox"):
        print(f"INBOX ({len(st['inbox'])} later messages to fold in):")
        for x in st["inbox"][-5:]:
            print(f"  - {x[:120]}")
    r = ready_nodes(st)
    if st["status"] == "active" and r:
        print("READY: " + ", ".join(n["id"] for n in r))
    print(f"GRAPH: file://{root}/.ouro/graph.html")


def need(root: Path) -> dict:
    st = load(root)
    if not st:
        print("no active run; start one with: ouro start \"<goal>\"", file=sys.stderr)
        sys.exit(2)
    return st


def cmd_status(a) -> int:
    root = find_root(a.root)
    st = need(root)
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print_status(root, st)
    return 0


def cmd_wait(a) -> int:
    root = find_root(a.root)
    end = time.time() + a.timeout
    while time.time() < end:
        st = load(root)
        if st and st["status"] != "planning":
            print_status(root, st)
            return 0
        time.sleep(1)
    print("still planning (timed out waiting)", file=sys.stderr)
    return 1


def cmd_next(a) -> int:
    root = find_root(a.root)
    st = need(root)
    r = ready_nodes(st)
    if not r:
        print("no ready nodes" + (" - goal achieved" if st["status"] == "achieved" else ""))
        return 0
    n = r[0]
    if a.claim:
        n["status"] = "in_progress"
        save(root, st)
    print(f"{n['id']}: {n['title']}" + (f"\n  verify: {n['verify']}" if n.get("verify") else ""))
    return 0


def mark(a, status: str) -> int:
    root = find_root(a.root)
    st = need(root)
    n = node_map(st).get(a.id)
    if not n:
        print(f"unknown node {a.id}", file=sys.stderr)
        return 2
    n["status"] = status
    if status == "failed":
        n["attempts"] += 1
    note = getattr(a, "evidence", None) or getattr(a, "reason", None)
    if note:
        n["evidence"] = note[:500]
    save(root, st)
    log(root, status, node=a.id, note=(note or "")[:200])
    print(f"{a.id} -> {status}")
    return 0


def verify_env() -> dict:
    """Hooks run with a minimal PATH. Put $OURO_HOME/bin (ouro, laya-guard) first so verify
    commands behave the same in the stop hook as in an interactive shell."""
    env = dict(os.environ)
    base = [str(BIN), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env["PATH"] = ":".join(dict.fromkeys(base + env.get("PATH", "").split(":")))
    return env


def run_verify(root: Path, st: dict, only: list | None = None) -> tuple[int, int]:
    passed = failed = 0
    for n in st["nodes"]:
        if not n.get("verify") or (only and n["id"] not in only):
            continue
        if n["status"] == "pending":
            continue
        ok, why = safe_cmd(n["verify"])
        if not ok:
            n["status"], n["evidence"] = "blocked", f"verify command refused: {why}"
            failed += 1
            continue
        try:
            p = subprocess.run(n["verify"], shell=True, cwd=root, capture_output=True, text=True, timeout=900, env=verify_env())
            tail = (p.stdout + p.stderr).strip()[-300:]
            if p.returncode == 0:
                n["status"], n["evidence"] = "done", f"verify passed: {tail[-160:]}"
                passed += 1
            else:
                n["status"], n["evidence"] = "failed", f"verify exit {p.returncode}: {tail}"
                n["attempts"] += 1
                failed += 1
        except subprocess.TimeoutExpired:
            n["status"], n["evidence"] = "failed", "verify timed out (900s)"
            n["attempts"] += 1
            failed += 1
    return passed, failed


def cmd_verify(a) -> int:
    root = find_root(a.root)
    st = need(root)
    p, f = run_verify(root, st, a.ids or None)
    save(root, st)
    log(root, "verify", passed=p, failed=f)
    print(f"verify: {p} passed, {f} failed")
    return 0 if f == 0 else 1


def cmd_add(a) -> int:
    root = find_root(a.root)
    st = need(root)
    nid = slug(a.id or a.title)
    if nid in node_map(st):
        nid += f"-{len(st['nodes'])}"
    deps = [d for d in (a.deps.split(",") if a.deps else []) if d in node_map(st)]
    new = {"id": nid, "title": a.title[:100], "deps": deps, "verify": a.verify, "status": "pending", "evidence": "", "attempts": 0, "kind": "work"}
    gate_i = next(i for i, n in enumerate(st["nodes"]) if n["id"] == "verify")
    st["nodes"].insert(gate_i, new)
    gate = st["nodes"][gate_i + 1]
    if nid not in gate["deps"]:
        gate["deps"].append(nid)
    if st["status"] == "achieved":
        st["status"] = "active"
    save(root, st)
    print(f"added {nid}")
    return 0


def cmd_set_verify(a) -> int:
    root = find_root(a.root)
    st = need(root)
    n = node_map(st).get(a.id)
    if not n:
        print(f"unknown node {a.id}", file=sys.stderr)
        return 2
    ok, why = safe_cmd(a.command)
    if not ok:
        print(f"refused: {why}", file=sys.stderr)
        return 3
    n["verify"] = a.command
    save(root, st)
    print(f"{a.id}: verify set (runs on `ouro verify` and automatically before the goal is declared achieved)")
    return 0


def cmd_graph(a) -> int:
    root = find_root(a.root)
    st = need(root)
    render(root, st)
    print(f"{root}/.ouro/graph.html\n{root}/.ouro/graph.svg\n{root}/.ouro/graph.md")
    if a.open:
        subprocess.run(["open", str(root / ".ouro" / "graph.html")])
    return 0


def cmd_abandon(a) -> int:
    root = find_root(a.root)
    st = need(root)
    st["status"] = "abandoned"
    save(root, st)
    print("run abandoned")
    return 0


def cmd_warm(a) -> int:
    if helper("laya-serve"):
        subprocess.run([helper("laya-serve"), "start"], capture_output=True)
    up = ensure_model_server()
    print("planner model server: " + ("up" if up else "NOT running (start it with `ollama serve`; plans fall back to a template)"))
    return 0


def cmd_doctor(a) -> int:
    ok = True

    def line(name: str, good: bool, extra: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'ok' if good else 'FAIL'}] {name} {extra}")

    print(f"[{'ok' if Path(LAYA).exists() else '--'}] laya-guard (optional) {LAYA}")
    line("model server", model_up(), f"{OLLAMA_URL} (start it with: ollama serve)")
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            names = [m["name"] for m in json.loads(r.read())["models"]]
        line(f"model {MODEL}", MODEL in names, str(names))
    except (urllib.error.URLError, OSError):
        line(f"model {MODEL}", False)
    hooks = Path.home() / ".cursor" / "hooks.json"
    line("cursor hooks.json", hooks.exists() and "ouro" in hooks.read_text(), str(hooks))
    return 0 if ok else 1


# --------------------------------------------------------------------------- hooks


def stdin_json() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def hook_root(j: dict) -> Path | None:
    roots = j.get("workspace_roots") or []
    if roots:
        return Path(roots[0])
    return find_root(None) if (Path.cwd() / ".ouro").exists() else None


def is_task(prompt: str) -> bool:
    words = prompt.split()
    if len(words) < 8:
        return False
    p = prompt.strip().lower()
    if len(words) < 40 and (p.endswith("?") or re.match(r"(what|whats|what's|how|why|which|who|when|where|can you|could you|does|do|is|are|show|tell|explain|list|count|give me)\b", p)):
        return False  # questions and lookups answer in one turn: no run, no loop
    return True


def cmd_hook_prompt(a) -> int:
    j = stdin_json()
    print(json.dumps({"continue": True}))
    sys.stdout.flush()
    prompt = j.get("prompt") or j.get("text") or ""
    root = hook_root(j)
    if not root or not root.is_dir() or not is_task(prompt):
        return 0
    for tool in ("laya-serve", "ollama-local"):
        if helper(tool):
            subprocess.Popen([helper(tool), "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    st = load(root)
    if st and st["status"] in ("planning", "active") and time.time() - st.get("updated", 0) < 1800:  # a run idle >30min is stale: new prompt replaces it
        st.setdefault("inbox", []).append(prompt[:600])
        save(root, st)
        return 0
    pf = odir(root) / "prompt.txt"
    pf.write_text(prompt)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "start", "--root", str(root), "--file", str(pf), "--quiet", "--replace"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


def followup(root: Path, st: dict) -> str:
    r = ready_nodes(st)
    lines = [
        f"[ouro {st['iteration']}/{st['max_iterations']}] Not achieved. Continue; don't ask.",
        "Ready nodes: " + (", ".join(f"{n['id']} ({n['title'][:60]})" for n in r) or "none (check failed/blocked nodes)"),
    ]
    bad = [n for n in st["nodes"] if n["status"] in ("failed", "blocked")]
    for n in bad[:3]:
        lines.append(f"{n['status'].upper()} {n['id']}: {n['evidence'][:200]}")
    lines.append("`ouro next --claim` -> work -> `ouro done ID --evidence ...`. No commit/push.")
    return "\n".join(lines)


def cmd_hook_stop(a) -> int:
    j = stdin_json()
    root = hook_root(j)
    st = load(root) if root else None
    if not st or st["status"] != "active" or j.get("status") in ("aborted", "error"):
        print("{}")
        return 0
    # Every iteration re-runs trusted verify commands, so a node the agent fixed flips failed -> done on its own
    # and a node marked done that no longer verifies flips back. Verification, not self-report, decides.
    vids = [n["id"] for n in st["nodes"] if n.get("verify") and n["status"] in ("done", "failed")]
    if vids:
        run_verify(root, st, vids)
    if all_done(st):
        st["status"] = "achieved"
        save(root, st)
        log(root, "achieved")
        print("{}")
        return 0
    sig = json.dumps([(n["id"], n["status"], n["attempts"]) for n in st["nodes"]])
    st["stalls"] = st["stalls"] + 1 if sig == st.get("sig") else 0
    st["sig"] = sig
    st["iteration"] += 1
    if st["iteration"] >= st["max_iterations"] or st["stalls"] >= MAX_STALLS:
        st["status"] = "blocked"
        st["block_reason"] = "iteration cap reached" if st["iteration"] >= st["max_iterations"] else f"no progress for {MAX_STALLS} loops"
        save(root, st)
        log(root, "blocked", reason=st["block_reason"])
        print("{}")
        return 0
    save(root, st)
    log(root, "loop", iteration=st["iteration"])
    print(json.dumps({"followup_message": followup(root, st)}))
    return 0


def cmd_hook_shell(a) -> int:
    j = stdin_json()
    cmd = j.get("command", "")
    if not cmd.strip():
        print(json.dumps({"permission": "allow"}))
        return 0
    v = laya_check(cmd, "command", timeout=20)
    reasons = "; ".join(v.get("reasons", []))[:200]
    if v["decision"] == "block":
        print(json.dumps({"permission": "deny", "user_message": f"laya-guard blocked: {reasons}", "agent_message": f"laya-guard BLOCKED this command ({reasons}). Do not retry via rephrasing or another tool; continue without it or report to the user."}))
    elif v["decision"] == "review":
        print(json.dumps({"permission": "ask", "user_message": f"laya-guard flagged: {reasons}", "agent_message": "laya-guard returned REVIEW; waiting for the user."}))
    else:
        print(json.dumps({"permission": "allow"}))
    return 0


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(prog="ouro", description="Ouroboros: goal -> graph -> loop -> verified")
    ap.add_argument("--root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("--root", default=argparse.SUPPRESS)
        p.set_defaults(fn=fn)
        return p

    p = add("start", cmd_start)
    p.add_argument("goal", nargs="*")
    p.add_argument("--file")
    p.add_argument("--max", type=int, default=DEFAULT_MAX_ITER)
    p.add_argument("--no-model", action="store_true")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p = add("status", cmd_status)
    p.add_argument("--json", action="store_true")
    p = add("wait", cmd_wait)
    p.add_argument("--timeout", type=int, default=120)
    p = add("next", cmd_next)
    p.add_argument("--claim", action="store_true")
    p = add("done", lambda a: mark(a, "done"))
    p.add_argument("id")
    p.add_argument("--evidence")
    p = add("fail", lambda a: mark(a, "failed"))
    p.add_argument("id")
    p.add_argument("--reason")
    p = add("block", lambda a: mark(a, "blocked"))
    p.add_argument("id")
    p.add_argument("--reason")
    p = add("verify", cmd_verify)
    p.add_argument("ids", nargs="*")
    p = add("set-verify", cmd_set_verify)
    p.add_argument("id")
    p.add_argument("command")
    p = add("skip", lambda a: mark(a, "done"))
    p.add_argument("id")
    p.add_argument("--evidence", default="skipped: not applicable to this workspace")
    p = add("add", cmd_add)
    p.add_argument("title")
    p.add_argument("--id")
    p.add_argument("--deps")
    p.add_argument("--verify")
    p = add("graph", cmd_graph)
    p.add_argument("--open", action="store_true")
    add("abandon", cmd_abandon)
    add("warm", cmd_warm)
    add("doctor", cmd_doctor)
    add("hook-prompt", cmd_hook_prompt)
    add("hook-stop", cmd_hook_stop)
    add("hook-shell", cmd_hook_shell)

    a = ap.parse_args()
    if not hasattr(a, "root"):
        a.root = None
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
