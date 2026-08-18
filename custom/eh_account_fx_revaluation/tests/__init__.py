# -*- encoding: utf-8 -*-
from . import test_fx_revaluation  # noqa: F401
from . import test_fx_report  # noqa: F401
from . import test_performance  # noqa: F401
from . import test_rate_providers  # noqa: F401
from . import test_hedge  # noqa: F401
from . import test_golden_ias21_cta  # noqa: F401
from . import test_workflow_guard  # noqa: F401
from . import test_post_lock  # noqa: F401
from . import test_line_freeze  # noqa: F401

# Per-source rate provider tests (hermetic, no network, no DB writes).
from . import test_fx_frankfurter  # noqa: F401
from . import test_fx_erapi  # noqa: F401
from . import test_fx_boc  # noqa: F401
from . import test_fx_nbp  # noqa: F401
from . import test_fx_cnb  # noqa: F401
from . import test_fx_cbr  # noqa: F401
from . import test_fx_bnr  # noqa: F401
from . import test_fx_bnb  # noqa: F401
from . import test_fx_srb  # noqa: F401
from . import test_fx_bcu  # noqa: F401
from . import test_fx_nbkz  # noqa: F401
from . import test_fx_cbu  # noqa: F401
from . import test_fx_tcmb  # noqa: F401
from . import test_fx_rba  # noqa: F401
from . import test_fx_cbk  # noqa: F401
from . import test_fx_cbb  # noqa: F401
from . import test_fx_bcb  # noqa: F401
from . import test_fx_bnm  # noqa: F401
from . import test_fx_banrepco  # noqa: F401
from . import test_fx_bcrp  # noqa: F401
from . import test_fx_hmrc  # noqa: F401
from . import test_fx_gcc_peg  # noqa: F401
from . import test_fx_fixer  # noqa: F401
from . import test_fx_oxr  # noqa: F401
from . import test_fx_currencylayer  # noqa: F401
from . import test_fx_banxico  # noqa: F401

# Browser tour (eh_tour tag, selected by the matrix runner with --tours).
from . import test_tour  # noqa: F401
