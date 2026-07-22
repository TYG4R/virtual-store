"""
App configuration.

All secrets are read from environment variables so nothing sensitive
lives in the code. For local/manual hosting we also support a plain
`.env` file (KEY=VALUE per line) — no extra dependency needed for that,
it's parsed by the tiny loader below.
"""
import os
import secrets


def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

# --- Core ---
# No hardcoded fallback here on purpose: a secret key baked into source code
# (visible to anyone who can read this repo) defeats the point of a secret.
# If you don't set SECRET_KEY yourself, a random one is generated at startup
# instead — sessions just won't survive a restart until you set a real one.
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
SECRET_KEY_WAS_GENERATED = "SECRET_KEY" not in os.environ

DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# --- Database ---
DB_PATH = os.environ.get("DB_PATH", os.path.join("instance", "store.db"))

# --- Razorpay ---
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# --- First-run admin account (only used the very first time the DB is created) ---
# Same reasoning as SECRET_KEY: no predictable fallback password. If
# ADMIN_PASSWORD isn't set, database.py generates a random one on first run
# and writes it to instance/INITIAL_ADMIN_PASSWORD.txt so you can log in once
# and then change it immediately from the admin panel.
DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# --- Product file uploads (for auto-delivery download links) ---
PRODUCT_UPLOAD_FOLDER = os.path.join("static", "product_files")
ALLOWED_PRODUCT_EXTENSIONS = {"pdf", "zip", "txt", "csv", "json", "xml", "doc", "docx", "xlsx", "jpg", "jpeg", "png", "gif", "mp3", "mp4", "epub", "mobi"}
MAX_PRODUCT_FILE_MB = int(os.environ.get("MAX_PRODUCT_FILE_MB", "100"))

# --- Uploads ---
UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_SIZE_MB = 5
MAX_IMAGE_DIMENSION = 1600  # longest side, in pixels — keeps the site fast

# --- Optional: email notifications to customers (SMTP). Leave blank to disable. ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)

# --- Optional: Cloudflare Turnstile CAPTCHA (free, no request limits) ---
# Leave both blank to disable — the site works fine without it, just with
# less bot protection on login/checkout/newsletter forms. Get free keys at
# https://dash.cloudflare.com/ -> Turnstile.
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")

# --- Optional: Firebase Phone Authentication (OTP sign-in / account creation) ---
# Lets customers verify their phone number with a one-time SMS code, right
# from the homepage or at checkout, instead of (or alongside) guest checkout.
# All six values below come from Firebase Console -> Project settings ->
# General -> "Your apps" -> Web app (</> icon) -> SDK setup and config.
# They are meant to be public (shipped to the browser) — Firebase's security
# model relies on backend ID-token verification, not on hiding these.
# Leave FIREBASE_API_KEY blank to disable phone sign-in entirely; the site
# works fine as guest-checkout-only without it.
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_AUTH_DOMAIN = os.environ.get("FIREBASE_AUTH_DOMAIN", "")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
FIREBASE_APP_ID = os.environ.get("FIREBASE_APP_ID", "")
FIREBASE_MESSAGING_SENDER_ID = os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "")
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "")

# --- Optional: Firebase Cloud Messaging (push notifications to the Android
# admin app). This is a *different* Firebase credential from the six
# FIREBASE_* values above: those are public web-SDK keys used to verify
# customer phone sign-in, this one is a private, server-only service-account
# key used to *send* pushes via the firebase-admin SDK. Never expose it to a
# browser or commit it to source control.
#
# Get it from: Firebase Console -> Project settings -> Service accounts ->
# "Generate new private key" (downloads a JSON file).
#
# Two ways to supply it, either works:
#   1. FIREBASE_SERVICE_ACCOUNT_JSON = the full JSON contents, pasted as one
#      env var value (handiest on hosts like Render/Railway that don't let
#      you upload a file).
#   2. FIREBASE_SERVICE_ACCOUNT_FILE = a filesystem path to the downloaded
#      JSON file (handiest for local development).
# Leave both blank to disable push notifications entirely — the rest of the
# site and the /api/admin/* endpoints work fine without it, admins just won't
# get a phone alert when an order comes in.
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "")

# Web Push (VAPID) — used for browser push notifications on the admin panel.
# Generate a key pair with: openssl ecparam -genkey -name prime256v1 -noout -out vapid_private.pem
# Then: openssl ec -in vapid_private.pem -pubout -out vapid_public.pem
# Store the base64-encoded (URL-safe) raw public key and the PEM private key.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "admin@virtualstore.local")

# --- Android admin app: bearer-token API auth ---
# How long a /api/admin/login token stays valid before the app needs to log
# in again. Independent of the web admin's session-cookie lifetime above.
API_TOKEN_EXPIRY_DAYS = int(os.environ.get("API_TOKEN_EXPIRY_DAYS", "30") or 30)

# --- Optional: transactional email via SendGrid or Resend (HTTP APIs) ---
# Preferred over SMTP when set — no app-password hassle, better deliverability,
# generous free tiers. If both are set, Resend is tried first, then SendGrid,
# falling back to SMTP (above) if neither is configured. Leave all blank and
# the site simply skips emailing (admin still sees everything in the panel).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_FROM)
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "")

# --- Optional: SMS gateway for OTP delivery ---
# When SMS_GATEWAY is "dev", OTP codes are returned to the frontend (shown in
# the UI) so you can test the full flow without an SMS provider. Set to "twilio"
# or another provider when ready for real SMS. Leave blank to default to "dev".
OTP_EXPIRY_MINUTES = int(os.environ.get("OTP_EXPIRY_MINUTES", "5"))
# Security: default to FALSE in production. OTP codes are only returned to the
# client when this is true, so the default must never expose verification codes
# on a live deployment. Set OTP_DEV_MODE=true locally for testing without SMS.
OTP_DEV_MODE = os.environ.get("OTP_DEV_MODE", "false").lower() == "true"
# --- Twilio SMS (for OTP delivery) ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_PHONE = os.environ.get("TWILIO_PHONE", "") or TWILIO_FROM_NUMBER

# --- Optional: Calendarific holiday API (provides accurate festival/holiday data) ---
# Get a free API key at https://calendarific.com/
# When set, the site auto-fetches holidays for the configured country on each
# admin settings save and caches them for greeting display. Leave blank to
# rely on the built-in static list only.
CALENDARIFIC_API_KEY = os.environ.get("CALENDARIFIC_API_KEY", "")
# ISO 3166-1 alpha-2 country code for holiday lookups (default: IN = India)
CALENDARIFIC_COUNTRY = os.environ.get("CALENDARIFIC_COUNTRY", "IN")

# --- Optional: Google OAuth (direct, no Firebase) ---
# Google Client ID for direct OAuth sign-in via Google Identity Services (GIS).
# Set this in your environment to enable fast Google sign-in without the
# Firebase SDK overhead. When empty, the Google sign-in button is hidden.
# Get a client ID from: https://console.cloud.google.com/apis/credentials
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

# --- Downloads ---
MAX_DOWNLOADS = 5  # max times a customer can re-download before the token expires
