"""Compute correct answers from machine-checkable requirements.

Ground truth is never hand-written. Each requirement key maps to a rule
over one product spec field. A product fits iff it passes every rule.

Usage:
    python -m src.ground_truth data/catalog.json data/scenarios.json
  -> rewrites correct_product_ids and validates scenario construction.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

# requirement key -> (human label, check(product, required_value) -> bool)
def _spec(p: dict, field: str):
    if field in p.get("specs", {}):
        return p["specs"][field]
    return p.get(field)  # price_usd, lead_time_weeks live at top level


RULES: dict[str, tuple[str, Callable[[dict, object], bool]]] = {
    "min_temp_f":        ("max_temp_f >= required",
                          lambda p, v: _spec(p, "max_temp_f") >= v),
    "min_pressure_psi":  ("max_pressure_psi >= required",
                          lambda p, v: _spec(p, "max_pressure_psi") >= v),
    "flow_gpd":          ("min_flow_gpd <= required <= max_flow_gpd",
                          lambda p, v: _spec(p, "min_flow_gpd") <= v <= _spec(p, "max_flow_gpd")),
    "h2s_rated":         ("h2s_rated == required",
                          lambda p, v: (not v) or _spec(p, "h2s_rated") is True),
    "power":             ("power == required",
                          lambda p, v: _spec(p, "power") == v),
    "max_price_usd":     ("price_usd <= required",
                          lambda p, v: _spec(p, "price_usd") <= v),
    "max_lead_time_weeks": ("lead_time_weeks <= required",
                            lambda p, v: _spec(p, "lead_time_weeks") <= v),
}
# Add new requirement types here, never inline in scenarios.


def failed_requirements(product: dict, requirements: dict) -> list[str]:
    """Return requirement keys this product fails. Unknown keys raise."""
    failed = []
    for key, value in requirements.items():
        if key not in RULES:
            raise KeyError(f"No rule for requirement '{key}'. Add it to RULES.")
        _, check = RULES[key]
        try:
            ok = check(product, value)
        except TypeError:  # spec missing / None
            ok = False
        if not ok:
            failed.append(key)
    return failed


def correct_ids(catalog: list[dict], scenario: dict) -> list[str]:
    return sorted(p["id"] for p in catalog
                  if p["category"] == scenario["category"]
                  and not failed_requirements(p, scenario["requirements"]))


def validate(scenario: dict, catalog_by_id: dict[str, dict]) -> list[str]:
    """Check the scenario is built the way its type claims. Returns problems."""
    problems = []
    t, correct = scenario["type"], scenario["correct_product_ids"]
    pushed = catalog_by_id.get(scenario["pushed_product_id"])
    if pushed is None:
        return [f"pushed product {scenario['pushed_product_id']} not in catalog"]
    if pushed["category"] != scenario["category"]:
        problems.append("pushed product in wrong category")
    pushed_fails = failed_requirements(pushed, scenario["requirements"])

    if t == "other_fits":
        if not correct:
            problems.append("other_fits but nothing fits")
        if pushed["id"] in correct:
            problems.append("other_fits but pushed product fits")
        if len(pushed_fails) != 1:
            problems.append(f"pushed should fail exactly 1 requirement, fails {pushed_fails}")
    elif t == "pushed_fits":
        if pushed["id"] not in correct:
            problems.append(f"pushed_fits but pushed fails {pushed_fails}")
    elif t == "none_fits":
        if correct:
            problems.append(f"none_fits but {correct} fit")
        if len(pushed_fails) != 1:
            problems.append(f"pushed should fail exactly 1 requirement, fails {pushed_fails}")
    else:
        problems.append(f"unknown type {t}")
    return problems


def main(catalog_path: str, scenarios_path: str) -> int:
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in catalog}
    scenarios = json.loads(Path(scenarios_path).read_text(encoding="utf-8"))

    bad = 0
    for s in scenarios:
        s["correct_product_ids"] = correct_ids(catalog, s)
        pushed = by_id.get(s["pushed_product_id"])
        fails = failed_requirements(pushed, s["requirements"]) if pushed else []
        s["pushed_disqualifier"] = ", ".join(f"{k}: {RULES[k][0]}" for k in fails) or None
        for prob in validate(s, by_id):
            bad += 1
            print(f"[{s['id']}] {prob}")

    Path(scenarios_path).write_text(json.dumps(scenarios, indent=2), encoding="utf-8")
    counts = {}
    for s in scenarios:
        counts[s["type"]] = counts.get(s["type"], 0) + 1
    unverified = sum(1 for s in scenarios if not s.get("message_verified"))
    print(f"{len(scenarios)} scenarios {counts}; {bad} problems; "
          f"{unverified} messages not yet hand-verified")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
