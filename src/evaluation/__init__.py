"""Evaluation harness — splits, walk-forward, significance tests, scoring."""

from .symbols import (
    SP500_SUBSET,
    CRYPTO_TOP_15,
    sp500_random_subset,
    sp500_with_required,
    top_crypto,
    save_symbol_list,
    load_symbol_list,
)
from .splits import (
    HOLDOUT_BOUNDARY,
    HoldoutAccessError,
    holdout_load,
    is_in_optimization_mode,
    optimization_mode,
    slice_window,
    train_test_load,
)
from .significance import (
    BaselineResult,
    BootstrapResult,
    bootstrap_profit_factor,
    profit_factor,
    random_baseline,
    trade_count_warning,
)
from .walkforward import (
    WalkForwardConfig,
    WalkForwardResult,
    WindowResult,
    walk_forward,
)
from .scoring import (
    FailedCondition,
    PromiseVerdict,
    ScoreBreakdown,
    classify_promise,
    compute_robustness_score,
)
from .pipeline import (
    EvaluationResult,
    SymbolEvaluation,
    run_evaluation,
)
from .fast_pipeline import (
    FAST_LABEL,
    FAST_SYMBOLS,
    FastEvaluationResult,
    run_fast_evaluation,
)

__all__ = [
    "SP500_SUBSET",
    "CRYPTO_TOP_15",
    "sp500_random_subset",
    "sp500_with_required",
    "top_crypto",
    "save_symbol_list",
    "load_symbol_list",
    "HOLDOUT_BOUNDARY",
    "HoldoutAccessError",
    "holdout_load",
    "is_in_optimization_mode",
    "optimization_mode",
    "slice_window",
    "train_test_load",
]
