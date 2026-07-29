# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Stamp legacy maturity band rows as extracted.

Before 1.1.0 the band list was read-only and every eh.fin.maturity.line row
was built by action_populate, so on upgrade they are all extracted rows: the
new 'manual' column default is only right for rows keyed after this upgrade.
Their class follows the old populate precedence: instrument rows when the
run listed instruments, otherwise the selected-accounts liability class.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE eh_fin_maturity_line l
           SET origin = 'extracted',
               item_class = CASE WHEN EXISTS (
                       SELECT 1 FROM eh_fin_maturity_instrument i
                        WHERE i.run_id = l.run_id)
                   THEN 'instrument' ELSE 'liability' END
    """)
