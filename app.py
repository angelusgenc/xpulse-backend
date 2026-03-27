import os
import json
import secrets
import string
from datetime import datetime, timezone

import resend
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import timedelta

app = Flask(__name__)

FIREBASE_SERVICE_ACCOUNT_JSON = os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
FROM_EMAIL = os.environ.get("FROM_EMAIL", "support@xpulselabs.com")
GUMROAD_SELLER_ID = os.environ.get("GUMROAD_SELLER_ID", "").strip()

# Firebase init
service_account_info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
cred = credentials.Certificate(service_account_info)
firebase_admin.initialize_app(cred)
db = firestore.client()

resend.api_key = RESEND_API_KEY


def utc_now():
    return datetime.now(timezone.utc)


def generate_license_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    parts = [
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
    ]
    return "XP-" + "-".join(parts)


def send_license_email(customer_email: str, license_key: str):
    if not customer_email:
        return

    resend.Emails.send({
        "from": f"XPulse Pro <{FROM_EMAIL}>",
        "to": [customer_email],
        "subject": "Your XPulse Pro license key",
        "html": f"""
        <h2>Your XPulse Pro license key</h2>
        <p>Thank you for your purchase.</p>
        <p><strong>License Key:</strong> {license_key}</p>
        <p>Open XPulse Pro, paste your key into the activation screen, and click <strong>Activate</strong>.</p>
        <p>If you need help, reply to this email.</p>
        """
    })


def parse_plan_name(data: dict) -> str:
    recurrence = str(data.get("recurrence", "")).strip().lower()
    tier = str(data.get("variants[Tier]", "")).strip().lower()
    price_name = str(data.get("price_name", "")).strip().lower()
    variant = str(data.get("variant", "")).strip().lower()
    variants_and_quantity = str(data.get("variants_and_quantity", "")).strip().lower()
    product_name = str(data.get("product_name", "")).strip().lower()

    joined = " | ".join(
        x for x in [recurrence, tier, price_name, variant, variants_and_quantity, product_name] if x
    )

    if recurrence == "monthly":
        return "monthly"
    if recurrence in ("yearly", "annual"):
        return "yearly"

    if any(word in joined for word in ["year", "annual", "yearly"]):
        return "yearly"
    if any(word in joined for word in ["month", "monthly"]):
        return "monthly"

    return "unknown"


def get_or_create_license(source_id: str, customer_email: str, product_name: str, plan_name: str, raw_data: dict):
    existing = (
        db.collection("licenses")
        .where("source", "==", "gumroad")
        .where("source_id", "==", source_id)
        .limit(1)
        .stream()
    )
    existing = list(existing)

    now = utc_now()

    # PLAN SÜRESİ
    if plan_name == "monthly":
        delta = timedelta(days=30)
    elif plan_name == "yearly":
        delta = timedelta(days=365)
    else:
        delta = timedelta(days=0)

    # 🔥 VARSA → SÜRE UZAT
    if existing:
        doc = existing[0]
        license_key = doc.id
        license_data = doc.to_dict()

        current_expiry = license_data.get("expires_at")

        if current_expiry and current_expiry > now:
            new_expiry = current_expiry + delta
        else:
            new_expiry = now + delta

        db.collection("licenses").document(license_key).update({
            "expires_at": new_expiry,
            "updated_at": now
        })

        return license_key, license_data, False

    # 🔥 YOKSA → YENİ OLUŞTUR
    while True:
        license_key = generate_license_key()
        doc_ref = db.collection("licenses").document(license_key)
        if not doc_ref.get().exists:
            break

    expires_at = now + delta if delta else None

    payload = {
        "active": True,
        "used": False,
        "device_id": "",
        "created_at": now,
        "updated_at": now,
        "customer_email": customer_email or "",
        "product_name": product_name or "XPulse Pro",
        "plan_name": plan_name or "unknown",
        "source": "gumroad",
        "source_id": source_id,
        "raw": raw_data,
        "expires_at": expires_at,
    }

    db.collection("licenses").document(license_key).set(payload)

    return license_key, payload, True


@app.get("/")
def home():
    return jsonify({"ok": True, "service": "xpulse-backend", "provider": "gumroad"}), 200


@app.post("/webhook/gumroad")
def gumroad_webhook():
    data = request.form.to_dict(flat=True)

    db.collection("webhook_logs").add({
        "provider": "gumroad",
        "kind": "incoming",
        "payload": data,
        "created_at": utc_now(),
    })

    # Optional seller validation
    incoming_seller_id = (data.get("seller_id") or "").strip()
    if GUMROAD_SELLER_ID and incoming_seller_id != GUMROAD_SELLER_ID:
        db.collection("webhook_logs").add({
            "provider": "gumroad",
            "kind": "rejected_seller_id",
            "payload": data,
            "created_at": utc_now(),
        })
        return jsonify({"ok": False, "error": "invalid seller_id"}), 403

    # Gumroad test ping may not include normal purchase fields.
    customer_email = (data.get("email") or data.get("purchaser_email") or "").strip().lower()
    product_name = (data.get("product_name") or "XPulse Pro").strip()
    plan_name = parse_plan_name(data)

    # Prefer recurring subscription id when present so renewals reuse same key.
    source_id = (
        data.get("subscription_id")
        or data.get("subscription_token")
        or data.get("sale_id")
        or data.get("purchase_id")
        or data.get("order_id")
        or ""
    ).strip()

    if not customer_email or not source_id:
        return jsonify({
            "ok": True,
            "message": "ping received",
            "license_created": False
        }), 200

    license_key, _, created = get_or_create_license(
        source_id=source_id,
        customer_email=customer_email,
        product_name=product_name,
        plan_name=plan_name,
        raw_data=data,
    )

    if created:
        send_license_email(customer_email, license_key)

        db.collection("webhook_logs").add({
            "provider": "gumroad",
            "kind": "license_created",
            "customer_email": customer_email,
            "license_key": license_key,
            "source_id": source_id,
            "plan_name": plan_name,
            "created_at": utc_now(),
        })

    return jsonify({
        "ok": True,
        "license_created": created,
        "license_key": license_key,
        "plan_name": plan_name,
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
