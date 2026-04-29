# Strategy Discovery Framework

Phase-3 strategy generation pipeline + backtest engine. See `DESIGN.md` for the full design document, scope, and phase plan.

## Layout

- `src/` — engine, evaluation pipeline, generator (Claude tool-use spec → translated Python)
- `strategies/` — `manual/` (hand-coded baselines) + `generated/` (auto-generated, regenerable)
- `scripts/` — CLI entry points (`backtest.py`, `discover.py`, `evaluate.py`, etc.)
- `tests/` — `unit/`, `regression/`, `integration/`
- `data/` — `polygon/<SYM>/5m.parquet` (train+test) + `holdout/polygon/<SYM>/5m.parquet`
- `results/` — generation logs, fast-eval reports, quirks counter

Run the test suite with `./venv/bin/python -m pytest tests/`.

## Debugging tools

Scripts under `scripts/` whose names start with `diagnose_` are interactive debugging tools, not part of the production pipeline. They wrap or instrument production code paths to surface "why didn't this happen the way I expected?" questions.

- **`scripts/diagnose_disconnect.py`** — when a generated strategy produces fewer trades than the signal-frequency diagnostic suggests it should, this script wraps `on_bar` and runs the strategy through the real backtester (no walk-forward) so you can see what the engine actually did at each bar: how many bars had a position open (entries can't fire while open), how many `on_bar` calls emitted orders, how many orders became trades. Complements `evaluation.diagnostics.diagnose_signal_frequency` (which counts DSL clause hits).

  Usage:
  ```
  ./venv/bin/python scripts/diagnose_disconnect.py \
      --strategy ZscoreBbMeanReversion --symbol AMD
  ```
