# Phase 1 test plan:
#
# Functional unit tests:
from . import test_sql_builder_unit          # builder produces correct SQL/params  # noqa: F401
from . import test_report_execution          # audit model lifecycle and hashing  # noqa: F401
from . import test_payload_codec             # zlib JSON codec round trip  # noqa: F401
from . import test_xlsx_writer               # XLSX writer shape and formats  # noqa: F401
#
# Integration tests (require an installed account module and demo data):
from . import test_cache_invalidation        # account.move state changes bump version  # noqa: F401
from . import test_posted_line_edit_invalidation  # posted line edits bump version  # noqa: F401
from . import test_sql_builder_integration   # builder executes against real data  # noqa: F401
from . import test_dynamic_report            # orchestrator render path and cache behaviour  # noqa: F401
from . import test_report_wizard             # wizard build_options and export action  # noqa: F401
from . import test_account_move_report       # branded Journal Entry PDF render regression  # noqa: F401
#
# Performance / pressure scaffolding (post_install tag, gated on perf threshold):
from . import test_perf_sql_builder  # noqa: F401
from . import test_report_fold_state  # noqa: F401
from . import test_move_seal  # noqa: F401
from . import test_net_guard  # noqa: F401
