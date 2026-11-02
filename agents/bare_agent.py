#!/usr/bin/env python3
"""Bare tool-loop agent for any OpenAI-compatible endpoint. No verifier, no skills, no memory.

This is the "without" arm: a model, three tools (bash, read_file, write_file) scoped to the
workdir, and a one-paragraph system prompt. Stdlib only, so it runs anywhere python3 does.

  python3 {evals_dir}/agents/bare_agent.py --prompt {prompt} --cwd {workdir} --usage {usage_file} \\
      --model {model} [--base-url https://api.vultrinference.com/v1] [--env-file PATH]

--trace FILE appends one JSON line per tool call ({"tool", "args", "exit"}) for trace rules.
Key: $VULTR_INFERENCE_API_KEY (or --key-env NAME), optionally loaded from --env-file (KEY=VALUE lines).
Usage file: model (as reported by the API), provider, api_calls, total_tokens, completed,
turn_exit_reason, estimated_cost_usd (only if --usd-per-mtok is given).
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

SYSTEM = ("You are an agent working in a project directory. Use the tools to inspect files and run "
          "commands, then complete the user's task. When finished, reply with a short final answer.")

TOOLS = [
    {"type": "function", "function": {
        "name": "bash", "description": "Run a bash command in the project directory. Returns exit code, stdout, stderr.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file (path relative to the project directory).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write (overwrite) a text file (path relative to the project directory).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]


def load_env(path: str | None) -> None:
    if not path or not Path(path).expanduser().exists():
        return
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("'\""))


def inside(cwd: Path, p: str) -> Path:
    q = (cwd / p).resolve()
    if q != cwd and cwd not in q.parents:
        raise ValueError(f"path escapes the project directory: {p}")
    return q


def run_tool(name: str, args: dict, cwd: Path, timeout: int) -> str:
    try:
        if name == "bash":
            r = subprocess.run(["bash", "-c", args["command"]], cwd=cwd, capture_output=True, text=True, timeout=timeout)
            out = f"exit={r.returncode}\n--- stdout\n{r.stdout}\n--- stderr\n{r.stderr}"
        elif name == "read_file":
            out = inside(cwd, args["path"]).read_text(errors="replace")
        elif name == "write_file":
            q = inside(cwd, args["path"]); q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text(args["content"]); out = f"wrote {len(args['content'])} chars to {args['path']}"
        else:
            out = f"unknown tool {name}"
    except subprocess.TimeoutExpired:
        out = f"timed out after {timeout}s"
    except Exception as e:  # report tool errors to the model, don't crash the loop
        out = f"error: {type(e).__name__}: {e}"
    return out if len(out) <= 20000 else out[:10000] + "\n...[truncated]...\n" + out[-10000:]


def chat(base: str, key: str, body: dict, retries: int = 4) -> dict:
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:500]
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(2 ** i * 3); continue
            raise RuntimeError(f"HTTP {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if i < retries - 1:
                time.sleep(2 ** i * 3); continue
            raise RuntimeError(f"network: {e}") from None
    raise RuntimeError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True); ap.add_argument("--cwd", required=True)
    ap.add_argument("--model", required=True); ap.add_argument("--usage")
    ap.add_argument("--base-url", default="https://api.vultrinference.com/v1")
    ap.add_argument("--provider", default="vultr")
    ap.add_argument("--key-env", default="VULTR_INFERENCE_API_KEY"); ap.add_argument("--env-file")
    ap.add_argument("--max-turns", type=int, default=30); ap.add_argument("--tool-timeout", type=int, default=60)
    ap.add_argument("--max-tokens", type=int, default=32768)  # reasoning models burn 4k+ on thinking alone
    ap.add_argument("--usd-per-mtok", type=float, help="blended price for a cost estimate")
    ap.add_argument("--trace", help="append one JSON line per tool call (run_evals {trace_file})")
    a = ap.parse_args()
    load_env(a.env_file)
    key = os.environ.get(a.key_env)
    if not key:
        print(f"bare_agent: ${a.key_env} not set", file=sys.stderr); return 2
    cwd = Path(a.cwd).resolve()
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": a.prompt}]
    u = {"provider": a.provider, "model": None, "api_calls": 0, "total_tokens": 0, "completed": False,
         "turn_exit_reason": None}
    final, rc = "", 0
    log = [] if os.environ.get("BARE_AGENT_LOG") else None  # dir for full transcripts (kept outside the workdir)
    try:
        for _ in range(a.max_turns):
            resp = chat(a.base_url, key, {"model": a.model, "messages": msgs, "tools": TOOLS,
                                          "tool_choice": "auto", "max_tokens": a.max_tokens, "temperature": 0})
            u["api_calls"] += 1; u["model"] = resp.get("model") or u["model"]
            u["total_tokens"] += (resp.get("usage") or {}).get("total_tokens") or 0
            ch = resp["choices"][0]; m = ch["message"]; finish = ch.get("finish_reason")
            calls = m.get("tool_calls") or []
            if log is not None:
                log.append({"finish_reason": finish, "message": m, "usage": resp.get("usage")})
            msgs.append({k: v for k, v in {"role": "assistant", "content": m.get("content") or "",
                                            "tool_calls": calls or None}.items() if v is not None})
            if not calls:
                final = m.get("content") or ""; u["completed"] = finish != "length"
                u["turn_exit_reason"] = finish or "stop"; break
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError as e:
                    out = f"error: arguments were not valid JSON: {e}"
                else:
                    out = run_tool(c["function"]["name"], args, cwd, a.tool_timeout)
                if a.trace:
                    ex = out.split("\n", 1)[0].removeprefix("exit=") if out.startswith("exit=") else None
                    with open(a.trace, "a") as tf:
                        tf.write(json.dumps({"tool": c["function"]["name"], "args": args,
                                             "exit": int(ex) if ex and ex.lstrip("-").isdigit() else None}) + "\n")
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        else:
            u["turn_exit_reason"] = "max_turns"; final = msgs[-1].get("content", "") if msgs[-1]["role"] == "assistant" else ""
    except Exception as e:
        u["turn_exit_reason"] = f"error: {e}"[:300]; print(f"bare_agent: {e}", file=sys.stderr); rc = 1
    if a.usd_per_mtok is not None:
        u["estimated_cost_usd"] = round(u["total_tokens"] / 1e6 * a.usd_per_mtok, 5)
    if a.usage:
        Path(a.usage).write_text(json.dumps(u))
    if log is not None:
        d = Path(os.environ["BARE_AGENT_LOG"]); d.mkdir(parents=True, exist_ok=True)
        (d / f"{cwd.name}-{int(time.time())}.json").write_text(json.dumps({"usage": u, "messages": msgs, "raw": log}, indent=1))
    print(final)
    return rc


if __name__ == "__main__":
    sys.exit(main())
