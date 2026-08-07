"""Regression tests for e-receipt submission correctness and idempotency.

Covered contracts:

1. ``EReceiptSubmitter`` no longer creates the ETA Log after the ETA POST:
   the caller passes a pre-created "Started" intent log in. Accepted
   responses map the ETA fields onto the invoice exactly once and complete
   the intent; explicit ETA rejections/errors mark it Failed (retryable).

2. ``ereceipt_schema.submit_ereceipt`` creates the durable intent *before*
   the external POST: it locks the invoice row FOR UPDATE, re-reads
   accepted/Started evidence under the lock, inserts the ETA Log, and
   commits deliberately at the side-effect boundary. A concurrent request
   that waited on the lock sees the committed Started intent and is
   blocked. Ambiguous failures (timeout, post-accept local failure) leave
   the intent Started/blocked pending operator reconciliation — they are
   never turned back into retryable state.

All tests are offline: the HTTP layer, log creation, and every
``frappe.db`` side effect are monkeypatched — no network, ETA, or
database access happens.
"""

import frappe
import pytest
import requests

import erpnext_egypt_compliance.erpnext_eta.ereceipt_schema as ereceipt_schema
from erpnext_egypt_compliance.erpnext_eta.doctype.eta_log.eta_log import ETALog
from erpnext_egypt_compliance.erpnext_eta.ereceipt_submitter import EReceiptSubmitter


class FakeDoc:
    """Minimal stand-in for a Frappe document (permission-checkable)."""

    def __init__(self, doctype, name, fields=None):
        self.doctype = doctype
        self.name = name
        self._fields = dict(fields or {})
        self.permission_checks = []

    def get(self, key, default=None):
        if key in self._fields:
            return self._fields[key]
        return self.__dict__.get(key, default)

    def check_permission(self, permission_type):
        self.permission_checks.append(permission_type)


class FakeConnector:
    pos_profile = "STORE-1"
    ETA_BASE = "https://eta.example.invalid/api/v1"

    def get_access_token(self):
        return "token"


def _make_eta_log(
    from_doctype, reference_doctype, docnames, events, name="ETA-LOG-0001"
):
    """A real ETALog instance (bypassing Document.__init__) with a stubbed save."""
    log = ETALog.__new__(ETALog)
    log.__dict__["name"] = name
    log.__dict__["from_doctype"] = from_doctype
    log.__dict__["submission_status"] = "Started"
    log.__dict__["documents"] = [
        frappe._dict(
            {"reference_doctype": reference_doctype, "reference_document": docname}
        )
        for docname in docnames
    ]

    def _save():
        events.append(("log_save", log.submission_status))

    log.save = _save
    return log


def _patch_db(monkeypatch, events):
    """Record frappe.db.commit / set_value calls into ``events``."""
    monkeypatch.setattr(frappe.db, "commit", lambda: events.append(("commit",)))

    def _set_value(dt, dn, field, *args, **kwargs):
        events.append(("set_value", dt, dn, field))

    monkeypatch.setattr(frappe.db, "set_value", _set_value)


def _accepted_response(receipt_number, uuid="uuid-1", submission_id="SUB-1"):
    return frappe._dict(
        {
            "submissionId": submission_id,
            "status_code": 202,
            "acceptedDocuments": [
                {
                    "receiptNumber": receipt_number,
                    "uuid": uuid,
                    "longId": "LONG-1",
                    "hashKey": "HASH-1",
                }
            ],
            "rejectedDocuments": [],
        }
    )


