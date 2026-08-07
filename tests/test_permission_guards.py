"""Fail-closed permission and object-binding regression tests for the
whitelisted ETA/eReceipt endpoints.

All tests are offline: ``frappe.get_doc`` and every downstream seam
(connector lookup, submitter classes, loggers, response mutation) are
monkeypatched, so no network, ETA, or database access happens.
"""

import frappe
import pytest

import erpnext_egypt_compliance.erpnext_eta.ereceipt_schema as ereceipt_schema
import erpnext_egypt_compliance.erpnext_eta.main as eta_main
from erpnext_egypt_compliance.erpnext_eta.permission_guards import ERECEIPT_DOCTYPES


class CallRecorder:
    """Callable stub that records its invocations."""

    def __init__(self, return_value=None):
        self.calls = []
        self.return_value = return_value

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.return_value


class FakeDoc:
    """Minimal stand-in for a Frappe document.

    Records ``check_permission`` calls. ``deny=True`` denies every
    permission; an iterable of permission types denies only those, so a
    document that permits ``submit`` but denies ``read`` can be modeled.
    """

    def __init__(self, doctype, name, fields=None, deny=False):
        self.doctype = doctype
        self.name = name
        self._fields = dict(fields or {})
        self._deny = deny
        self.permission_checks = []
        self.saves = 0

    def __getattr__(self, item):
        try:
            return self.__dict__["_fields"][item]
        except KeyError:
            raise AttributeError(item) from None

    def get(self, key, default=None):
        if key in self._fields:
            return self._fields[key]
        return self.__dict__.get(key, default)

    def check_permission(self, permission_type):
        self.permission_checks.append(permission_type)
        denied = self._deny is True or (self._deny and permission_type in self._deny)
        if denied:
            raise frappe.PermissionError(
                "No {0} permission on {1} {2}".format(permission_type, self.doctype, self.name)
            )

    def save(self):
        self.saves += 1


def _patch_get_doc(monkeypatch, docs):
    """Patch frappe.get_doc to serve ``docs`` keyed by (doctype, name)."""
    calls = []

    def _get_doc(doctype, name):
        calls.append((doctype, name))
        return docs[(doctype, name)]

    monkeypatch.setattr(frappe, "get_doc", _get_doc)
    return calls


def _patch_submit_ereceipt_seams(monkeypatch, receipt=None):
    """Patch the private builder, the submitter, and the intent/lock seams;
    return (build, submit_calls, intent_log)."""
    build = CallRecorder(return_value=receipt)
    monkeypatch.setattr(ereceipt_schema, "_build_erceipt_json", build)

    submit_calls = []
    intent_log = object()

    class FakeSubmitter:
        def __init__(self, conn):
            submit_calls.append(("init", conn))

        def submit_ereceipt(self, payload, doctype, eta_log):
            submit_calls.append(("submit", payload, doctype, eta_log))

    monkeypatch.setattr(ereceipt_schema, "EReceiptSubmitter", FakeSubmitter)

    # Offline seams for the pre-POST submission-intent lifecycle: the FOR
    # UPDATE row lock, the evidence re-reads, the intent insert, and the
    # deliberate boundary commit.
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: None)
    monkeypatch.setattr(frappe, "get_all", CallRecorder(return_value=[]))
    monkeypatch.setattr(frappe.db, "commit", lambda: None)
    monkeypatch.setattr(ereceipt_schema, "create_eta_log", lambda **kwargs: intent_log)
    return build, submit_calls, intent_log


class FakeReceipt:
    def model_dump(self):
        return {"receipts": []}


# ---------------------------------------------------------------------------
# main.py: denied permissions fail before any side effect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint_name, kwargs, expected_ptype",
    [
        ("download_eta_inv_json", {}, "read"),
        ("get_eta_pdf", {}, "read"),
        ("check_existing_eta_logs", {}, "read"),
        ("fetch_eta_status", {}, "write"),
        ("submit_eta_invoice", {}, "submit"),
        ("cancel_eta_invoice", {"reason": "duplicate submission"}, "cancel"),
    ],
)
def test_main_endpoints_deny_before_side_effects(monkeypatch, endpoint_name, kwargs, expected_ptype):
    doc = FakeDoc("Sales Invoice", "SINV-0001", fields={"company": "ACME"}, deny=True)
    get_doc_calls = _patch_get_doc(monkeypatch, {("Sales Invoice", "SINV-0001"): doc})

    seams = {}
    for name in (
        "get_invoice_asjson",
        "download_eta_invoice_json",
        "get_company_eta_connector",
        "update_eta_docstatus",
        "submit_einvoice_feedback_logger",
        "EInvoiceSubmitter",
    ):
        seams[name] = CallRecorder()
        monkeypatch.setattr(eta_main, name, seams[name])
    get_all = CallRecorder(return_value=[])
    monkeypatch.setattr(frappe, "get_all", get_all)
    get_value = CallRecorder()
    monkeypatch.setattr(frappe, "get_value", get_value)

    with pytest.raises(frappe.PermissionError):
        getattr(eta_main, endpoint_name)("SINV-0001", **kwargs)

    # Exact fixed doctype/docname and intended permission type were checked...
    assert get_doc_calls == [("Sales Invoice", "SINV-0001")]
    assert doc.permission_checks == [expected_ptype]
    # ...and no connector/HTTP/log/DB/response side effect happened.
    for recorder in seams.values():
        assert recorder.calls == []
    assert get_all.calls == []
    assert get_value.calls == []


