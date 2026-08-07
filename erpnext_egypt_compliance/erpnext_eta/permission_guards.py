import frappe
from frappe import _

#: Doctypes the e-Receipt API endpoints are allowed to operate on.
#: Anything else is rejected before any document lookup happens.
ERECEIPT_DOCTYPES = ("POS Invoice", "Sales Invoice")


def get_permitted_doc(doctype, docname, permission_type, allowed_doctypes=None):
    """Load the exact document and enforce a standard Frappe permission on it.

    The doctype allow-list (when given) is enforced before any document
    lookup so callers cannot probe arbitrary doctypes through these
    endpoints. Permission denials raise ``frappe.PermissionError`` and must
    propagate; callers must not invoke this inside broad ``except Exception``
    blocks.
    """
    if allowed_doctypes is not None and doctype not in allowed_doctypes:
        frappe.throw(
            _("Doctype {0} is not supported for this operation.").format(doctype),
            title=_("ETA Validation"),
        )
    doc = frappe.get_doc(doctype, docname)
    doc.check_permission(permission_type)
    return doc
