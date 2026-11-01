#!/usr/bin/env python3
"""contexttax: where your Claude Code usage actually goes.

Reads the session logs Claude Code keeps on this computer (~/.claude/projects) and shows
how much of your usage went to long conversations, compared with the causes people
usually blame. Prints numbers only. Reads no conversation text into the output, sends
nothing anywhere, writes nothing.

Usage is weighted by API prices, the closest public stand-in for how plan limits are
counted: per model, Opus 5, Sonnet 3, Haiku 1; within a model, input 1, cache write
1.25 (5-minute) or 2 (1-hour), cache read 0.1, output 5.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

__version__ = "0.1.2"

BIG_CONTEXT = 200_000  # where "long" starts in the report
SUMMARY = 20_000  # tokens a fresh start would carry over
BIG_RESULT_CHARS = 40_000
CHARS_PER_TOKEN = 3.6


def model_weight(model: str) -> float:
    m = model.lower()
    return 5.0 if "opus" in m else 1.0 if "haiku" in m else 3.0


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def result_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                n += len(c.get("text", ""))
            elif isinstance(c, dict) and c.get("type") == "image":
                n += 6000
        return n
    return 0


class Turn:
    __slots__ = ("t", "model", "side", "context", "cost", "write_cost", "parts", "write_1h")

    def __init__(self, t, model, side, usage):
        self.t, self.model, self.side = t, model, side
        cc = usage.get("cache_creation") or {}
        w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        w5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
        written = usage.get("cache_creation_input_tokens", 0) or 0
        if not w1h and not w5m:
            w5m = written
        read = usage.get("cache_read_input_tokens", 0) or 0
        fresh = usage.get("input_tokens", 0) or 0
        out = usage.get("output_tokens", 0) or 0
        mw = model_weight(model)
        self.parts = {"cache reads": read * 0.1 * mw, "cache writes": (w5m * 1.25 + w1h * 2) * mw, "output": out * 5 * mw, "fresh input": fresh * mw}
        self.cost = sum(self.parts.values())
        self.write_cost = self.parts["cache writes"]
        self.write_1h = bool(w1h)
        self.context = fresh + written + read


def read_session(path: str):
    """Model calls (deduplicated), tool calls and tool result sizes from one log."""
    turns, uses, results, seen = [], {}, {}, set()
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            if d.get("type") == "assistant" and m.get("usage"):
                key = m.get("id") or d.get("requestId")
                model = m.get("model") or ""
                if key not in seen and not model.startswith("<"):
                    seen.add(key)
                    turns.append(Turn(parse_time(d.get("timestamp")), model, bool(d.get("isSidechain")), m["usage"]))
                for c in m.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        uses[c.get("id")] = (c.get("name"), c.get("input") or {}, bool(d.get("isSidechain")))
            elif d.get("type") == "user" and isinstance(m.get("content"), list):
                for c in m["content"]:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        results[c.get("tool_use_id")] = (parse_time(d.get("timestamp")), result_chars(c.get("content")))
    turns.sort(key=lambda x: x.t or 0)
    return turns, uses, results


def compact_window(settings_path: str = os.path.expanduser("~/.claude/settings.json")) -> int | None:
    """The auto-compact window in force for new sessions, if one is set: the environment
    variable wins over the user setting."""
    env = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if env and env.isdigit():
        return int(env)
    try:
        with open(settings_path) as fh:
            value = json.load(fh).get("autoCompactWindow")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, int) else None


def analyse(root: str, days: int | None):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() if days else None
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    if since:
        files = [f for f in files if os.path.getmtime(f) >= since]
    total = 0.0
    parts = Counter()
    by_model = Counter()
    session_costs = []
    big_context = 0.0
    fresh_start_saving = 0.0
    small_turns, big_turns = [], []
    contexts = []
    subagents = idle_rebuilds = 0.0
    reads = rereads = loops = 0
    big_result_carry = 0.0
    turn_count = 0
    first = last = None

    for f in files:
        turns, uses, results = read_session(f)
        if since:
            turns = [t for t in turns if t.t and t.t >= since]
        if not turns:
            continue
        s_cost, prev = 0.0, None
        for t in turns:
            total += t.cost
            s_cost += t.cost
            turn_count += 1
            by_model[t.model] += t.cost
            for k, v in t.parts.items():
                parts[k] += v
            if t.t:
                first = t.t if first is None else min(first, t.t)
                last = t.t if last is None else max(last, t.t)
            if t.side:
                subagents += t.cost
                continue
            contexts.append(t.context)
            if t.context >= BIG_CONTEXT:
                big_context += t.cost
                # Started over with a summary at BIG_CONTEXT, this turn would carry
                # (BIG_CONTEXT - SUMMARY) fewer tokens, re-read from cache.
                fresh_start_saving += (BIG_CONTEXT - SUMMARY) * 0.1 * model_weight(t.model)
            if 50_000 <= t.context < 100_000:
                small_turns.append(t.cost)
            elif t.context >= 500_000:
                big_turns.append(t.cost)
            # Most of the context written to cache at once = the cache was rebuilt.
            written = t.parts["cache writes"]
            if prev and prev.t and t.t and written > 0 and t.context > 20_000:
                life = 3600 if t.write_1h else 300
                if t.t - prev.t > life and written > 0.5 * t.cost:
                    idle_rebuilds += written
            prev = t
        session_costs.append(s_cost)

        # Re-reads of the same file and range; runs of three identical calls.
        seen_reads = Counter()
        sequence = []
        for tid, (name, inp, side) in uses.items():
            if name == "Read":
                reads += 1
                key = (inp.get("file_path"), inp.get("offset"), inp.get("limit"))
                seen_reads[key] += 1
                if seen_reads[key] > 1:
                    rereads += 1
            if not side:
                sequence.append(name + json.dumps({k: v for k, v in inp.items() if k != "description"}, sort_keys=True)[:400])
        run = 1
        for a, b in zip(sequence, sequence[1:]):
            run = run + 1 if a == b else 1
            if run == 3:
                loops += 1
        # Big tool results, re-sent from cache on every later turn.
        main_times = [(t.t, model_weight(t.model)) for t in turns if not t.side and t.t]
        for tid, (when, chars) in results.items():
            if chars > BIG_RESULT_CHARS and when:
                later = sum(w for (tt, w) in main_times if tt > when)
                big_result_carry += chars / CHARS_PER_TOKEN * 0.1 * later

    if not total:
        return None
    session_costs.sort(reverse=True)
    top = session_costs[: max(1, len(session_costs) // 10)]
    contexts.sort()
    return {
        "days": min(days or 10**6, max(1, round(((last or 0) - (first or 0)) / 86400))) if first else 0,
        "sessions": len(session_costs),
        "turns": turn_count,
        "parts": {k: v / total for k, v in parts.items()},
        "models": {k: v / total for k, v in by_model.most_common(4)},
        "top_tenth_share": sum(top) / total,
        "biggest_session_share": session_costs[0] / total,
        "big_context_share": big_context / total,
        "median_context": contexts[len(contexts) // 2] if contexts else 0,
        "p90_context": contexts[int(0.9 * len(contexts))] if contexts else 0,
        "big_vs_small": (sum(big_turns) / len(big_turns)) / (sum(small_turns) / len(small_turns)) if big_turns and small_turns else None,
        "fresh_start_saving": min(fresh_start_saving / total, 0.95),
        "compact_window": compact_window(),
        "blamed": {
            "subagents": subagents / total,
            "cache rebuilt after a break": idle_rebuilds / total,
            "huge tool outputs": min(big_result_carry / total, 1.0),
        },
        "rereads": (rereads, reads),
        "loops": loops,
    }


def pct(x: float) -> str:
    return f"{100 * x:.0f}%" if x >= 0.01 or x == 0 else "<1%"


def report(r: dict) -> str:
    k = lambda n: f"{n / 1000:.0f}k"
    lines = [
        f"contexttax · Claude Code · last {r['days']} days · {r['sessions']} sessions · {r['turns']:,} model calls",
        "",
        f"  {pct(r['big_context_share'])} of your usage went to turns where the conversation was already over {k(BIG_CONTEXT)} tokens.",
        f"  Your biggest 10% of sessions used {pct(r['top_tenth_share'])}. One session alone used {pct(r['biggest_session_share'])}.",
    ]
    if r["big_vs_small"]:
        lines.append(f"  A turn at 500k+ context costs {r['big_vs_small']:.1f}x a turn at 50-100k.")
    lines += [
        f"  Median turn: {k(r['median_context'])} of context. One in ten: over {k(r['p90_context'])}.",
        "",
        f"  Starting fresh with a short summary at {k(BIG_CONTEXT)} would have saved about {pct(r['fresh_start_saving'])}.",
    ]
    window = r.get("compact_window")
    if window and window <= BIG_CONTEXT:
        lines.append(f"  Your autoCompactWindow is already {k(window)}, so new sessions compact before this point.")
    elif r["big_context_share"] >= 0.3:
        lines += [
            f'  The fix is one setting: add "autoCompactWindow": {BIG_CONTEXT} to ~/.claude/settings.json',
            "  (or use /autocompact). Claude Code then compacts at 200k instead of near the limit.",
        ]
    lines += [
        "",
        "  What people usually blame:",
        f"    re-reading the same file     {r['rereads'][0]} of {r['rereads'][1]} reads",
        f"    loops (same call 3 times)    {r['loops']}",
        f"    huge tool outputs            {pct(r['blamed']['huge tool outputs'])}",
        f"    subagents                    {pct(r['blamed']['subagents'])}",
        f"    cache rebuilt after a break  {pct(r['blamed']['cache rebuilt after a break'])}",
        "",
        "  Where it goes: " + ", ".join(f"{name} {pct(v)}" for name, v in sorted(r["parts"].items(), key=lambda x: -x[1]) if v >= 0.005),
        "  Models: " + ", ".join(f"{m} {pct(v)}" for m, v in r["models"].items() if v >= 0.005),
        "",
        "  Weighted by API prices. Read on this computer; nothing was sent anywhere.",
        "  To share it: contexttax --share",
        "  github.com/theadamdanielsson/contexttax",
    ]
    return "\n".join(lines)


def share(r: dict) -> str:
    """A few lines to paste anywhere, in the first person, ending with the command."""
    lines = [
        f"{pct(r['big_context_share'])} of my Claude Code usage went to turns where the conversation was already past {BIG_CONTEXT // 1000}k tokens.",
        f"My biggest 10% of sessions used {pct(r['top_tenth_share'])}.",
    ]
    rereads, reads = r["rereads"]
    if r["blamed"]["huge tool outputs"] < 0.05 and r["loops"] < 5 and (not reads or rereads / reads < 0.1):
        lines.append("Re-reads, loops and huge tool outputs barely registered.")
    lines.append("Find yours: uvx contexttax")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="contexttax", description="Where your Claude Code usage actually goes. Reads local logs only.")
    p.add_argument("--days", type=int, default=30, help="look back this many days (default 30; 0 = all)")
    p.add_argument("--json", action="store_true", help="print the numbers as JSON")
    p.add_argument("--share", action="store_true", help="a few lines to paste anywhere")
    p.add_argument("--path", default=os.path.expanduser("~/.claude/projects"), help="where Claude Code keeps its logs")
    p.add_argument("--version", action="version", version=__version__)
    a = p.parse_args(argv)
    if not os.path.isdir(a.path):
        print(f"No Claude Code logs found at {a.path}.", file=sys.stderr)
        return 1
    r = analyse(a.path, a.days or None)
    if not r:
        print("No model calls found in those logs.", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2) if a.json else share(r) if a.share else report(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