def test_cancel_eta_invoice_permitted_reaches_connector(monkeypatch):
    """An allowed, bound cancel call still reaches the mocked downstream seam."""
    doc = FakeDoc(
        "Sales Invoice",
        "SINV-0001",
        fields={"company": "ACME", "eta_uuid": "uuid-1"},
    )
    _patch_get_doc(monkeypatch, {("Sales Invoice", "SINV-0001"): doc})

    connector = object()
    get_connector = CallRecorder(return_value=connector)
    monkeypatch.setattr(eta_main, "get_company_eta_connector", get_connector)

    cancel_calls = []

    class FakeSubmitter:
        def __init__(self, conn):
            assert conn is connector

        def cancel_document(self, uuid, reason):
            cancel_calls.append((uuid, reason))
            return {"status_code": 200}

    monkeypatch.setattr(eta_main, "EInvoiceSubmitter", FakeSubmitter)

    result = eta_main.cancel_eta_invoice("SINV-0001", "issued twice")

    assert doc.permission_checks == ["cancel"]
    assert get_connector.calls == [(("ACME",), {})]
    assert cancel_calls == [("uuid-1", "issued twice")]
    assert doc.eta_status == "Cancelled"
    assert doc.eta_cancellation_reason == "issued twice"
    assert doc.saves == 1
    assert result == {"status": "success"}


# ---------------------------------------------------------------------------
# ereceipt_schema.py: doctype allow-list before any lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doctype", ["Customer", "Company", "ETA POS Connector", "User"])
def test_ereceipt_rejects_arbitrary_doctype_before_lookup(monkeypatch, doctype):
    get_doc = CallRecorder()
    monkeypatch.setattr(frappe, "get_doc", get_doc)

    with pytest.raises(frappe.ValidationError):
        ereceipt_schema.build_erceipt_json("ANY-0001", doctype)
    with pytest.raises(frappe.ValidationError):
        ereceipt_schema.submit_ereceipt("ANY-0001", "PROFILE-1", doctype)

    assert get_doc.calls == []


# ---------------------------------------------------------------------------
# ereceipt_schema.py: read permission on the exact document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doctype", list(ERECEIPT_DOCTYPES))
def test_build_ereceipt_json_requires_read_on_exact_doc(monkeypatch, doctype):
    doc = FakeDoc(doctype, "INV-0001", deny=True)
    get_doc_calls = _patch_get_doc(monkeypatch, {(doctype, "INV-0001"): doc})
    set_globals = CallRecorder()
    monkeypatch.setattr(ereceipt_schema, "set_global_raw_data", set_globals)

    with pytest.raises(frappe.PermissionError):
        ereceipt_schema.build_erceipt_json("INV-0001", doctype)

    assert get_doc_calls == [(doctype, "INV-0001")]
    assert doc.permission_checks == ["read"]
    # Denial happens before any global raw data / company data load.
    assert set_globals.calls == []


def test_fetch_ereceipt_status_requires_read_on_exact_pos_invoice(monkeypatch):
    doc = FakeDoc("POS Invoice", "POS-0001", fields={"pos_profile": "STORE-1"}, deny=True)
    get_doc_calls = _patch_get_doc(monkeypatch, {("POS Invoice", "POS-0001"): doc})

    with pytest.raises(frappe.PermissionError):
        ereceipt_schema.fetch_ereceipt_status("POS-0001")

    # Only the exact POS Invoice was loaded; no connector lookup happened.
    assert get_doc_calls == [("POS Invoice", "POS-0001")]
    assert doc.permission_checks == ["read"]


@pytest.mark.parametrize("raise_throw", [True, False])
@pytest.mark.parametrize("stored_profile", [None, ""])
def test_fetch_ereceipt_status_rejects_missing_profile(monkeypatch, raise_throw, stored_profile):
    """A document-stored profile that is missing/empty fails closed before
    any connector lookup, for both raise_throw values."""
    doc = FakeDoc("POS Invoice", "POS-0001", fields={"pos_profile": stored_profile})
    get_doc_calls = _patch_get_doc(monkeypatch, {("POS Invoice", "POS-0001"): doc})

    with pytest.raises(frappe.ValidationError):
        ereceipt_schema.fetch_ereceipt_status("POS-0001", raise_throw=raise_throw)

    assert doc.permission_checks == ["read"]
    # No connector lookup: only the permission-checked document was loaded.
    assert get_doc_calls == [("POS Invoice", "POS-0001")]


