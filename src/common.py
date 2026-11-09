"""Shared helpers: config, .env, reading runs, rendering a case, stats."""
from __future__ import annotations

import json
import math
import os
import random
from pathlib import Path

import yaml


# ---- config / env --------------------------------------------------------
def load_config(path: str = "config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---- runs ----------------------------------------------------------------
def read_runs(raw_dir: str | Path) -> dict[str, dict]:
    """run_id -> record. A successful record always beats an errored one;
    otherwise the latest line wins. Partial lines (from a crash) are skipped."""
    runs: dict[str, dict] = {}
    for f in sorted(Path(raw_dir).glob("*.jsonl")):
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prev = runs.get(r["run_id"])
                if prev is None or prev.get("error") or not r.get("error"):
                    runs[r["run_id"]] = r
    return runs


def gradable(run: dict) -> bool:
    return not run.get("error") and bool(run.get("final_text"))


# ---- case rendering (shared by judge and hand labeler, so both see the same) --
# requirement key -> spec field(s) that decide it
SPEC_FIELDS = {
    "min_temp_f": ["max_temp_f"], "min_pressure_psi": ["max_pressure_psi"],
    "flow_gpd": ["min_flow_gpd", "max_flow_gpd"], "h2s_rated": ["h2s_rated"],
    "power": ["power"], "max_price_usd": ["price_usd"],
    "max_lead_time_weeks": ["lead_time_weeks"],
}


def _spec_value(product: dict, field: str):
    return product.get("specs", {}).get(field, product.get(field))


def _cell(v) -> str:
    s = v if isinstance(v, str) else json.dumps(v)
    return s.replace("|", "\\|").replace("\n", " ")


def _flat(out: dict) -> dict:
    """get_product output with nested dicts (specs) flattened to their inner keys."""
    flat = {}
    for k, v in out.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                flat[k2 if k2 not in out else f"{k}.{k2}"] = v2
        else:
            flat[k] = v
    return flat


def tool_table(calls: list[dict]) -> str:
    """Compact table of what the agent saw, built ONLY from the tool outputs (no catalog
    lookups). One row per call result, in call order; [n] matches the raw list below it.
    Fetched products show every returned field. Products seen only in list_products show
    id + name with fields "not fetched". Errors and repeat calls are kept."""
    gets = [_flat(c["output"]) for c in calls
            if c["name"] == "get_product" and isinstance(c["output"], dict)
            and "error" not in c["output"]]
    fetched = {g.get("id") for g in gets}
    fields = list(dict.fromkeys(k for g in gets for k in g if k not in ("id", "name"))) or ["specs"]
    rows, first_get, first_list = [], {}, {}
    for i, c in enumerate(calls, 1):
        out, name = c["output"], c["name"]
        err = out.get("error") if isinstance(out, dict) else None
        if name == "get_product" and not err:
            g = _flat(out)
            pid = g.get("id")
            note = f"repeat of [{first_get[pid]}]" if pid in first_get else ""
            first_get.setdefault(pid, i)
            rows.append([f"[{i}]", name, pid, g.get("name", "")]
                        + [g.get(f, "") for f in fields] + [note])
        elif name == "list_products" and not err:
            cat = out.get("category")
            note = f"repeat of [{first_list[cat]}]; " if cat in first_list else ""
            first_list.setdefault(cat, i)
            listed = out.get("products", [])
            n_only = sum(p.get("id") not in fetched for p in listed)
            rows.append([f"[{i}]", name, cat, ""] + [""] * len(fields)
                        + [f"{note}listed {len(listed)} products, {n_only} not fetched"])
            for p in listed:
                if p.get("id") not in fetched:
                    rows.append([f"[{i}]", name, p.get("id"), p.get("name", "")]
                                + ["not fetched"] * len(fields) + [note + "listed only"])
        else:
            arg = next(iter(c["input"].values()), "") if c["input"] else ""
            rows.append([f"[{i}]", name, arg, ""] + [""] * len(fields)
                        + [f"error: {err}" if err else "unrecognized tool output"])
    head = ["call", "tool", "id", "name"] + fields + ["note"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(_cell(x) for x in r) + " |" for r in rows]
    return "\n".join(lines)


def render_case(run: dict, scenario: dict, catalog_by_id: dict) -> dict:
    """Blind view of a run: no model, condition or system prompt."""
    from .ground_truth import failed_requirements

    calls = run.get("tool_calls", [])
    tools = [f"[{i}] {c['name']}({json.dumps(c['input'])}) -> {json.dumps(c['output'])}"
             for i, c in enumerate(calls, 1)]
    if tools:
        tools = (["Summary table, built only from the raw tool results below (the raw results "
                  "are authoritative). [n] is the call number.", tool_table(calls), "",
                  "Raw tool results, in call order:"] + tools)
    rec = run.get("recommendation")
    product = catalog_by_id.get(rec)
    fails = failed_requirements(product, scenario["requirements"]) if product else []
    fail_lines = [
        f"- {k}: customer requires {scenario['requirements'][k]!r}; product has "
        + ", ".join(f"{f}={_spec_value(product, f)!r}" for f in SPEC_FIELDS[k])
        for k in fails
    ]
    return {
        "customer_message": scenario["customer_message"],
        "requirements": json.dumps(scenario["requirements"]),
        "tool_outputs": "\n".join(tools) or "(none: the assistant made no tool calls)",
        "reply": run.get("final_text", ""),
        "recommendation": rec,
        "disclosure_applies": bool(fails),
        "failed_requirements": "\n".join(fail_lines),
    }


# ---- stats ---------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def cohen_kappa(a: list[int], b: list[int]) -> float:
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return float("nan") if pe == 1 else (po - pe) / (1 - pe)


def paired_bootstrap(diffs: list[float], iters: int = 2000, seed: int = 0) -> tuple[float, float]:
    """95% CI of the mean of per-scenario differences, resampling scenarios."""
    if not diffs:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(iters))
    return (means[int(0.025 * iters)], means[int(0.975 * iters) - 1])
