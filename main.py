# main.py
"""
FastAPI backend for ticket scanner app.
Endpoints:
 - GET  /api/attendee/{attendee_id}      -> fetch attendee from MongoDB
 - POST /api/attendee/{attendee_id}/mark -> mark attendance (requires scanner auth)
Config via environment variables (see README below).
"""
import os
import json
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from pymongo import MongoClient
from bson import ObjectId

# Optional Google Sheets
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- Config from env ---
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
MONGO_DB = client["ticket_admin"]
MONGO_COLLECTION = MONGO_DB["DHyefCvOk28GyaVN"]

# Simple scanner auth (set these as env vars on the server)
SCANNER_ID = os.getenv("SCANNER_ID", "scanner1")
SCANNER_PASSWORD = os.getenv("SCANNER_PASSWORD", "password123")

# Google sheets optional settings
GOOGLE_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # JSON content, not path
SHEETS_SPREADSHEET_ID = os.getenv("SHEETS_SPREADSHEET_ID")  # e.g. 1_xxx...
SHEETS_TAB_NAME = os.getenv("SHEETS_TAB_NAME", "Form_Responses_1")
SHEETS_ATTENDANCE_COL = os.getenv("SHEETS_ATTENDANCE_COL", "L")  # column letter to write attendance status
UPDATE_SHEETS_ON_MARK = os.getenv("UPDATE_SHEETS_ON_MARK", "false").lower() in ("1","true","yes")

# CORS origins (mobile app URL / wildcard during dev)
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")  # comma-separated

if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable must be set")

# --- Mongo client ---
mongo_client = MongoClient(MONGO_URI)
db = MONGO_DB
collection = MONGO_COLLECTION

# --- Optional Google Sheets client factory ---
def build_sheets_service():
    """
    Builds a Google Sheets service if GOOGLE_SERVICE_ACCOUNT_JSON is provided.
    GOOGLE_SERVICE_ACCOUNT_JSON should be the full JSON string of the service account.
    """
    if not GOOGLE_SA_JSON:
        return None
    cred_dict = json.loads(GOOGLE_SA_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(cred_dict, scopes=scopes)
    service = build("sheets", "v4", credentials=creds)
    return service

sheets_service = build_sheets_service()

# --- FastAPI app ---
app = FastAPI(title="Ticket Scanner Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if CORS_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --- Pydantic models ---
class MarkRequest(BaseModel):
    scanner_id: str
    scanner_password: str
    meta: Optional[dict] = None  # optional metadata, e.g. location, device id


# --- Helpers ---
def attendee_doc_to_dict(doc):
    """Convert Mongo document to JSON-serializable dict."""
    if not doc:
        return None
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = str(doc.get("attendee_id") or doc.get("_id"))
    return out

def update_google_sheet_mark(attendee_id, mark_value="Attended"):
    """Optional: update Google Sheet by searching for attendee_id in sheet and writing attendance."""
    if not sheets_service or not SHEETS_SPREADSHEET_ID:
        return False
    try:
        # Read all rows
        range_all = f"{SHEETS_TAB_NAME}!A:Z"
        result = sheets_service.spreadsheets().values().get(spreadsheetId=SHEETS_SPREADSHEET_ID, range=range_all).execute()
        rows = result.get("values", [])
        if not rows:
            return False
        # Find header and locate attendee id column if applicable; here we search by attendee_id in any cell
        found = False
        for idx, row in enumerate(rows[1:], start=2):  # skip header, rows indexed 1-based
            if attendee_id in row:
                # write mark_value into configured column
                range_to_write = f"{SHEETS_TAB_NAME}!{SHEETS_ATTENDANCE_COL}{idx}"
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=SHEETS_SPREADSHEET_ID,
                    range=range_to_write,
                    valueInputOption="RAW",
                    body={"values": [[mark_value]]}
                ).execute()
                found = True
                break
        return found
    except Exception as e:
        print("Error updating Google Sheet:", e)
        return False


# --- Endpoints ---
@app.get("/api/attendee/{attendee_id}")
def get_attendee(attendee_id: str):
    """
    Fetch attendee by attendee_id (the UUID stored in QR).
    Returns 404 if not found.
    """
    doc = collection.find_one({"attendee_id": attendee_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Attendee not found")
    return attendee_doc_to_dict(doc)


@app.post("/api/attendee/{attendee_id}/mark")
def mark_attendance(attendee_id: str, req: MarkRequest):
    """
    Mark attendance for an attendee.
    Requires scanner credentials in POST body (scanner_id, scanner_password).
    """
    # Basic auth check
    if req.scanner_id != SCANNER_ID or req.scanner_password != SCANNER_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid scanner credentials")

    doc = collection.find_one({"attendee_id": attendee_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Attendee not found")

    # Update DB: set attendance_status and attendance_ts
    update = {
        "attendance_status": "Attended",
        "attendance_ts": datetime.utcnow().isoformat(),
    }
    if req.meta:
        update["attendance_meta"] = req.meta

    collection.update_one({"attendee_id": attendee_id}, {"$set": update})

    # Optionally update Google Sheet
    sheet_updated = False
    if UPDATE_SHEETS_ON_MARK:
        try:
            sheet_updated = update_google_sheet_mark(attendee_id, mark_value="Attended")
        except Exception as e:
            print("Error updating sheet:", e)

    return {
        "ok": True,
        "message": "Attendance marked",
        "sheet_updated": sheet_updated
    }