def _run_submitter(
    monkeypatch, doctype, docname, eta_response=None, send_error=None, events=None
):
    """Drive EReceiptSubmitter.submit_ereceipt with a pre-created intent log.

    The caller-side boundary commit is represented by an
    ("intent_committed",) marker before the submitter runs.
    Returns (result, events, eta_log).
    """
    events = events if events is not None else []
    _patch_db(monkeypatch, events)
    monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(frappe, "get_traceback", lambda: "traceback")
    eta_log = _make_eta_log(doctype, doctype, [docname], events)

    if send_error is not None:

        def _raise(self, url, headers, data):
            raise send_error

        monkeypatch.setattr(EReceiptSubmitter, "_send_submit_request", _raise)
    else:
        monkeypatch.setattr(
            EReceiptSubmitter,
            "_send_submit_request",
            lambda self, url, headers, data: eta_response,
        )

    events.append(("intent_committed",))
    submitter = EReceiptSubmitter(FakeConnector())
    payload = {"receipts": [{"header": {"receiptNumber": docname}}]}
    result = submitter.submit_ereceipt(payload, doctype, eta_log)
    return result, events, eta_log


# ---------------------------------------------------------------------------
# Submitter: accepted responses map fields exactly once and complete the intent
# ---------------------------------------------------------------------------


def test_accepted_ereceipt_updates_sales_invoice_fields_exactly_once(monkeypatch):
    """Pre-fix this raised TypeError inside _handle_success_response, returned
    {"error": ...} and never committed, leaving the accepted receipt retryable."""
    response = _accepted_response("SINV-0001")
    result, events, eta_log = _run_submitter(
        monkeypatch, "Sales Invoice", "SINV-0001", response
    )

    # The response is returned as-is, not swallowed into an error dict.
    assert result.get("submissionId") == "SUB-1"
    assert "error" not in result

    expected_fields = {
        "eta_uuid": "uuid-1",
        "eta_hash_key": "HASH-1",
        "eta_long_key": "LONG-1",
        "eta_submission_id": "SUB-1",
        "eta_status": "Submitted",
    }
    # Documents are processed (exactly once), then the log is saved, then
    # the success transaction commits — all after the pre-POST intent commit.
    assert events == [
        ("intent_committed",),
        ("set_value", "Sales Invoice", "SINV-0001", expected_fields),
        ("log_save", "Completed"),
        ("commit",),
    ]

    # Intent log is completed with the accepted document's identifiers.
    assert eta_log.submission_id == "SUB-1"
    assert eta_log.submission_status == "Completed"
    child_row = eta_log.get("documents")[0]
    assert child_row.uuid == "uuid-1"
    assert child_row.long_id == "LONG-1"
    assert child_row.accepted is True


def test_accepted_ereceipt_updates_pos_invoice_custom_fields(monkeypatch):
    response = _accepted_response("POS-0001")
    result, events, eta_log = _run_submitter(
        monkeypatch, "POS Invoice", "POS-0001", response
    )

    assert result.get("submissionId") == "SUB-1"
    assert "error" not in result

    set_value_calls = [e for e in events if e[0] == "set_value"]
    assert set_value_calls == [
        (
            "set_value",
            "POS Invoice",
            "POS-0001",
            {
                "custom_eta_uuid": "uuid-1",
                "custom_eta_hash_key": "HASH-1",
                "custom_eta_long_id": "LONG-1",
                "custom_eta_submission_id": "SUB-1",
                "custom_eta_status": "Submitted",
            },
        )
    ]
    assert eta_log.submission_status == "Completed"
    assert events.count(("commit",)) == 1


def test_einvoice_internal_id_keying_still_works(monkeypatch):
    """The e-invoice response shape (internalId) must be unaffected."""
    events = []
    _patch_db(monkeypatch, events)
    eta_log = _make_eta_log("Sales Invoice", "Sales Invoice", ["SINV-0009"], events)
    eta_log.process_documents(
        frappe._dict(
            {
                "submissionId": "SUB-9",
                "acceptedDocuments": [
                    {
                        "internalId": "SINV-0009",
                        "uuid": "uuid-9",
                        "longId": "LONG-9",
                        "hashKey": "H9",
                    }
                ],
            }
        )
    )

    set_value_calls = [e for e in events if e[0] == "set_value"]
    assert set_value_calls == [
        (
            "set_value",
            "Sales Invoice",
            "SINV-0009",
            {
                "eta_uuid": "uuid-9",
                "eta_hash_key": "H9",
                "eta_long_key": "LONG-9",
                "eta_submission_id": "SUB-9",
                "eta_status": "Submitted",
            },
        )
    ]
    assert eta_log.get("documents")[0].accepted is True


