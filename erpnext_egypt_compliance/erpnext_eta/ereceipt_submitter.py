import frappe
from erpnext_egypt_compliance.erpnext_eta.doctype.eta_pos_connector.eta_pos_connector import ETASession
import json
import requests

class EReceiptSubmitter:
    """
    A class to submit e-receipts to the ETA portal.

    Attributes:
        eta_connector: An instance of ETA connector to handle sessions and tokens.
    """

    def __init__(self, eta_connector):
        """
        Initialize the EReceiptSubmitter with an ETA connector.
        """
        self.eta_connector = eta_connector

    def submit_ereceipt(self, ereceipts, doctype, eta_log):
        """
        Submit e-receipts to the ETA portal.

        Args:
            ereceipts (dict): A dictionary of e-receipts to be submitted.
            doctype (str): The document type of the receipts.
            eta_log (Document): The pre-created ETA Log submission intent
                (status "Started", already committed by the caller before
                this external POST). It is updated in place from the ETA
                response; it is never created here.

        Returns:
            dict: The response from the ETA portal.

        Failure semantics: an ambiguous failure (network timeout/exception,
        or a local error after ETA may have accepted) leaves the intent at
        "Started" and therefore blocked for retry pending operator
        reconciliation; it is NOT marked retryable here. Only a definitive
        ETA response updates the intent (Completed/Partially Succeeded/Failed).
        """
        try:
            headers = self._get_headers()
            url = self._get_submission_url()
            data = self._prepare_data(ereceipts)
        except Exception as e:
            # Pre-POST failure (headers/url/payload preparation): the ETA
            # portal was definitely never reached, so the intent is
            # conclusively Failed and retryable — record and commit that.
            self._handle_exception(e)
            eta_log.eta_response = str(e)
            eta_log.submission_status = "Failed"
            eta_log.save()
            frappe.db.commit()  # nosemgrep: frappe-manual-commit
            return {"error": str(e)}

        # Irreversible boundary: from here on the POST may have reached ETA.
        # Any exception from _send_submit_request or afterwards is ambiguous
        # and must leave the already-committed intent "Started" (no save,
        # no commit) pending operator reconciliation.
        try:
            eta_response = self._send_submit_request(url, headers, data)
            processed_response = self._process_response(eta_response, ereceipts, doctype, eta_log)
            frappe.db.commit()  # nosemgrep: frappe-manual-commit
            return processed_response
        except Exception as e:
            self._handle_exception(e)
            return {"error": str(e)}
    
    def get_receipt_submission(self, submission_id):
        """
        Get the submission details from the ETA portal.
        """
        try:
            access_token = self.eta_connector.get_access_token()
            headers = self._get_headers()

            url = f"{self.eta_connector.ETA_BASE}/receiptsubmissions/{submission_id}/details?PageNo=1&PageSize=100"
            eta_session = ETASession().get_session()
            eta_response = eta_session.get(url, headers=headers)
            eta_response.raise_for_status()
            eta_data = eta_response.json()

            return eta_data

        except requests.RequestException as e:
            frappe.log_error(title="Get e-receipt submission", message=str(e))
            return f"Error when Get e-receipt submission: {str(e)}"

        except Exception as e:
            frappe.log_error(title="Unexpected error in get_receipt_submission", message=str(e))
    
    def get_receipt_status(self, uuid):
        try:
            access_token = self.eta_connector.get_access_token()
            headers = self._get_headers()

            url = f"{self.eta_connector.ETA_BASE}/receipts/{uuid}/raw/"
            eta_session = ETASession().get_session()
            eta_response = eta_session.get(url, headers=headers)
            eta_response.raise_for_status()
            eta_data = eta_response.json()
            return eta_data

        except requests.RequestException as e:
            frappe.log_error(title="Get e-receipt Status", message=str(e))
            return f"Error when Get e-receipt status: {str(e)}"

    def _get_headers(self):
        """
        Get the headers required for the request.

        Returns:
            dict: A dictionary of headers.
        """
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer " + self.eta_connector.get_access_token(),
        }

    def _get_submission_url(self):
        return self.eta_connector.ETA_BASE + "/receiptsubmissions"

    def _prepare_data(self, ereceipts):
        """
        Prepare the e-receipts data for submission.

        Args:
            ereceipts (dict): A dictionary of e-receipts.

        Returns:
            bytes: The JSON-encoded data.
        """
        return json.dumps(ereceipts, ensure_ascii=False).encode("utf8")

    def _send_submit_request(self, url, headers, data):
        """
        Send the request to the ETA portal.

        Args:
            url (str): The submission URL.
            headers (dict): The request headers.
            data (bytes): The JSON-encoded data.

        Returns:
            dict: The response from the ETA portal.
        """
        eta_session = ETASession().get_session()
        response = eta_session.post(url, headers=headers, data=data)
        _eta_response = frappe._dict(response.json())
        _eta_response["status_code"] = response.status_code or None
        return _eta_response

    def _process_response(self, eta_response, ereceipts, doctype, eta_log):
        """
        Process the response from the ETA portal.

        Args:
            eta_response (dict): The response from the ETA portal.
            ereceipts (dict): The submitted e-receipts.
            doctype (str): The document type of the receipts.
            eta_log (Document): The pre-created submission intent ETA Log.

        Returns:
            dict: The processed response.
        """
        if eta_response.get("acceptedDocuments") or eta_response.get("rejectedDocuments"):
            self._handle_success_response(eta_response, ereceipts, eta_log, doctype)

        if eta_response.get("error"):
            self._handle_error_response(eta_response, eta_log)

        return eta_response

    def _handle_success_response(self, eta_response, ereceipts, eta_log, doctype):
        """
        Handle the successful response from the ETA portal.

        Args:
            eta_response (dict): The response from the ETA portal.
            ereceipts (dict): The submitted e-receipts.
            eta_log (Document): The ETA Log document.
        """
        summary_message = (
            f"Total no of receipts: {len(ereceipts.get('receipts'))}\n"
            f"Total no of accepted: {len(eta_response.get('acceptedDocuments', []))}\n"
            f"Total no of rejected: {len(eta_response.get('rejectedDocuments', []))}\n"
        ).strip()
        submission_status = (
            "Partially Succeeded" if (len(eta_response.get('acceptedDocuments', [])) < len(ereceipts.get('receipts', [])))
            else "Completed"
        )
        eta_log.status_code = eta_response.get("status_code", None)
        eta_log.submission_id = eta_response.get("submissionId")
        eta_log.submission_summary = summary_message
        eta_log.submission_status = submission_status
        # Process documents before saving so child-row updates (uuid,
        # long_id, accepted, error) are persisted by the same save.
        # ETALog.process_documents takes only the ETA response.
        eta_log.process_documents(eta_response)
        eta_log.save()

    def _handle_error_response(self, eta_response, initial_eta_log):
        """
        Handle the error response from the ETA portal.

        Args:
            eta_response (dict): The response from the ETA portal.
            initial_eta_log (Document): The initial log document.
        """
        initial_eta_log.status_code = eta_response.get("status_code", None)
        initial_eta_log.eta_response = str(eta_response.get("error"))
        initial_eta_log.submission_status = "Failed"
        initial_eta_log.save()

    def _handle_exception(self, exception):
        """
        Handle exceptions that occur during the submission process.

        Args:
            exception (Exception): The caught exception.
        """
        traceback = frappe.get_traceback()
        frappe.log_error("Submit E-Receipt", message=traceback)
