# Phase 1 test plan:
#
# Functional unit tests:
from . import test_suggestion_engine     # five heuristics, combined score, find_suggestions  # noqa: F401
from . import test_reconciliation_session # lifecycle, counters, audit creation  # noqa: F401,E261
from . import test_auto_reconcile          # batch auto-reconcile gates  # noqa: F401
from . import test_predictor               # counterpart prediction from history  # noqa: F401
from . import test_writeoff_amount_types    # CE write-off amount types  # noqa: F401
from . import test_learned_rules            # learned match rules from history  # noqa: F401
from . import test_write_off                 # write-off + FX write-off clear lines  # noqa: F401
from . import test_posted_move_inalterability # posted move stays immutable; adjusting entry carries reclass  # noqa: F401,E261,E501
from . import test_suspense_config_immutable  # posting never flips suspense account reconcile config  # noqa: F401
from . import test_exception_report  # exception PDF renders (report-values wiring)  # noqa: F401
from . import test_workflow_guard  # session state machine not RPC-skippable  # noqa: F401
from . import test_direction_guard  # wrong-side match refused (no cash misclassification)  # noqa: F401
#
# Combination tests (planned):
#   from . import test_combo_multi_currency_match
#   from . import test_combo_partial_payment
#   from . import test_combo_writeoff_with_analytic
#   from . import test_combo_with_other_account_modules  # coexistence
#
# Pressure tests (planned):
#   from . import test_perf_10k_unreconciled
#   from . import test_perf_suggestion_engine_throughput
#   from . import test_perf_bulk_match_500_lines
