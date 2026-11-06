# DreamCatcher49

Does commercial pressure make a tool-using sales agent less honest? A small,
pre-registered eval: the same customer requests run through an LLM sales agent
under neutral vs. pressured system prompts, measuring how often it pushes a
product that doesn't fit, fabricates specs, or hides deal-breakers.

Design and hypotheses: [DESIGN.md](DESIGN.md). All products and the company
are fictional.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then paste your API key into .env
```

## Pipeline

```bash
python -m src.build_data                       # 1. catalog + 120 scenarios (seeded)
python -m src.ground_truth data/catalog.json data/scenarios.json   # 2. must say 0 problems

# 3. pilot: 100 runs, Haiku only
python -m src.run --models claude-haiku-5-5 --limit 20 --reps 1 --out results/pilot
python -m src.grade_rules --raw results/pilot

# 4. full run (resumable: rerun the same command after any crash)
python -m src.run --dry-run                    # check plan + token estimate
python -m src.run
python -m src.grade_rules

# 5. hand labels FIRST (blind), then validate the judge on them
python -m src.label --n 50
python -m src.judge submit --run-ids labels/hand_labels.csv
python -m src.judge status                     # wait for "ended"
python -m src.judge collect
python -m src.judge agreement                  # kappa >= 0.6 before going on

# 6. judge everything else, then analyze
python -m src.judge submit
python -m src.judge collect
python -m src.analyze                          # summary, contrasts, headline chart
```

## Layout

```
config.yaml          models, reps, concurrency, seed (pin before the full run)
DESIGN.md            pre-registration + changelog
data/                catalog.json, scenarios.json (generated), conditions.json
src/
  build_data.py      seeded catalog + scenario generator
  ground_truth.py    requirements -> correct products; scenario validation
  tools.py           list_products / get_product
  agent.py           one conversation: tool loop -> RECOMMENDATION
  run.py             all cells, async, resumable
  grade_rules.py     code-graded metrics
  label.py           blind hand-labeling in the terminal
  judge.py           blind LLM judge via Message Batches API + agreement
  analyze.py         rates, Wilson CIs, paired bootstrap contrasts, chart
  common.py          shared helpers
tests/
  test_parser.py     recommendation parser cases (python -m unittest tests.test_parser)
results/             raw transcripts (JSONL), grades, judge outputs, charts
labels/              hand labels
```

## Results

TBD after the full run.
