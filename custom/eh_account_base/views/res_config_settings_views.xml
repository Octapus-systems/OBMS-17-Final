<?xml version="1.0" encoding="UTF-8" ?>
<!--
    Settings page for the ERP Heritage suite. Adds a Heritage section
    under the standard Settings > Accounting page with grouped knobs
    for reporting, forecasting, assets, collections, AP, payments, and
    approval workflow.
-->
<odoo>

    <record id="res_config_settings_view_form_eh" model="ir.ui.view">
        <field name="name">res.config.settings.view.form.eh.heritage</field>
        <field name="model">res.config.settings</field>
        <field name="inherit_id" ref="account.res_config_settings_view_form"/>
        <field name="arch" type="xml">
            <xpath expr="//block[1]" position="after">

                <block title="ERP Heritage Reporting"
                       name="eh_block_reporting"
                       id="eh_block_reporting"
                       help="Heritage reporting engine and dashboard knobs.">
                    <setting id="eh_gl_row_limit_setting"
                             string="GL Row Limit"
                             help="Maximum journal-item rows row-driven reports will materialise.">
                        <field name="eh_gl_row_limit"/>
                    </setting>
                    <setting id="eh_dashboard_lookback_setting"
                             string="Dashboard Lookback (days)"
                             help="History window scanned by the financial dashboard tiles.">
                        <field name="eh_dashboard_lookback_days"/>
                    </setting>
                </block>

                <block title="ERP Heritage Forecasting"
                       name="eh_block_forecast"
                       id="eh_block_forecast"
                       help="Default horizons and history for new forecast scenarios.">
                    <setting id="eh_forecast_horizon_setting"
                             string="Default Forecast Horizon (months)">
                        <field name="eh_forecast_default_horizon"/>
                    </setting>
                    <setting id="eh_forecast_history_setting"
                             string="Default Forecast History (months)">
                        <field name="eh_forecast_default_history_months"/>
                    </setting>
                </block>

                <block title="ERP Heritage Assets and Leases"
                       name="eh_block_assets"
                       id="eh_block_assets"
                       help="Defaults for new fixed-asset and IFRS 16 lease contracts.">
                    <setting id="eh_asset_useful_life_setting"
                             string="Default Asset Useful Life (months)">
                        <field name="eh_asset_default_useful_life_months"/>
                    </setting>
                    <setting id="eh_lease_term_setting"
                             string="Default Lease Term (months)">
                        <field name="eh_lease_default_term_months"/>
                    </setting>
                </block>

                <block title="ERP Heritage Operations"
                       name="eh_block_operations"
                       id="eh_block_operations"
                       help="Operations defaults for collections, AP intake, and SEPA.">
                    <setting id="eh_collections_grace_setting"
                             string="Collections Grace Days"
                             help="Days past invoice due date before a collection case opens automatically.">
                        <field name="eh_collections_grace_days"/>
                    </setting>
                    <setting id="eh_approval_material_setting"
                             string="Approval Material Change (%)"
                             help="Percentage change to a request amount that triggers a re-approval.">
                        <field name="eh_approval_material_change_pct"/>
                    </setting>
                    <setting id="eh_sepa_dd_instrument_setting"
                             string="Default SEPA DD Local Instrument">
                        <field name="eh_sepa_dd_default_instrument"/>
                    </setting>
                </block>

                <block title="ERP Heritage AP Automation"
                       name="eh_block_ap"
                       id="eh_block_ap"
                       help="Default regular-expression patterns used by the bill-intake parser.">
                    <setting id="eh_ap_invoice_ref_setting"
                             string="Invoice Ref Regex"
                             help="Pattern used to extract the invoice number from raw email or text.">
                        <field name="eh_ap_invoice_ref_regex"/>
                    </setting>
                    <setting id="eh_ap_total_setting"
                             string="Total Amount Regex">
                        <field name="eh_ap_total_regex"/>
                    </setting>
                </block>

            </xpath>
        </field>
    </record>

</odoo>
