# Copyright (c) 2026, bakr-systems and Contributors
# See license.txt
"""Regression tests for the UAT incident "Missing value for Company:
ETA Default Activity Code" (frappe.MandatoryError during Setup Wizard).

Contract under test:

* ``Company-eta_default_activity_code`` is NOT mandatory on the Company
  form — creating a Company, finishing the Setup Wizard, and submitting
  internal Sales Invoices must all work with no ETA Activity Code and no
  ETA credentials (mock / disabled mode).
* ETA master data is enforced exactly once, at the moment a LIVE
  submission to ETA is attempted, via
  ``utils.validate_live_submission_readiness`` — wired into both live
  choke points (``_submit_einvoice`` for e-invoices and
  ``EReceiptSubmitter.submit_ereceipt`` for e-receipts). It must fail
  with one clear message when the Activity Code, the default Connector,
  or the Client Credentials are missing, and let the path proceed when
  the company is properly configured.

The full end-to-end "internal Sales Invoice submits without a connector"
scenario lives in medagency's suite (``medagency.tests.test_eta_contract``),
whose helpers prepare the whole medagency tax-classification ritual; both
suites are run together as the regression gate for this contract.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from erpnext_egypt_compliance.erpnext_eta.utils import (
	validate_live_submission_readiness,
)

COMPANY = "ETA Readiness Test Co"
ABBR = "ERTC"
WIZARD_COMPANY = "ETA Wizard Path Co"
WIZARD_ABBR = "EWPC"


def _get_or_create(doctype, name, fields):
	if frappe.db.exists(doctype, name):
		return name
	doc = frappe.get_doc(dict({"doctype": doctype}, **fields))
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_company_prerequisites():
	"""Records the Setup Wizard normally creates (install_fixtures) and that
	Company.insert depends on — needed on a fresh site that has not run the
	wizard yet."""
	_get_or_create("Warehouse Type", "Transit", {"name": "Transit"})


class TestLiveSubmissionReadiness(IntegrationTestCase):
	def _make_company(self, with_activity_code=False):
		"""Idempotent: Company.insert commits internally (chart-of-accounts
		creation), so a test transaction rollback does NOT remove it — reuse
		the record across tests and align the activity code with what each
		test needs."""
		_ensure_company_prerequisites()
		code = None
		if with_activity_code:
			code = frappe.db.get_value("ETA Activity Code", {}, "name")
			if not code:
				self.skipTest("ETA Activity Code fixtures are not installed on this site")

		if frappe.db.exists("Company", COMPANY):
			frappe.db.set_value("Company", COMPANY, "eta_default_activity_code", code)
			return COMPANY

		fields = {
			"doctype": "Company",
			"company_name": COMPANY,
			"abbr": ABBR,
			"default_currency": "EGP",
			"country": "Egypt",
		}
		if code:
			fields["eta_default_activity_code"] = code
		return frappe.get_doc(fields).insert(ignore_permissions=True).name

	def _make_connector(self, company):
		"""Idempotent: reuse an existing connector (left over by an earlier
		run — Company creation commits flush prior inserts) and always
		re-align the dummy credentials with what the tests expect."""
		name = f"{company}-Pre-Production"
		if frappe.db.exists("ETA Connector", name):
			doc = frappe.get_doc("ETA Connector", name)
			doc.client_id = "readiness-test-client-id"
			doc.client_secret = "readiness-test-client-secret"
			doc.is_default = 1
			doc.save(ignore_permissions=True)
			return name
		return frappe.get_doc(
			{
				"doctype": "ETA Connector",
				"company": company,
				"environment": "Pre-Production",
				# Explicitly dummy values — no network, no real credentials.
				"client_id": "readiness-test-client-id",
				"client_secret": "readiness-test-client-secret",
				"is_default": 1,
			}
		).insert(ignore_permissions=True).name

	def _delete_connectors(self, company):
		for name in frappe.get_all("ETA Connector", filters={"company": company}, pluck="name"):
			frappe.delete_doc("ETA Connector", name, force=True, ignore_permissions=True)

	# ── the incident: the field must not be mandatory on Company ────────

	def test_activity_code_field_is_not_mandatory(self):
		df = frappe.get_meta("Company").get_field("eta_default_activity_code")
		if df is None:
			self.skipTest("eta_default_activity_code custom field is not installed")
		self.assertEqual(
			df.reqd,
			0,
			"eta_default_activity_code must not be reqd — it blocks Company creation "
			"and the Setup Wizard (the UAT MandatoryError incident)",
		)

	def test_company_creates_and_saves_without_activity_code(self):
		name = self._make_company(with_activity_code=False)
		self.assertFalse(frappe.db.get_value("Company", name, "eta_default_activity_code"))
		# A second save round-trip must pass too (form-level save in Desk).
		doc = frappe.get_doc("Company", name)
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.name, name)

	def test_setup_wizard_company_path_without_activity_code(self):
		"""The exact insert the Setup Wizard performs
		(erpnext setup_wizard company_setup.create_fiscal_year_and_company)
		must succeed without the ETA Activity Code."""
		_ensure_company_prerequisites()
		if frappe.db.exists("Company", WIZARD_COMPANY):
			self.assertTrue(
				not frappe.db.get_value("Company", WIZARD_COMPANY, "eta_default_activity_code")
			)
			return
		doc = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": WIZARD_COMPANY,
				"enable_perpetual_inventory": 1,
				"abbr": WIZARD_ABBR,
				"default_currency": "EGP",
				"country": "Egypt",
				"create_chart_of_accounts_based_on": "Standard Template",
			}
		).insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists("Company", doc.name))

	# ── internal invoices stay untouched without ETA setup ──────────────

	def test_before_submit_hook_ignores_company_without_eta_setup(self):
		"""The compliance app's Sales Invoice before_submit hook must return
		quietly for a company with no Activity Code and no connector — that
		is what keeps internal saves/submits working in mock/disabled mode.
		(The full submit end-to-end is covered by medagency's
		test_eta_contract.test_invoice_submits_without_eta_connector.)"""
		from erpnext_egypt_compliance.erpnext_eta import pre_validation

		company = self._make_company(with_activity_code=False)
		self._delete_connectors(company)

		# Must not raise: no connector ⇒ the ETA path is disengaged.
		pre_validation.validate_eta_before_submit(
			frappe._dict(
				name="READINESS-DRAFT-SI",
				company=company,
				pos_profile=None,
				posting_date=frappe.utils.today(),
			)
		)

	# ── the central gate on live submission ─────────────────────────────

	def test_live_gate_fails_clearly_without_activity_code(self):
		company = self._make_company(with_activity_code=False)
		connector_name = self._make_connector(company)
		connector = frappe.get_doc("ETA Connector", connector_name)

		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_live_submission_readiness(company, connector=connector)

		self.assertIn("ETA Default Activity Code", str(ctx.exception))

	def test_live_gate_fails_clearly_without_connector(self):
		company = self._make_company(with_activity_code=True)
		self._delete_connectors(company)

		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_live_submission_readiness(company)

		self.assertIn("ETA Connector", str(ctx.exception))

	def test_live_gate_fails_clearly_without_client_credentials(self):
		company = self._make_company(with_activity_code=True)
		self._delete_connectors(company)
		# client_id / client_secret are reqd at schema level only — a
		# connector saved with ignore_mandatory has neither, which is
		# exactly the half-configured state the gate must catch.
		connector = frappe.get_doc(
			{
				"doctype": "ETA Connector",
				"company": company,
				"environment": "Pre-Production",
				"is_default": 1,
			}
		)
		connector.flags.ignore_mandatory = True
		connector.insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_live_submission_readiness(company, connector=connector)

		message = str(ctx.exception)
		self.assertIn("Client ID", message)
		self.assertIn("Client Secret", message)

	def test_live_gate_passes_and_submission_reaches_http(self):
		"""With the Activity Code and a fully configured connector, the gate
		passes and the submission machinery proceeds to the HTTP layer
		(mocked — no network, no real credentials)."""
		from erpnext_egypt_compliance.erpnext_eta.einvoice_submitter import (
			EInvoiceSubmitter,
		)

		company = self._make_company(with_activity_code=True)
		connector_name = self._make_connector(company)
		connector = frappe.get_doc("ETA Connector", connector_name)

		self.assertIs(
			validate_live_submission_readiness(company, connector=connector),
			connector,
			"gate must return the connector when the company is submission-ready",
		)

		fake_response = frappe._dict(
			{
				"submissionId": "readiness-test-submission",
				"acceptedDocuments": [{"uuid": "U", "longId": "L", "internalId": "READINESS-SI"}],
				"rejectedDocuments": [],
				"status_code": 202,
			}
		)
		submitter = EInvoiceSubmitter(connector)
		with patch.object(
			EInvoiceSubmitter, "_send_submit_request", return_value=fake_response
		) as http:
			result = submitter.submit_documents([{"internalID": "READINESS-SI"}])

		http.assert_called_once()
		self.assertEqual(result.get("submissionId"), "readiness-test-submission")

	def test_einvoice_choke_point_invokes_gate(self):
		"""_submit_einvoice (manual button, Live on-signature, hourly Batch)
		must run the readiness gate before creating any ETA Log or touching
		the network — a gate failure propagates to the caller unchanged."""
		from erpnext_egypt_compliance.erpnext_eta.doctype.eta_log import (
			einvoice_logging_utils,
		)

		company = self._make_company(with_activity_code=True)
		connector_name = self._make_connector(company)
		connector = frappe.get_doc("ETA Connector", connector_name)

		with patch.object(
			einvoice_logging_utils,
			"validate_live_submission_readiness",
			side_effect=frappe.ValidationError("readiness-gate-marker"),
		) as gate:
			with self.assertRaises(frappe.ValidationError) as ctx:
				einvoice_logging_utils._submit_einvoice(
					[{"internalID": "READINESS-SI"}], connector, "Administrator"
				)

		gate.assert_called_once()
		self.assertIn("readiness-gate-marker", str(ctx.exception))

	def test_ereceipt_gate_runs_before_http(self):
		"""The e-receipt choke point invokes the same gate before any HTTP
		call: without an Activity Code it must raise the clear readiness
		error instead of posting to ETA."""
		from erpnext_egypt_compliance.erpnext_eta.ereceipt_submitter import (
			EReceiptSubmitter,
		)

		company = self._make_company(with_activity_code=False)
		if frappe.db.exists("ETA POS Connector", "READINESS-TEST-POS"):
			frappe.delete_doc(
				"ETA POS Connector", "READINESS-TEST-POS", force=True, ignore_permissions=True
			)
		connector = frappe.get_doc(
			{
				"doctype": "ETA POS Connector",
				# autoname is field:pos_profile; db_insert() bypasses autoname,
				# so the name is set explicitly.
				"name": "READINESS-TEST-POS",
				"pos_profile": "READINESS-TEST-POS",
				"pos_name": "Readiness Test POS",
				"serial_number": "READINESS-SN",
				"pos_os_version": "1.0",
				"environment": "Pre-Production",
				"client_id": "readiness-test-client-id",
				"client_secret": "readiness-test-client-secret",
			}
		)
		connector.flags.ignore_mandatory = True
		connector.db_insert()

		real_get_value = frappe.db.get_value

		def route_pos_profile(doctype, name, fieldname, *args, **kwargs):
			if doctype == "POS Profile":
				return company
			return real_get_value(doctype, name, fieldname, *args, **kwargs)

		submitter = EReceiptSubmitter(frappe.get_doc("ETA POS Connector", connector.name))
		with patch.object(frappe.db, "get_value", side_effect=route_pos_profile):
			with self.assertRaises(frappe.ValidationError) as ctx:
				submitter.submit_ereceipt([], "POS Invoice")

		self.assertIn("ETA Default Activity Code", str(ctx.exception))
