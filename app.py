import os
import json
import hmac
import hashlib
import secrets
import string
from datetime import datetime, timezone

from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]

# Firebase init
service_account_info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
cred = credentials.Certificate(service_account_info)
firebase_admin.initialize_app(cred)
db = firestore.client()


def verify_signature(raw_body: bytes, signature: str) -> bool:
    digest = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, signature or "")


def generate_license_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    parts = [
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
    ]
    return "XP-" + "-".join(parts)


def get_or_create_license_for_subscription(subscription_id: str, customer_email: str, product_name: str):
    # Aynı subscription için ikinci kez key üretme
    existing = (
        db.collection("licenses")
        .where("subscription_id", "==", subscription_id)
        .limit(1)
        .stream()
    )
    existing = list(existing)
    if existing:
        doc = existing[0]
        return doc.id, doc.to_dict()

    # Unique key üret
    while True:
        license_key = generate_license_key()
        doc_ref = db.collection("licenses").document(license_key)
        if not doc_ref.get().exists:
            break

    payload = {
        "active": True,
        "used": False,
        "device_id": "",
        "created_at": datetime.now(timezone.utc),
        "customer_email": customer_email or "",
        "subscription_id": subscription_id or "",
        "product_name": product_name or "",
        "source": "lemon_squeezy",
    }
    db.collection("licenses").document(license_key).set(payload)
    return license_key, payload


@app.get("/")
def home():
    return jsonify({"ok": True, "service": "xpulse-backend"}), 200


@app.post("/webhook")
def webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Signature", "")
    event_name = request.headers.get("X-Event-Name", "")

    if not verify_signature(raw_body, signature):
        return jsonify({"ok": False, "error": "invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    data = payload.get("data", {})
    attributes = data.get("attributes", {})
    meta = payload.get("meta", {})

    # Subscription ilk oluştuğunda license üret
    if event_name == "subscription_created":
        subscription_id = str(data.get("id", ""))
        customer_email = (
            attributes.get("user_email")
            or attributes.get("customer_email")
            or meta.get("custom_data", {}).get("email")
            or ""
        )
        product_name = attributes.get("product_name", "Xpulse Pro")

        license_key, _ = get_or_create_license_for_subscription(
            subscription_id=subscription_id,
            customer_email=customer_email,
            product_name=product_name,
        )

        # İstersen burada ayrıca "orders" koleksiyonuna log da atabilirsin
        db.collection("webhook_logs").add({
            "event": event_name,
            "subscription_id": subscription_id,
            "customer_email": customer_email,
            "license_key": license_key,
            "created_at": datetime.now(timezone.utc),
        })

        return jsonify({"ok": True, "license_key": license_key}), 200

    # Subscription iptal / expiry olursa lisansı pasife çek
    if event_name in {"subscription_expired", "subscription_cancelled"}:
        subscription_id = str(data.get("id", ""))
        docs = (
            db.collection("licenses")
            .where("subscription_id", "==", subscription_id)
            .limit(10)
            .stream()
        )
        for doc in docs:
            doc.reference.update({
                "active": False,
                "updated_at": datetime.now(timezone.utc),
            })

        db.collection("webhook_logs").add({
            "event": event_name,
            "subscription_id": subscription_id,
            "created_at": datetime.now(timezone.utc),
        })

        return jsonify({"ok": True}), 200

    # Geri kalan event'leri şimdilik sadece kabul et
    db.collection("webhook_logs").add({
        "event": event_name,
        "created_at": datetime.now(timezone.utc),
    })
    return jsonify({"ok": True, "ignored": event_name}), 200
