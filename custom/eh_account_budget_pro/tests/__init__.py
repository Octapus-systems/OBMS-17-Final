# Phase 1 test plan:
#
# Functional unit tests:
from . import test_budget  # noqa: F401
from . import test_commitment  # noqa: F401
from . import test_forecast_unit  # noqa: F401
from . import test_budget_b4  # noqa: F401
from . import test_budget_report  # noqa: F401
#
# IFRS 10/10 program (Phase 7, CMA layer): flexible budgets, variance
# decomposition, rolling reforecast.
from . import test_golden_flexible_budget  # noqa: F401
from . import test_property_flex_budget  # noqa: F401
#
# Security: workflow state-write guard (RPC-bypass regression).
from . import test_workflow_guard  # noqa: F401
#
# Concurrency: 'block' overrun policy must serialise concurrent PO
# confirms on the shared budget-line availability (SELECT ... FOR UPDATE)
# so two confirms cannot both pass the gate and over-encumber.
from . import test_block_lock  # noqa: F401
#
# Security: multi-company record-rule isolation on eh.budget.commitment.
from . import test_company_isolation  # noqa: F401
#
# Security: multi-company record-rule isolation on the eh.budget.report
# SQL view (Budget vs Actual graph/pivot).
from . import test_report_company_isolation  # noqa: F401
#
# Combination tests (planned):
#   from . import test_combo_multi_company_budgets
#   from . import test_combo_versioning_chains
#
# Pressure tests (planned):
#   from . import test_perf_batch_actuals_500_lines
