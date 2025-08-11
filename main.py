# main.py
"""
FastAPI backend for ticket scanner app.
Endpoints:
 - GET  /api/attendees                   -> fetch all attendees
 - POST /api/login                       -> validate scanner credentials
 - GET  /api/attendee/{attendee_id}      -> fetch a single attendee
 - POST /api/attendee/{attendee_id}/mark -> mark attendance
Config via environment variables.
"""
import os
import json
from dotenv import load_dotenv
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from pymongo import MongoClient

# Load environment variables from a .env file for local development
load_dotenv()

# --- Configuration from Environment Variables ---

# MongoDB connection details (now fully dynamic)
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME")
MONGO_COLLECTION_NAME = os.getenv("MONGO_COLLECTION_NAME")

# Scanner credentials (required)
SCANNER_ID = os.getenv("SCANNER_ID")
SCANNER_PASSWORD = os.getenv("SCANNER_PASSWORD")

# Google Sheets integration (optional)
GOOGLE_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEETS_SPREADSHEET_ID = os.getenv("SHEETS_SPREADSHEET_ID")
SHEETS_TAB_NAME = os.getenv("SHEETS_TAB_NAME", "Form_Responses_1")
UPDATE_SHEETS_ON_MARK = os.getenv("UPDATE_SHEETS_ON_MARK", "false").lower() in ("true", "1", "yes")

# CORS origins for the mobile app
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

# --- Critical Configuration Checks ---
# The application will not start if these required variables are missing.
if not MONGO_URI:
    raise RuntimeError("FATAL: MONGO_URI environment variable must be set.")
if not MONGO_DB_NAME:
    raise RuntimeError("FATAL: MONGO_DB_NAME environment variable must be set.")
if not MONGO_COLLECTION_NAME:
    raise RuntimeError("FATAL: MONGO_COLLECTION_NAME environment variable must be set.")
if not SCANNER_ID or not SCANNER_PASSWORD:
    raise RuntimeError("FATAL: SCANNER_ID and SCANNER_PASSWORD environment variables must be set.")

# --- Database Client Initialization ---
try:
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB_NAME]
    collection = db[MONGO_COLLECTION_NAME]
    # Test the connection
    client.admin.command('ping')
    print("✅ MongoDB connection successful.")
except Exception as e:
    raise RuntimeError(f"❌ Could not connect to MongoDB: {e}")


# --- Optional Google Sheets Service ---
def build_sheets_service():
    """Builds and returns a Google Sheets service client if configured."""
    if not GOOGLE_SA_JSON:
        return None
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        cred_dict = json.loads(GOOGLE_SA_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(cred_dict, scopes=scopes)
        service = build("sheets", "v4", credentials=creds)
        print("✅ Google Sheets service initialized.")
        return service
    except Exception as e:
        print(f"⚠️ Warning: Could not initialize Google Sheets service: {e}")
        return None

sheets_service = build_sheets_service()

# --- FastAPI Application Setup ---
app = FastAPI(title="Ticket Scanner Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# --- Pydantic Models for API Requests ---
class LoginRequest(BaseModel):
    scanner_id: str
    scanner_password: str

class MarkRequest(BaseModel):
    scanner_id: str
    scanner_password: str
    meta: Optional[dict] = None

# --- Helper Functions ---
def attendee_doc_to_dict(doc):
    """Converts a MongoDB document to a JSON-serializable dictionary."""
    if not doc:
        return None
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = str(doc.get("attendee_id") or doc.get("_id"))
    return out

def update_google_sheet_mark(attendee_id: str, mark_value: str = "Attended") -> bool:
    """Updates the 'Attendance' column in Google Sheets for a given attendee ID."""
    if not sheets_service or not SHEETS_SPREADSHEET_ID:
        return False
    
    try:
        range_all = f"{SHEETS_TAB_NAME}!A:Z"
        result = sheets_service.spreadsheets().values().get(spreadsheetId=SHEETS_SPREADSHEET_ID, range=range_all).execute()
        rows = result.get("values", [])
        if not rows: return False
        
        header = rows[0]
        data_rows = rows[1:]

        id_col_index = header.index("Attendee ID")
        attendance_col_index = header.index("Attendance")

        row_index = -1
        for idx, row in enumerate(data_rows):
            if len(row) > id_col_index and row[id_col_index] == attendee_id:
                row_index = idx + 2
                break
        
        if row_index == -1: return False

        attendance_col_letter = chr(ord('A') + attendance_col_index)
        range_to_write = f"{SHEETS_TAB_NAME}!{attendance_col_letter}{row_index}"
        
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SHEETS_SPREADSHEET_ID,
            range=range_to_write,
            valueInputOption="RAW",
            body={"values": [[mark_value]]}
        ).execute()
        
        return True
    except Exception as e:
        print(f"❌ An exception occurred during sheet update: {e}")
        return False

# --- API Endpoints ---

@app.post("/api/login")
def login(req: LoginRequest):
    """Validates scanner credentials."""
    if req.scanner_id == SCANNER_ID and req.scanner_password == SCANNER_PASSWORD:
        return {"ok": True, "message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid scanner credentials")

@app.get("/api/attendees")
def get_all_attendees():
    """Fetches a list of all attendees from the database."""
    docs = collection.find({}, {"name": 1, "attendee_id": 1, "attendance_status": 1, "_id": 0})
    return list(docs)

@app.get("/api/attendee/{attendee_id}")
def get_attendee(attendee_id: str):
    """Fetches full details for a single attendee from the database."""
    # Exclude fields that are not needed on the details screen
    projection = {"_id": 0, "Timestamp": 0, "Ticket Status": 0, "Email Status": 0}
    doc = collection.find_one({"attendee_id": attendee_id}, projection)
    if not doc:
        raise HTTPException(status_code=404, detail="Attendee not found")
    return doc

@app.post("/api/attendee/{attendee_id}/mark")
def mark_attendance(attendee_id: str, req: MarkRequest):
    """Marks an attendee as present, updating the sheet first, then the database."""
    if req.scanner_id != SCANNER_ID or req.scanner_password != SCANNER_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid scanner credentials")

    doc = collection.find_one({"attendee_id": attendee_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Attendee not found")

    if doc.get("attendance_status") == "Attended":
        raise HTTPException(status_code=409, detail=f"Ticket already used at {doc.get('attendance_ts', 'N/A')}")

    if UPDATE_SHEETS_ON_MARK:
        sheet_updated = update_google_sheet_mark(attendee_id)
        if not sheet_updated:
            return {"ok": False, "message": "Failed to update Google Sheet. Attendance not marked.", "sheet_updated": False}

    update_data = {
        "attendance_status": "Attended",
        "attendance_ts": datetime.utcnow().isoformat(),
    }
    if req.meta:
        update_data["attendance_meta"] = req.meta
    
    collection.update_one({"attendee_id": attendee_id}, {"$set": update_data})

    return {
        "ok": True,
        "message": "Attendance marked successfully.",
        "sheet_updated": UPDATE_SHEETS_ON_MARK and sheet_updated
    }