# ---------------------------------------------------------------------------
# Submitter: explicit ETA rejection/error is retryable; ambiguous failure is not
# ---------------------------------------------------------------------------


def test_rejected_ereceipt_marks_child_row_and_stays_retryable(monkeypatch):
    """A rejected receipt records the error, completes the intent as
    non-Started, and leaves no acceptance evidence — so it is resubmittable."""
    response = frappe._dict(
        {
            "submissionId": "SUB-2",
            "status_code": 202,
            "acceptedDocuments": [],
            "rejectedDocuments": [
                {
                    "receiptNumber": "POS-0002",
                    "error": {
                        "message": "Invalid tax",
                        "target": "taxableItems",
                        "details": [],
                    },
                }
            ],
        }
    )
    result, events, eta_log = _run_submitter(
        monkeypatch, "POS Invoice", "POS-0002", response
    )

    assert "error" not in result
    assert eta_log.submission_status == "Partially Succeeded"
    child_row = eta_log.get("documents")[0]
    assert child_row.accepted is False
    assert "Invalid tax" in child_row.error
    assert not child_row.get("uuid")
    # The rejected document's ETA fields carry no uuid: no acceptance
    # evidence is left behind, and the intent is not "Started".
    set_value_calls = [e for e in events if e[0] == "set_value"]
    assert set_value_calls[0][3]["custom_eta_uuid"] is None
    assert events.count(("commit",)) == 1


def test_explicit_eta_error_marks_intent_failed_and_retryable(monkeypatch):
    """A definitive ETA error response marks the intent Failed: the guard
    only blocks on Started, so the document becomes resubmittable."""
    response = frappe._dict(
        {"status_code": 400, "error": {"message": "Invalid receipt", "details": []}}
    )
    result, events, eta_log = _run_submitter(
        monkeypatch, "POS Invoice", "POS-0003", response
    )

    assert (
        result["error"]["message"] == "Invalid receipt"
    )  # the ETA error payload is preserved
    assert eta_log.submission_status == "Failed"
    assert eta_log.eta_response
    assert events == [("intent_committed",), ("log_save", "Failed"), ("commit",)]


def test_timeout_leaves_intent_started_and_blocked(monkeypatch):
    """An ambiguous network timeout must NOT mark the intent Failed: it stays
    Started (as committed before the POST) and no success commit happens."""
    result, events, eta_log = _run_submitter(
        monkeypatch, "POS Invoice", "POS-0004", send_error=requests.Timeout("timed out")
    )

    assert result == {"error": "timed out"}
    # The intent remains exactly as committed pre-POST: Started, unresolved.
    assert eta_log.submission_status == "Started"
    assert [e for e in events if e[0] == "log_save"] == []
    assert ("commit",) not in events  # no post-POST commit on the ambiguous path


def test_pre_post_token_failure_marks_intent_failed_and_retryable(monkeypatch):
    """A get_access_token failure happens before the irreversible POST
    boundary: ETA was definitely never reached, so the intent is conclusively
    marked Failed (save + commit) and _send_submit_request is never called."""
    events = []
    _patch_db(monkeypatch, events)
    monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(frappe, "get_traceback", lambda: "traceback")
    eta_log = _make_eta_log("POS Invoice", "POS Invoice", ["POS-0010"], events)

    send_calls = []

    def _record_send(self, url, headers, data):
        send_calls.append((url, headers, data))

    monkeypatch.setattr(EReceiptSubmitter, "_send_submit_request", _record_send)

    class _FailingTokenConnector(FakeConnector):
        def get_access_token(self):
            raise RuntimeError("token endpoint unreachable")

    events.append(("intent_committed",))
    submitter = EReceiptSubmitter(_FailingTokenConnector())
    result = submitter.submit_ereceipt(
        {"receipts": [{"header": {"receiptNumber": "POS-0010"}}]},
        "POS Invoice",
        eta_log,
    )

    assert result == {"error": "token endpoint unreachable"}
    # The pre-POST failure is conclusive: Failed + save + commit.
    assert eta_log.submission_status == "Failed"
    assert eta_log.eta_response == "token endpoint unreachable"
    assert events == [("intent_committed",), ("log_save", "Failed"), ("commit",)]
    # The irreversible boundary was never crossed.
    assert send_calls == []


