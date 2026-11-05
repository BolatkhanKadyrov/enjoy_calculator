"""Non-persistent draft metadata and signed acknowledgements of successful saves."""
import uuid
from urllib.parse import urlencode

from django.core import signing
from django.shortcuts import redirect

RECEIPT_SALT = "tseh-draft-saved-v1"
RECEIPT_MAX_AGE = 300


def request_id(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        return ""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return ""


def draft_context(request, state):
    return {
        "draft_revision": str(state.revision),
        "draft_epoch": str(state.epoch),
        "draft_request": request_id(request.POST.get("_draft_request")) if request.method == "POST" else "",
        "draft_user_scope": str(request.user.pk),
    }


def saved_redirect(request, detail, **kwargs):
    response = redirect(detail, **kwargs)
    marker = request_id(request.POST.get("_draft_request"))
    if marker:
        receipt = signing.dumps({"user": str(request.user.pk), "path": request.path, "request": marker},
                                salt=RECEIPT_SALT)
        response["Location"] += "?" + urlencode({"draft_saved": receipt})
    return response


def saved_receipt(request):
    raw = request.GET.get("draft_saved", "")
    if not raw or len(raw) > 4096:
        return None
    try:
        payload = signing.loads(raw, salt=RECEIPT_SALT, max_age=RECEIPT_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"user", "path", "request"}:
        return None
    if payload["user"] != str(request.user.pk) or not isinstance(payload["path"], str):
        return None
    if not payload["path"].startswith("/") or payload["path"].startswith("//"):
        return None
    if not request_id(payload["request"]) or request_id(payload["request"]) != payload["request"]:
        return None
    return payload


def receipt_context(request):
    payload = saved_receipt(request)
    return {"draft_saved_receipt": payload} if payload is not None else {}
