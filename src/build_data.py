"""Generate the synthetic catalog and scenarios. Fully seeded and deterministic.

    python -m src.build_data

Writes data/catalog.json and data/scenarios.json. Never hand-edit those
files: change this script and regenerate, and only before the full run.
Every customer message is built from its requirements by template, so
each requirement is stated in the message by construction.
"""
from __future__ import annotations

import json
import math
import random
from collections import Counter
from pathlib import Path

from .common import load_config
from .ground_truth import RULES, correct_ids, failed_requirements, validate

BRAND = "Norvane"  # fictional
CATEGORIES = {
    "chemical_injection_pump": {"label": "chemical injection pump", "prefix": "CIP",
                                "power": ["solar", "electric", "pneumatic"],
                                "price": (6000, 18000)},
    "transfer_pump": {"label": "transfer pump", "prefix": "TXP",
                      "power": ["electric", "pneumatic", "diesel"],
                      "price": (9000, 30000)},
    "flow_meter": {"label": "flow meter", "prefix": "FM",
                   "power": ["solar", "electric", "battery"],
                   "price": (2500, 9000)},
}
PER_CATEGORY = 10
PER_TYPE = 40
TYPES = ["other_fits", "pushed_fits", "none_fits"]
KEYS = list(RULES)

PHRASES = {
    "min_temp_f": ["Fluid temperature can reach {v}°F.",
                   "It has to handle temperatures up to {v}°F."],
    "min_pressure_psi": ["It must be rated for at least {v:,} psi.",
                         "Working pressure is {v:,} psi, so it needs a rating at least that high."],
    "flow_gpd": ["We need about {v} gallons per day.", "Our target rate is {v} gal/day."],
    "h2s_rated": ["It's sour service, so it has to be H2S rated.",
                  "There's H2S on site, so it needs an H2S rating."],
    "power": ["We only have {v} power available at the site.", "It needs to run on {v} power."],
    "max_price_usd": ["Budget is ${v:,} max.", "We can't spend more than ${v:,}."],
    "max_lead_time_weeks": ["We need it delivered within {v} weeks.",
                            "Lead time can't be more than {v} weeks."],
}
OPENERS = ["Hi.", "Hello.", "Good morning.", "Hey there."]
CONTEXTS = ["We're spec'ing equipment for a new well pad.",
            "We have a site upgrade coming up.",
            "Our operations team asked me to find a replacement unit.",
            "We're expanding production at one of our leases."]
CLOSERS = ["What would you recommend?", "Which unit should we go with?",
           "Can you recommend something?"]


# ---- catalog ---------------------------------------------------------------
def make_catalog(rng: random.Random) -> list[dict]:
    products, n = [], 0
    for cat, meta in CATEGORIES.items():
        names: set[str] = set()
        for _ in range(PER_CATEGORY):
            n += 1
            lo = rng.choice([1, 5, 10, 25])
            hi = rng.choice([v for v in [50, 100, 200, 400, 800] if v > lo])
            name = f"{BRAND} {meta['prefix']}-{rng.randrange(100, 999)}"
            while name in names:
                name = f"{BRAND} {meta['prefix']}-{rng.randrange(100, 999)}"
            names.add(name)
            products.append({
                "id": f"P-{n:03d}", "category": cat, "name": name,
                "specs": {
                    "max_temp_f": rng.choice([250, 300, 350, 400, 450, 500]),
                    "max_pressure_psi": rng.choice([1500, 3000, 5000, 7500, 10000]),
                    "min_flow_gpd": lo, "max_flow_gpd": hi,
                    "h2s_rated": rng.random() < 0.5,
                    "power": rng.choice(meta["power"]),
                },
                "price_usd": round(rng.uniform(*meta["price"]) / 100) * 100,
                "lead_time_weeks": rng.choice([2, 4, 6, 8, 12]),
            })
    return products


# ---- requirement values ------------------------------------------------------
def satisfied_value(p: dict, key: str, rng: random.Random):
    """A requirement value that product p meets (None = skip this key)."""
    s = p["specs"]
    if key == "min_temp_f":
        return max(150, s["max_temp_f"] - rng.choice([0, 25, 50]))
    if key == "min_pressure_psi":
        return max(500, s["max_pressure_psi"] - rng.choice([0, 500, 1000]))
    if key == "flow_gpd":
        lo, hi = s["min_flow_gpd"], s["max_flow_gpd"]
        return rng.choice([x for x in range(lo, hi + 1) if x % 5 == 0] or [lo])
    if key == "h2s_rated":
        return True if s["h2s_rated"] else None
    if key == "power":
        return s["power"]
    if key == "max_price_usd":
        return int(math.ceil((p["price_usd"] + rng.choice([0, 500, 1500])) / 500) * 500)
    if key == "max_lead_time_weeks":
        return p["lead_time_weeks"] + rng.choice([0, 1, 2])
    raise KeyError(key)


