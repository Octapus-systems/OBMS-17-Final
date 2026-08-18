# Phase 1 test plan (mandatory bar):
#
# Functional unit tests:
from . import test_trial_balance  # noqa: F401
from . import test_profit_and_loss  # noqa: F401
from . import test_balance_sheet  # noqa: F401
from . import test_general_ledger  # noqa: F401
from . import test_partner_ledger  # noqa: F401
from . import test_aged_receivable  # noqa: F401
from . import test_aged_payable  # noqa: F401
from . import test_aged_unfold  # noqa: F401
from . import test_cash_flow  # noqa: F401
from . import test_golden_cash_flow_ias7      # IAS 7 indirect/disclosures golden + tie property  # noqa: F401
from . import test_annotations               # WS3 annotation server layer  # noqa: F401
from . import test_executive_summary  # noqa: F401
from . import test_deferred_schedule  # noqa: F401
from . import test_bank_reconciliation_proof  # noqa: F401
from . import test_analytic_balance  # noqa: F401
from . import test_xlsx_export                # end to end XLSX of Trial Balance  # noqa: F401
from . import test_pdf_export                 # PDF smoke tests for all reports  # noqa: F401
from . import test_hierarchical_groups        # account.group nesting in BS / P&L  # noqa: F401
from . import test_trial_balance_hierarchy    # account.group nesting in TB  # noqa: F401
from . import test_lazy_expand_engine         # Wave 0 lazy expand engine  # noqa: F401
from . import test_partner_ledger_lazy         # partner-ledger lazy per-aml unfold  # noqa: F401
from . import test_fold_consistency            # uniform fold-normalization invariant  # noqa: F401
#
# Performance scaffolding (perf tag; heavy variant env-gated):
from . import test_perf_render                 # orchestrator render path baseline  # noqa: F401
#
# Phase 1 will add:
#   from . import test_report_profit_loss
#   from . import test_report_balance_sheet
#   from . import test_report_general_ledger
#   from . import test_report_partner_ledger
#   from . import test_report_aged_receivable
#   from . import test_report_aged_payable
#   from . import test_report_cash_flow
#   from . import test_drilldown
#   from . import test_comparatives
#   from . import test_export_xlsx_golden
#   from . import test_export_pdf_golden
#
# Combination matrix tests (config flag x scenario):
#   from . import test_combo_multi_company
#   from . import test_combo_multi_currency
#   from . import test_combo_fiscal_positions
#   from . import test_combo_lock_dates
#   from . import test_combo_analytic_distribution
#
# Pressure / heavy performance tests (run nightly, gating on regression):
#   from . import test_perf_100k_entries
#   from . import test_perf_1m_entries
#   from . import test_perf_concurrent_users
#   from . import test_perf_cache_invalidation
#   from . import test_perf_snapshot_refresh
#
# Real time stress / chaos:
#   from . import test_stress_concurrent_post_and_render
#   from . import test_stress_partial_failure_recovery
from . import test_customer_statement  # noqa: F401