def test_pre_post_prepare_data_failure_marks_intent_failed_and_retryable(
    monkeypatch,
):
    """A _prepare_data failure happens before the irreversible POST
    boundary: ETA was definitely never reached, so the intent is conclusively
    marked Failed (save + commit) and _send_submit_request is never called."""
    events = []
    _patch_db(monkeypatch, events)
    monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(frappe, "get_traceback", lambda: "traceback")
    eta_log = _make_eta_log("POS Invoice", "POS Invoice", ["POS-0011"], events)

    send_calls = []

    def _record_send(self, url, headers, data):
        send_calls.append((url, headers, data))

    def _raise_prepare(self, ereceipts):
        raise TypeError("unserializable receipt payload")

    monkeypatch.setattr(EReceiptSubmitter, "_send_submit_request", _record_send)
    monkeypatch.setattr(EReceiptSubmitter, "_prepare_data", _raise_prepare)

    events.append(("intent_committed",))
    submitter = EReceiptSubmitter(FakeConnector())
    result = submitter.submit_ereceipt(
        {"receipts": [{"header": {"receiptNumber": "POS-0011"}}]},
        "POS Invoice",
        eta_log,
    )

    assert result == {"error": "unserializable receipt payload"}
    assert eta_log.submission_status == "Failed"
    assert eta_log.eta_response == "unserializable receipt payload"
    assert events == [("intent_committed",), ("log_save", "Failed"), ("commit",)]
    assert send_calls == []


def test_post_accept_local_failure_leaves_intent_started(monkeypatch):
    """ETA accepted, but local processing failed (e.g. a missing custom
    column): the success save/commit never happens, so the durable intent
    stays Started and blocked pending operator reconciliation — the failure
    is surfaced, never swallowed into retryable state."""
    events = []
    _patch_db(monkeypatch, events)
    monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(frappe, "get_traceback", lambda: "traceback")
    eta_log = _make_eta_log("POS Invoice", "POS Invoice", ["POS-0005"], events)

    def _failing_set_value(dt, dn, field, *args, **kwargs):
        raise RuntimeError("Unknown column 'custom_eta_uuid'")

    monkeypatch.setattr(frappe.db, "set_value", _failing_set_value)
    monkeypatch.setattr(
        EReceiptSubmitter,
        "_send_submit_request",
        lambda self, url, headers, data: _accepted_response("POS-0005"),
    )

    events.append(("intent_committed",))
    submitter = EReceiptSubmitter(FakeConnector())
    result = submitter.submit_ereceipt(
        {"receipts": [{"header": {"receiptNumber": "POS-0005"}}]},
        "POS Invoice",
        eta_log,
    )

    # The failure is surfaced to the caller...
    assert "error" in result
    # ...and nothing durable changed after the intent commit: no save, no
    # commit. The committed DB intent is still "Started": blocked.
    assert events == [("intent_committed",)]


# ---------------------------------------------------------------------------
# Schema: intent lifecycle at the external-side-effect boundary
# ---------------------------------------------------------------------------


class _Receipt:
    def __init__(self, receipt_number):
        self._receipt_number = receipt_number

    def model_dump(self):
        return {"receipts": [{"header": {"receiptNumber": self._receipt_number}}]}


