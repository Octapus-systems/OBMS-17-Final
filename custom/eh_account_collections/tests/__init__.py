# Phase 1 test plan:
#
# Functional unit tests:
from . import test_collections_case  # noqa: F401
from . import test_collections_action  # noqa: F401
from . import test_followup_engine  # noqa: F401
from . import test_broken_promise  # noqa: F401
from . import test_collections_b5  # noqa: F401
from . import test_late_fee_wizard  # noqa: F401
from . import test_case_statement_report  # noqa: F401
from . import test_partner_collections_count  # noqa: F401
#
# Combination tests (planned):
#   from . import test_combo_multi_company
#
# Pressure tests (planned):
#   from . import test_perf_auto_create_500_partners
