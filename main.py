import os
import time
import requests
from typing import List, Optional

from fastapi import FastAPI, Form, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from pydantic import EmailStr
from dotenv import load_dotenv

load_dotenv()

# === ENV CONFIG ===
ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ZOHO_ORG_ID = os.getenv("ZOHO_ORG_ID")
ZOHO_DEPARTMENT_ID = os.getenv("ZOHO_DEPARTMENT_ID")

# Change for India region if needed
ZOHO_ACCOUNTS_DOMAIN = os.getenv("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.com")
ZOHO_DESK_BASE = os.getenv("ZOHO_DESK_BASE", "https://desk.zoho.com")

if not all([ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID, ZOHO_DEPARTMENT_ID]):
    raise RuntimeError("Missing one or more required Zoho env vars.")

app = FastAPI(title="Neurasics Support API")

# CORS – allow frontend on same origin or localhost dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this later to your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory cache (per process)
_access_token: Optional[str] = None
_access_token_expiry: float = 0.0


def get_access_token() -> str:
    """Get a valid Zoho access token using refresh token."""
    global _access_token, _access_token_expiry
    now = time.time()
    if _access_token and now < (_access_token_expiry - 60):
        return _access_token

    token_url = f"{ZOHO_ACCOUNTS_DOMAIN}/oauth/v2/token"
    data = {
        "refresh_token": ZOHO_REFRESH_TOKEN,
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    resp = requests.post(token_url, data=data)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to refresh Zoho access token: {resp.text}",
        )

    token_data = resp.json()
    access_token = token_data.get("access_token")
    expires_in = token_data.get("expires_in", 3600)
    if not access_token:
        raise HTTPException(
            status_code=500,
            detail=f"No access_token in Zoho response: {token_data}",
        )

    _access_token = access_token
    _access_token_expiry = now + int(expires_in)
    return _access_token


def create_zoho_ticket(
    subject: str,
    description: str,
    email: str,
    name: Optional[str],
    priority: str,
    category: str,
    issue_type: str,
    account_id: Optional[str],
    page_url: Optional[str],
    user_agent: Optional[str],
) -> dict:
    """Create a ticket in Zoho Desk."""
    token = get_access_token()
    url = f"{ZOHO_DESK_BASE}/api/v1/tickets"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "orgId": ZOHO_ORG_ID,
        "Content-Type": "application/json",
    }

    # Map category/type/etc. Adjust keys to match Zoho custom fields.
    custom_fields = {}
    if account_id:
        custom_fields["cf_account_id"] = account_id
    if issue_type:
        custom_fields["cf_type"] = issue_type
    if page_url:
        custom_fields["cf_page_url"] = page_url
    if user_agent:
        custom_fields["cf_user_agent"] = user_agent

    payload = {
        "subject": subject,
        "departmentId": ZOHO_DEPARTMENT_ID,
        "description": description,
        "priority": priority or "High",
        "status": "Open",
        "channel": "Web",
        "category": category,
        "contact": {
            "lastName": name or "User",
            "email": email,
        },
        "customFields": custom_fields,
    }

    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create Zoho ticket: {resp.text}",
        )

    return resp.json()


def add_attachments_to_ticket(ticket_id: str, files: List[UploadFile]):
    """Upload attachments to an existing Zoho Desk ticket."""
    if not files:
        return

    token = get_access_token()
    url = f"{ZOHO_DESK_BASE}/api/v1/tickets/{ticket_id}/attachments"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "orgId": ZOHO_ORG_ID,
        # Do not set Content-Type manually; requests will set multipart boundaries
    }

    for f in files:
        if f.filename is None:
            continue
        content = f.file.read()
        if not content:
            continue
        files_payload = {
            "file": (f.filename, content, f.content_type or "application/octet-stream")
        }
        resp = requests.post(url, headers=headers, files=files_payload)
        if resp.status_code not in (200, 201):
            # Log but don't hard-fail ticket creation
            print(f"[WARN] Failed to attach this file {f.filename}: {resp.status_code} {resp.text}")


@app.get("/", response_class=HTMLResponse)
async def serve_form():
    """Serve the support form HTML."""
    base_dir = os.path.dirname(__file__)
    file_path = os.path.join(base_dir, "templates", "support_form.html")
    if not os.path.exists(file_path):
        return HTMLResponse("<h1>support_form.html not found</h1>", status_code=500)
    return FileResponse(file_path, media_type="text/html")


@app.post("/api/support/tickets")
async def create_support_ticket(
    name: str = Form(...),
    email: EmailStr = Form(...),
    accountId: Optional[str] = Form(None),
    category: str = Form(...),
    issueType: str = Form(...),
    priority: str = Form("High"),
    subject: str = Form(...),
    description: str = Form(...),
    pageUrl: Optional[str] = Form(None),
    userAgent: Optional[str] = Form(None),
    attachments: Optional[List[UploadFile]] = File(None),
):
    try:
        ticket = create_zoho_ticket(
            subject=subject,
            description=description,
            email=email,
            name=name,
            priority=priority,
            category=category,
            issue_type=issueType,
            account_id=accountId,
            page_url=pageUrl,
            user_agent=userAgent,
        )

        ticket_id = ticket.get("id")
        ticket_number = ticket.get("ticketNumber")

        # Attach files if any
        if attachments:
            add_attachments_to_ticket(ticket_id, attachments)

        return JSONResponse(
            status_code=201,
            content={
                "success": True,
                "ticketId": ticket_id,
                "ticketNumber": ticket_number,
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        # For debugging; in production, be more careful with error details
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e),
            },
        )
