import os
import json
import firebase_admin
from firebase_admin import credentials, firestore

# Production (Render): credentials come from the SERVICE_ACCOUNT_KEY env var,
# which holds the full JSON of the service account key.
# Local dev: fall back to the serviceAccountKey.json file (gitignored).
service_account_json = os.environ.get("SERVICE_ACCOUNT_KEY")
if service_account_json:
    cred = credentials.Certificate(json.loads(service_account_json))
else:
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred)

db = firestore.client()