def _patch_schema_seams(
    monkeypatch,
    docs,
    events,
    eta_uuid=None,
    accepted_rows=None,
    child_log_rows=None,
    started_logs=None,
):
    """Patch every DB/HTTP seam of ereceipt_schema.submit_ereceipt.

    ``events`` records the call order: document loads, row lock, evidence
    re-reads, intent creation, boundary commit, and the external POST.
    """

    def _get_doc(doctype, name):
        events.append(("get_doc", doctype, name))
        return docs[(doctype, name)]

    monkeypatch.setattr(frappe, "get_doc", _get_doc)
    # submit_ereceipt logs expected ValidationErrors via frappe.log_error;
    # the real logger would build an Error Log doc through the fake get_doc
    # above, so stub it out and let the exception rethrow cleanly.
    monkeypatch.setattr(frappe, "log_error", lambda *args, **kwargs: None)
    # The rethrow formats the message through the module-level translation
    # function; real translation may consult the DB (recording a stray
    # get_all), so replace it with a deterministic passthrough.
    monkeypatch.setattr(ereceipt_schema, "_", lambda msg, *args, **kwargs: msg)

    def _db_get_value(doctype, name, fieldname, for_update=False, **kwargs):
        events.append(("db_get_value", doctype, name, fieldname, for_update))
        if fieldname == "eta_uuid":
            return eta_uuid
        return name  # the FOR UPDATE row-lock probe returns the row's name

    monkeypatch.setattr(frappe.db, "get_value", _db_get_value)
    monkeypatch.setattr(frappe.db, "commit", lambda: events.append(("commit",)))

    def _get_all(doctype, filters=None, fields=None, limit=None, **kwargs):
        events.append(("get_all", doctype, filters))
        if doctype == "ETA Log Documents" and filters.get("accepted") == 1:
            return accepted_rows or []
        if doctype == "ETA Log Documents":
            return child_log_rows or []
        if doctype == "ETA Log":
            return started_logs or []
        return []

    monkeypatch.setattr(frappe, "get_all", _get_all)

    monkeypatch.setattr(
        ereceipt_schema,
        "_build_erceipt_json",
        lambda docname, doctype: _Receipt(docname),
    )

    intent_log = _make_eta_log("POS Invoice", "POS Invoice", ["POS-0001"], events)

    def _create_eta_log(**kwargs):
        events.append(("create_eta_log", kwargs.get("submission_status")))
        return intent_log

    monkeypatch.setattr(ereceipt_schema, "create_eta_log", _create_eta_log)

    class FakeSubmitter:
        def __init__(self, connector):
            events.append(("submitter_init",))

        def submit_ereceipt(self, payload, doctype, eta_log):
            events.append(("POST", doctype, eta_log is intent_log))

    monkeypatch.setattr(ereceipt_schema, "EReceiptSubmitter", FakeSubmitter)
    return intent_log


def _pos_invoice_docs():
    return {
        ("POS Invoice", "POS-0001"): FakeDoc(
            "POS Invoice", "POS-0001", fields={"pos_profile": "STORE-1"}
        ),
        ("ETA POS Connector", "STORE-1"): FakeDoc("ETA POS Connector", "STORE-1"),
    }


def test_intent_created_and_committed_before_post(monkeypatch):
    events = []
    _patch_schema_seams(monkeypatch, _pos_invoice_docs(), events)

    ereceipt_schema.submit_ereceipt("POS-0001", "STORE-1", "POS Invoice")

    # Permission-checked invoice and connector loads, then the row lock,
    # evidence re-reads under the lock, Started intent inserted and
    # committed — all strictly before the external POST.
    kinds = [e[0] for e in events]
    assert kinds == [
        "get_doc",  # permission-checked invoice
        "get_doc",  # connector bound to the invoice's POS profile
        "db_get_value",  # FOR UPDATE row lock on the invoice
        "get_all",  # accepted-evidence re-read (ETA Log Documents, accepted=1)
        "get_all",  # child rows for the Started-intent re-read
        "create_eta_log",  # intent inserted with status Started
        "commit",  # deliberate boundary commit
        "submitter_init",
        "POST",  # external side effect happens last
    ]
    assert ("db_get_value", "POS Invoice", "POS-0001", "name", True) in events
    assert ("create_eta_log", "Started") in events
    # The pre-created intent log is the object handed to the submitter.
    assert events[-1] == ("POST", "POS Invoice", True)


