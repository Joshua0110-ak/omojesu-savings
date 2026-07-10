import uuid
import urllib.request
import urllib.error
import json
from django.conf import settings


def generate_reference():
    """Generate a unique payment reference."""
    return f"OMJS-{uuid.uuid4().hex[:12].upper()}"


def initialize_transaction(email, amount_naira, member_id, callback_url):
    """
    Initialize a Paystack transaction.
    FIX: Added User-Agent header — Cloudflare was blocking urllib with a 403 (error 1010).
    """
    amount_kobo = int(float(amount_naira) * 100)
    reference = generate_reference()

    payload = json.dumps({
        "email": email,
        "amount": amount_kobo,
        "reference": reference,
        "callback_url": callback_url,
        "metadata": {
            "member_id": member_id,
        }
    }).encode()

    req = urllib.request.Request(
        "https://api.paystack.co/transaction/initialize",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())

        if data.get("status"):
            return {
                "status": True,
                "authorization_url": data["data"]["authorization_url"],
                "reference": data["data"]["reference"],
            }
        return {"status": False, "message": data.get("message", "Unknown error")}

    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        return {"status": False, "message": f"HTTP Error {e.code}: {error_body}"}

    except Exception as e:
        return {"status": False, "message": str(e)}


def verify_transaction(reference):
    """
    Verify a Paystack transaction by reference.
    FIX: Added User-Agent header for same Cloudflare reason.
    """
    req = urllib.request.Request(
        f"https://api.paystack.co/transaction/verify/{reference}",
        headers={
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())

        if data.get("status") and data.get("data", {}).get("status") == "success":
            return {
                "status": True,
                "amount_naira": data["data"]["amount"] / 100,
                "email": data["data"]["customer"]["email"],
                "member_id": data["data"].get("metadata", {}).get("member_id"),
            }
        return {"status": False, "message": data.get("message", "Transaction not successful")}

    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        return {"status": False, "message": f"HTTP Error {e.code}: {error_body}"}

    except Exception as e:
        return {"status": False, "message": str(e)}