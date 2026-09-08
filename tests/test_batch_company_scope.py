"""Offline batch-selection tests: filter in the query before applying its limit."""

from types import SimpleNamespace

import frappe
import pytest

import erpnext_egypt_compliance.erpnext_eta.main as eta_main


def _batch_seams(monkeypatch, *, manual=False, batch_size=2):
    today = "2026-09-08"
    connectors = {
        company: SimpleNamespace(
            company=company,
            submission_mode="Manual" if manual else "Automatic",
            eta_batch_size=batch_size,
            delay_in_hours=0,
        )
        for company in ("ACME", "OTHER")
    }
    rows = [
        {
            "name": name,
            "company": company,
            "eta_signature": "signed",
            "docstatus": 1,
            "eta_status": "",
            "eta_submission_id": "",
            "posting_date": today,
        }
        for name, company in [
            ("OTHER-1", "OTHER"),
            ("OTHER-2", "OTHER"),
            ("ACME-1", "ACME"),
            ("ACME-2", "ACME"),
            ("ACME-3", "ACME"),
        ]
    ]
    queries, builds, submissions, lookups = [], [], [], []

    def get_connector(company):
        lookups.append(company)
        return connectors[company]

    def get_all(doctype, *, filters, pluck, limit):
        assert doctype == "Sales Invoice"
        assert pluck == "name"
        queries.append((filters, limit))
        selected = rows
        for field, operator, value in filters:
            assert operator in ("=", "!=")
            selected = [row for row in selected if (row[field] == value) == (operator == "=")]
        return [row[pluck] for row in selected[:limit]]

    def build(name, *, as_dict):
        assert as_dict is True
        builds.append(name)
        row = next(row for row in rows if row["name"] == name)
        return {"name": row["name"], "company": row["company"]}

    def submit(invoices, connector, *, submitted_by):
        submissions.append((invoices, connector, submitted_by))

    monkeypatch.setattr(eta_main, "nowdate", lambda: today)
    monkeypatch.setattr(eta_main, "get_company_eta_connector", get_connector)
    monkeypatch.setattr(frappe, "get_all", get_all)
    monkeypatch.setattr(eta_main, "get_eta_inv_datetime_diff", lambda name: 24)
    monkeypatch.setattr(eta_main, "get_invoice_asjson", build)
    monkeypatch.setattr(eta_main, "submit_einvoice_background_logger", submit)
    monkeypatch.setattr(frappe, "logger", lambda: SimpleNamespace(error=lambda message: None))

    def unexpected_error(*args, **kwargs):
        pytest.fail("Batch selection raised an unexpected error")

    monkeypatch.setattr(frappe, "log_error", unexpected_error)
    return connectors, queries, builds, submissions, lookups


def test_each_company_fills_its_batch_before_limit_and_uses_its_connector(monkeypatch):
    connectors, queries, builds, submissions, lookups = _batch_seams(monkeypatch)

    eta_main.get_batch_invoices("ACME")
    eta_main.get_batch_invoices("OTHER")

    assert lookups == ["ACME", "OTHER"]
    assert builds == ["ACME-1", "ACME-2", "OTHER-1", "OTHER-2"]
    for index, company in enumerate(("ACME", "OTHER")):
        filters, limit = queries[index]
        assert ["company", "=", company] in filters
        assert limit == 2
        assert ["posting_date", "=", "2026-09-08"] in filters
        invoices, connector, submitted_by = submissions[index]
        assert [invoice["name"] for invoice in invoices] == [company + "-1", company + "-2"]
        assert all(invoice["company"] == company for invoice in invoices)
        assert connector is connectors[company]
        assert submitted_by == "Agent"


def test_batch_without_matching_company_has_no_submission(monkeypatch):
    connectors, queries, builds, submissions, _ = _batch_seams(monkeypatch)
    connectors["EMPTY"] = SimpleNamespace(submission_mode="Automatic", eta_batch_size=2, delay_in_hours=0)

    eta_main.get_batch_invoices("EMPTY")

    assert queries[0][1] == 2
    assert builds == []
    assert submissions == []


def test_manual_connector_still_skips_query_and_submission(monkeypatch):
    _, queries, builds, submissions, lookups = _batch_seams(monkeypatch, manual=True)

    eta_main.get_batch_invoices("ACME")

    assert lookups == ["ACME"]
    assert queries == builds == submissions == []