def test_serialized_second_request_blocked_by_started_intent(monkeypatch):
    """The loser of the row-lock race re-reads evidence under the lock and
    sees the winner's committed Started intent: no second intent, no POST."""
    events = []
    _patch_schema_seams(
        monkeypatch,
        _pos_invoice_docs(),
        events,
        child_log_rows=[frappe._dict({"parent": "ETA-LOG-0001"})],
        started_logs=[frappe._dict({"name": "ETA-LOG-0001"})],
    )

    with pytest.raises(frappe.ValidationError):
        ereceipt_schema.submit_ereceipt("POS-0001", "STORE-1", "POS Invoice")

    kinds = [e[0] for e in events]
    assert kinds == [
        "get_doc",
        "get_doc",
        "db_get_value",  # waited on the winner's lock
        "get_all",  # accepted re-read: none
        "get_all",  # child rows: found the winner's row
        "get_all",  # ETA Log: still Started -> blocked
    ]
    assert "create_eta_log" not in kinds
    assert "POST" not in kinds
    assert "commit" not in kinds


def test_duplicate_refused_for_sales_invoice_with_eta_uuid(monkeypatch):
    events = []
    docs = {
        ("Sales Invoice", "SINV-0001"): FakeDoc(
            "Sales Invoice", "SINV-0001", fields={"pos_profile": "STORE-1"}
        ),
        ("ETA POS Connector", "STORE-1"): FakeDoc("ETA POS Connector", "STORE-1"),
    }
    _patch_schema_seams(monkeypatch, docs, events, eta_uuid="uuid-1")

    with pytest.raises(frappe.ValidationError):
        ereceipt_schema.submit_ereceipt("SINV-0001", "STORE-1", "Sales Invoice")

    # The fixture-defined eta_uuid (re-read under the lock) is sufficient
    # evidence: no log queries, no intent, no POST.
    kinds = [e[0] for e in events]
    assert kinds == ["get_doc", "get_doc", "db_get_value", "db_get_value"]
    assert events[2] == ("db_get_value", "Sales Invoice", "SINV-0001", "name", True)
    assert events[3] == (
        "db_get_value",
        "Sales Invoice",
        "SINV-0001",
        "eta_uuid",
        False,
    )


def test_duplicate_refused_when_accepted_log_row_exists(monkeypatch):
    """POS Invoice has no fixture-defined ETA uuid field; the accepted ETA
    Log Documents row is the reliable cross-doctype evidence."""
    events = []
    _patch_schema_seams(
        monkeypatch,
        _pos_invoice_docs(),
        events,
        accepted_rows=[frappe._dict({"uuid": "uuid-1"})],
    )

    with pytest.raises(frappe.ValidationError):
        ereceipt_schema.submit_ereceipt("POS-0001", "STORE-1", "POS Invoice")

    kinds = [e[0] for e in events]
    assert "create_eta_log" not in kinds
    assert "POST" not in kinds
    accepted_query = [e for e in events if e[0] == "get_all"][0]
    assert accepted_query[2] == {
        "reference_doctype": "POS Invoice",
        "reference_document": "POS-0001",
        "accepted": 1,
    }


def test_failed_intent_is_retryable(monkeypatch):
    """A previous attempt conclusively rejected/errored by ETA (intent not
    Started, no accepted evidence) does not block: a new intent is created
    and committed, then the POST proceeds."""
    events = []
    _patch_schema_seams(
        monkeypatch,
        _pos_invoice_docs(),
        events,
        child_log_rows=[frappe._dict({"parent": "ETA-LOG-0000"})],
        started_logs=[],  # the old intent is Failed, not Started
    )

    ereceipt_schema.submit_ereceipt("POS-0001", "STORE-1", "POS Invoice")

    kinds = [e[0] for e in events]
    assert ("create_eta_log", "Started") in events
    assert "POST" in kinds
    assert kinds.index("commit") < kinds.index("POST")
