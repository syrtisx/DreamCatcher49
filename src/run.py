"""Run the agent over scenarios x conditions x models x reps.

    python -m src.run --dry-run                       # plan + token estimate, no calls
    python -m src.run --models claude-haiku-5-5 --limit 20 --reps 1 --out results/pilot
    python -m src.run                                 # full run from config.yaml

Resumable: run_ids already finished in the output folder are skipped.
Infra errors (rate limits, network) are retried on the next invocation;
behavioral outcomes (e.g. hitting the tool-turn cap) are kept, never retried.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

from .agent import make_run_id, run_conversation
from .common import load_config, load_env, load_json, read_runs
from .tools import Catalog


def make_client():
    import anthropic
    return anthropic.AsyncAnthropic(max_retries=2)


def select_scenarios(scenarios: list[dict], limit: int | None, types: list[str] | None):
    pool = [s for s in scenarios if not types or s["type"] in types]
    if not limit or limit >= len(pool):
        return pool
    by_type = defaultdict(list)
    for s in pool:
        by_type[s["type"]].append(s)
    queues = [by_type[t] for t in sorted(by_type)]
    out, i = [], 0
    while len(out) < limit:  # round-robin across types = stratified sample
        q = queues[i % len(queues)]
        if i // len(queues) < len(q):
            out.append(q[i // len(queues)])
        i += 1
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--models", help="comma-separated; default: config agent_models")
    p.add_argument("--conditions", help="comma-separated ids; default: all")
    p.add_argument("--types", help="comma-separated scenario types; default: all")
    p.add_argument("--limit", type=int, help="number of scenarios (stratified by type)")
    p.add_argument("--reps", type=int)
    p.add_argument("--concurrency", type=int)
    p.add_argument("--out", help="output folder; default: config paths.raw_dir")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return p.parse_args(argv)


async def main_async(args) -> int:
    cfg = load_config(args.config)
    paths = cfg["paths"]
    catalog = Catalog(paths["catalog"])
    scenarios = load_json(paths["scenarios"])
    conditions = {c["id"]: c for c in load_json(paths["conditions"])}

    models = args.models.split(",") if args.models else cfg["agent_models"]
    cond_ids = args.conditions.split(",") if args.conditions else list(conditions)
    unknown = [c for c in cond_ids if c not in conditions]
    if unknown:
        sys.exit(f"Unknown condition(s): {unknown}")
    reps = args.reps or cfg["reps"]
    chosen = select_scenarios(scenarios, args.limit, args.types.split(",") if args.types else None)

    out_dir = Path(args.out or paths["raw_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = read_runs(out_dir)
    done = {rid for rid, r in existing.items() if not r.get("error")}

    tasks = [(s, conditions[c], m, r) for s in chosen for c in cond_ids
             for m in models for r in range(1, reps + 1)]
    todo = [t for t in tasks if make_run_id(t[0]["id"], t[1]["id"], t[2], t[3]) not in done]
    random.Random(cfg["seed"]).shuffle(todo)  # avoid time-of-run confounds

    print(f"Plan: {len(chosen)} scenarios x {len(cond_ids)} conditions x {len(models)} models "
          f"x {reps} reps = {len(tasks)} runs; {len(tasks) - len(todo)} done; {len(todo)} to run")
    print(f"Models: {models}\nConditions: {cond_ids}\nOutput: {out_dir}")

    # Token estimate from any successful runs we already have (e.g. the pilot).
    ref = [r for r in existing.values() if not r.get("error")] or \
          [r for r in read_runs(Path(paths["raw_dir"]).parent / "pilot").values() if not r.get("error")]
    if ref:
        by_model = defaultdict(list)
        for r in ref:
            by_model[r["model"]].append(r["usage"])
        print("Estimated tokens for remaining runs (from past runs; multiply by Console prices):")
        for m in models:
            u = by_model.get(m)
            if not u:
                print(f"  {m}: no past runs to estimate from")
                continue
            n = sum(1 for t in todo if t[2] == m)
            ai = sum(x["input_tokens"] for x in u) / len(u)
            ao = sum(x["output_tokens"] for x in u) / len(u)
            print(f"  {m}: {n} runs ~ {n * ai / 1e6:.2f}M input + {n * ao / 1e6:.2f}M output tokens")

    if args.dry_run or not todo:
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set (put it in .env)")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        return 0

    client = make_client()
    sem = asyncio.Semaphore(args.concurrency or cfg["concurrency"])
    files: dict[Path, object] = {}
    stats = {"done": 0, "errors": 0, "unparsed": 0, "truncated": 0}
    abort = asyncio.Event()
    t0 = time.time()
    every = max(1, len(todo) // 20)

    def write(rec: dict) -> None:
        path = out_dir / f"{rec['model']}_{rec['condition_id']}.jsonl"
        if path not in files:
            files[path] = path.open("a", encoding="utf-8")
        files[path].write(json.dumps(rec, ensure_ascii=False) + "\n")
        files[path].flush()

    async def worker(task) -> None:
        s, c, m, r = task
        if abort.is_set():
            return
        async with sem:
            if abort.is_set():
                return
            rec = await run_conversation(client, catalog, s, c, m, r,
                                         temperature=cfg.get("temperature"),
                                         max_tokens=cfg["max_tokens"],
                                         max_tool_turns=cfg["max_tool_turns"],
                                         thinking=cfg["agent_thinking"][m],
                                         effort=cfg["agent_effort"][m])
        write(rec)
        stats["done"] += 1
        stats["errors"] += bool(rec["error"])
        stats["unparsed"] += (not rec["error"] and rec["recommendation"] is None)
        stats["truncated"] += rec["truncated"]
        if rec["error"] and stats["errors"] <= 3:
            print(f"  error on {rec['run_id']}: {rec['error'][:200]}")
        if rec.get("fatal_error"):
            print(f"Non-retryable API error on {rec['run_id']}: {rec['error'][:500]}\n"
                  "Stopping now: fix the request, key or model, then rerun to resume.")
            abort.set()
        if stats["errors"] >= 10 and stats["errors"] > stats["done"] / 2:
            print("Too many errors (check key, credits, rate limits). Stopping; rerun to resume.")
            abort.set()
        if stats["done"] % every == 0 or stats["done"] == len(todo):
            print(f"  {stats['done']}/{len(todo)} | errors {stats['errors']} | "
                  f"unparsed {stats['unparsed']} | truncated {stats['truncated']} | "
                  f"{time.time() - t0:.0f}s")

    try:
        await asyncio.gather(*(worker(t) for t in todo))
    finally:
        for f in files.values():
            f.close()
    print("Finished." if not abort.is_set() else "Aborted.", stats)
    return 1 if stats["errors"] else 0


def main(argv=None) -> int:
    load_env()
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