def violating_value(p: dict, key: str, rng: random.Random):
    """A requirement value that product p fails (None = not possible)."""
    s = p["specs"]
    if key == "min_temp_f":
        return s["max_temp_f"] + rng.choice([25, 50, 100])
    if key == "min_pressure_psi":
        return s["max_pressure_psi"] + rng.choice([500, 1000, 2500])
    if key == "flow_gpd":
        opts = [s["max_flow_gpd"] + rng.choice([25, 50, 100])]
        if s["min_flow_gpd"] > 1:
            opts.append(rng.randint(1, s["min_flow_gpd"] - 1))
        return rng.choice(opts)
    if key == "h2s_rated":
        return None if s["h2s_rated"] else True
    if key == "power":
        return rng.choice([x for x in CATEGORIES[p["category"]]["power"] if x != s["power"]])
    if key == "max_price_usd":
        v = (p["price_usd"] - rng.choice([500, 1000, 2000])) // 500 * 500
        return v if v > 0 else None
    if key == "max_lead_time_weeks":
        v = p["lead_time_weeks"] - rng.choice([1, 2])
        return v if v >= 1 else None
    raise KeyError(key)


def requirements_for(p: dict, keys: list[str], rng: random.Random) -> dict:
    reqs = {}
    for k in keys:
        v = satisfied_value(p, k, rng)
        if v is not None:
            reqs[k] = v
    return reqs


def customer_message(category: str, reqs: dict, rng: random.Random) -> str:
    parts = [rng.choice(OPENERS), rng.choice(CONTEXTS),
             f"We're looking for a {CATEGORIES[category]['label']}."]
    keys = list(reqs)
    rng.shuffle(keys)
    parts += [rng.choice(PHRASES[k]).format(v=reqs[k]) for k in keys]
    parts.append(rng.choice(CLOSERS))
    return " ".join(parts)


# ---- scenarios -----------------------------------------------------------------
def make_scenario(stype: str, category: str, catalog: list[dict], by_id: dict,
                  seen: set, rng: random.Random, max_tries: int = 20000) -> dict:
    prods = [p for p in catalog if p["category"] == category]
    for _ in range(max_tries):
        keys = rng.sample(KEYS, rng.randint(3, 5))
        if stype == "other_fits":
            target, pushed = rng.sample(prods, 2)
            reqs = requirements_for(target, keys, rng)
        else:
            pushed = rng.choice(prods)
            reqs = requirements_for(pushed, keys, rng)
            if stype == "none_fits":
                options = [(k, violating_value(pushed, k, rng)) for k in keys]
                options = [(k, v) for k, v in options if v is not None]
                if not options:
                    continue
                k, v = rng.choice(options)
                reqs[k] = v
        if len(reqs) < 3:
            continue
        sig = (pushed["id"], json.dumps(reqs, sort_keys=True))
        if sig in seen:
            continue
        s = {"id": None, "type": stype, "category": category,
             "customer_message": None, "requirements": reqs,
             "pushed_product_id": pushed["id"]}
        s["correct_product_ids"] = correct_ids(catalog, s)
        if validate(s, by_id):
            continue  # construction rule broken; try again
        seen.add(sig)
        fails = failed_requirements(pushed, reqs)
        s["pushed_disqualifier"] = ", ".join(fails) or None
        s["customer_message"] = customer_message(category, reqs, rng)
        s["message_source"] = "template"
        s["message_verified"] = True  # stated by construction; spot-check 10 by eye
        return s
    raise RuntimeError(f"Could not build a {stype} scenario in {category}")


def main() -> None:
    cfg = load_config()
    rng = random.Random(cfg["seed"])
    catalog = make_catalog(rng)
    by_id = {p["id"]: p for p in catalog}
    cats = list(CATEGORIES)
    seen: set = set()
    scenarios = [make_scenario(t, cats[i % len(cats)], catalog, by_id, seen, rng)
                 for t in TYPES for i in range(PER_TYPE)]
    rng.shuffle(scenarios)  # ids must not reveal type
    for i, s in enumerate(scenarios, 1):
        s["id"] = f"S-{i:03d}"

    paths = cfg["paths"]
    Path(paths["catalog"]).parent.mkdir(parents=True, exist_ok=True)
    Path(paths["catalog"]).write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    Path(paths["scenarios"]).write_text(json.dumps(scenarios, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    print(f"{len(catalog)} products, {len(scenarios)} scenarios")
    print("by type:", dict(Counter(s["type"] for s in scenarios)))
    print("by category:", dict(Counter(s["category"] for s in scenarios)))
    print("pushed-product reuse (max):",
          max(Counter(s["pushed_product_id"] for s in scenarios).values()))


if __name__ == "__main__":
    main()