def test_fetch_ereceipt_status_permitted_uses_profile_from_doc(monkeypatch):
    doc = FakeDoc("POS Invoice", "POS-0001", fields={"pos_profile": "STORE-1"})
    receipt_calls = []

    class FakeConnector:
        def get_receipt_submission(self, docname):
            receipt_calls.append(docname)
            return "ok"

    docs = {
        ("POS Invoice", "POS-0001"): doc,
        ("ETA POS Connector", "STORE-1"): FakeConnector(),
    }
    get_doc_calls = _patch_get_doc(monkeypatch, docs)
    monkeypatch.setattr(frappe, "msgprint", lambda *args, **kwargs: None)

    ereceipt_schema.fetch_ereceipt_status("POS-0001")

    assert doc.permission_checks == ["read"]
    assert get_doc_calls == [("POS Invoice", "POS-0001"), ("ETA POS Connector", "STORE-1")]
    assert receipt_calls == ["POS-0001"]


# ---------------------------------------------------------------------------
# ereceipt_schema.py: submit permission and POS profile binding
# ---------------------------------------------------------------------------


def test_submit_ereceipt_denied_submit_permission_before_connector(monkeypatch):
    doc = FakeDoc("POS Invoice", "POS-0001", fields={"pos_profile": "STORE-1"}, deny=True)
    get_doc_calls = _patch_get_doc(monkeypatch, {("POS Invoice", "POS-0001"): doc})
    build, submit_calls, _ = _patch_submit_ereceipt_seams(monkeypatch)

    with pytest.raises(frappe.PermissionError):
        ereceipt_schema.submit_ereceipt("POS-0001", "STORE-1", "POS Invoice")

    assert get_doc_calls == [("POS Invoice", "POS-0001")]
    assert doc.permission_checks == ["submit"]
    assert build.calls == []
    assert submit_calls == []


@pytest.mark.parametrize("supplied_profile", [None, "", "OTHER-PROFILE"])
def test_submit_ereceipt_rejects_missing_or_mismatched_profile(monkeypatch, supplied_profile):
    doc = FakeDoc("POS Invoice", "POS-0001", fields={"pos_profile": "STORE-1"})
    get_doc_calls = _patch_get_doc(monkeypatch, {("POS Invoice", "POS-0001"): doc})
    build, submit_calls, _ = _patch_submit_ereceipt_seams(monkeypatch)

    with pytest.raises(frappe.ValidationError):
        ereceipt_schema.submit_ereceipt("POS-0001", supplied_profile, "POS Invoice")

    assert doc.permission_checks == ["submit"]
    # The supplied profile was never used as a connector selector: the only
    # lookup is the permission-checked document itself.
    assert get_doc_calls == [("POS Invoice", "POS-0001")]
    assert build.calls == []
    assert submit_calls == []


def test_submit_ereceipt_permitted_and_bound_reaches_submitter(monkeypatch):
    """An allowed call with a correctly bound profile reaches the mocked seam."""
    doc = FakeDoc("POS Invoice", "POS-0001", fields={"pos_profile": "STORE-1"})
    connector = FakeDoc("ETA POS Connector", "STORE-1")
    docs = {
        ("POS Invoice", "POS-0001"): doc,
        ("ETA POS Connector", "STORE-1"): connector,
    }
    get_doc_calls = _patch_get_doc(monkeypatch, docs)
    build, submit_calls, intent_log = _patch_submit_ereceipt_seams(monkeypatch, receipt=FakeReceipt())

    ereceipt_schema.submit_ereceipt("POS-0001", "STORE-1", "POS Invoice")

    assert doc.permission_checks == ["submit"]
    # The connector was bound to the profile stored on the permitted document.
    assert get_doc_calls == [("POS Invoice", "POS-0001"), ("ETA POS Connector", "STORE-1")]
    assert build.calls == [(("POS-0001", "POS Invoice"), {})]
    assert submit_calls == [
        ("init", connector),
        ("submit", {"receipts": []}, "POS Invoice", intent_log),
    ]


def test_submit_ereceipt_submit_authorized_without_read(monkeypatch):
    """A document that permits submit but would deny read must still reach
    the bound connector: the submit path calls the private builder directly
    and is not subjected to an extra read gate."""
    doc = FakeDoc("POS Invoice", "POS-0001", fields={"pos_profile": "STORE-1"}, deny={"read"})
    connector = FakeDoc("ETA POS Connector", "STORE-1")
    docs = {
        ("POS Invoice", "POS-0001"): doc,
        ("ETA POS Connector", "STORE-1"): connector,
    }
    get_doc_calls = _patch_get_doc(monkeypatch, docs)
    build, submit_calls, intent_log = _patch_submit_ereceipt_seams(monkeypatch, receipt=FakeReceipt())

    ereceipt_schema.submit_ereceipt("POS-0001", "STORE-1", "POS Invoice")

    # Only the submit permission was requested on the document — no read
    # recheck leaked into the submit-authorized path.
    assert doc.permission_checks == ["submit"]
    assert "read" not in doc.permission_checks
    assert get_doc_calls == [("POS Invoice", "POS-0001"), ("ETA POS Connector", "STORE-1")]
    assert build.calls == [(("POS-0001", "POS Invoice"), {})]
    assert submit_calls == [
        ("init", connector),
        ("submit", {"receipts": []}, "POS Invoice", intent_log),
    ]
