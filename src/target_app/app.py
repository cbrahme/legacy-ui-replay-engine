import asyncio
import copy
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Setup paths and templates
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(
    title="First Federal CU Core Banking Mock",
    description="Mock legacy banking portal for computer-use automation testing",
    version="4.8.2",
)

# Canonical mock banking registry
INITIAL_MEMBERS: Dict[str, Dict[str, Any]] = {
    "12345": {
        "id": "12345",
        "name": "Eleanor Vance",
        "ssn_masked": "***-**-6789",
        "join_date": "2018-04-12",
        "is_locked": False,
        "accounts": [
            {
                "name": "Standard Checking",
                "number": "CHK-4001",
                "balance": "$1,120.50",
                "ledger_balance": "$1,120.50",
                "status": "Active",
                "type_code": "checking",
            },
            {
                "name": "Regular Share Savings",
                "number": "SAV-8002",
                "balance": "$4,250.75",
                "ledger_balance": "$4,250.75",
                "status": "Active",
                "type_code": "savings",
            },
            {
                "name": "Money Market Premium",
                "number": "MM-9003",
                "balance": "$15,000.00",
                "ledger_balance": "$15,000.00",
                "status": "Active",
                "type_code": "money_market",
            },
        ],
    },
    "67890": {
        "id": "67890",
        "name": "Arthur Pendelton",
        "ssn_masked": "***-**-1122",
        "join_date": "2015-11-03",
        "is_locked": True,
        "accounts": [
            {
                "name": "Regular Share Savings",
                "number": "SAV-6003",
                "balance": "$5,400.00",
                "ledger_balance": "$5,400.00",
                "status": "Frozen",
                "type_code": "savings",
            },
            {
                "name": "Commercial Checking",
                "number": "CHK-6009",
                "balance": "$82,410.00",
                "ledger_balance": "$82,410.00",
                "status": "Frozen",
                "type_code": "checking",
            },
            {
                "name": "Escrow Reserve Account",
                "number": "ESC-7701",
                "balance": "$25,000.00",
                "ledger_balance": "$25,000.00",
                "status": "Frozen",
                "type_code": "escrow",
            },
        ],
    },
}

# Live mutable state
MEMBERS_DB: Dict[str, Dict[str, Any]] = copy.deepcopy(INITIAL_MEMBERS)


@app.get("/health")
async def health_check() -> Dict[str, str]:
    """Health check endpoint for CLI and orchestrator polling."""
    return {"status": "ok", "app": "First Federal CU Core Banking Mock", "version": "4.8.2"}


@app.post("/reset")
async def reset_database() -> Dict[str, str]:
    """Resets mock database state to initial fixture values."""
    global MEMBERS_DB
    MEMBERS_DB = copy.deepcopy(INITIAL_MEMBERS)
    return {"status": "reset_complete"}


@app.get("/", response_class=HTMLResponse)
async def root_redirect():
    """Default entry point redirects to the branch dashboard."""
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Staff dashboard displaying core modules."""
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={},
    )


@app.get("/members", response_class=HTMLResponse)
async def member_search_page(
    request: Request,
    member_id: Optional[str] = Query(None),
    not_found: Optional[int] = Query(None),
    delay: Optional[float] = Query(None),
):
    """
    Member search screen.
    Simulates transient network delay if ?delay=<seconds> is supplied.
    """
    if delay and delay > 0:
        await asyncio.sleep(min(delay, 5.0))

    error_message = None
    if not_found or (member_id and member_id not in MEMBERS_DB):
        error_message = f"Member record not found in system (ID: {member_id or '99999'})"

    return templates.TemplateResponse(
        request=request,
        name="member_search.html",
        context={
            "query_id": member_id,
            "error_message": error_message,
        },
    )


@app.post("/members/search", response_class=HTMLResponse)
async def member_search_submit(
    request: Request,
    member_id: str = Form(...),
    delay: Optional[float] = Query(None),
):
    """
    Processes the member ID lookup form.
    - If found: Redirects to /members/{id}
    - If not found (e.g. 99999): Re-renders search page with a warning alert banner.
    """
    if delay and delay > 0:
        await asyncio.sleep(min(delay, 5.0))

    clean_id = member_id.strip()
    if clean_id in MEMBERS_DB:
        return RedirectResponse(url=f"/members/{clean_id}", status_code=303)

    # Legitimate business outcome: record not found
    return templates.TemplateResponse(
        request=request,
        name="member_search.html",
        context={
            "query_id": clean_id,
            "error_message": f"Member record not found in system (ID: {clean_id})",
        },
    )


@app.get("/members/{member_id}", response_class=HTMLResponse)
async def member_detail(
    request: Request,
    member_id: str,
    delay: Optional[float] = Query(None),
):
    """Member profile view displaying account balances and security status."""
    if delay and delay > 0:
        await asyncio.sleep(min(delay, 5.0))

    clean_id = member_id.strip()
    if clean_id not in MEMBERS_DB:
        return templates.TemplateResponse(
            request=request,
            name="member_search.html",
            context={
                "query_id": clean_id,
                "error_message": f"Member record not found in system (ID: {clean_id})",
            },
            status_code=404,
        )

    member = MEMBERS_DB[clean_id]
    return templates.TemplateResponse(
        request=request,
        name="member_detail.html",
        context={
            "member": member,
        },
    )


@app.post("/members/{member_id}/override", response_class=HTMLResponse)
async def supervisor_override(member_id: str):
    """
    Supervisor clearance action used during human escalation.
    Clears the security lock on account 67890.
    """
    clean_id = member_id.strip()
    if clean_id in MEMBERS_DB:
        MEMBERS_DB[clean_id]["is_locked"] = False
        for acc in MEMBERS_DB[clean_id]["accounts"]:
            acc["status"] = "Active"
    return RedirectResponse(url=f"/members/{clean_id}", status_code=303)


@app.get("/transfers", response_class=HTMLResponse)
async def transfers_page(
    request: Request,
    from_member: Optional[str] = Query(None),
):
    """Inter-account and external funds transfer screen."""
    return templates.TemplateResponse(
        request=request,
        name="transfers.html",
        context={
            "from_member": from_member,
            "requires_confirmation": False,
        },
    )


@app.post("/transfers", response_class=HTMLResponse)
async def transfers_submit(
    request: Request,
    from_account: str = Form(...),
    to_account: str = Form(...),
    amount: str = Form(...),
):
    """Initial transfer submission presenting an irreversible confirmation step."""
    return templates.TemplateResponse(
        request=request,
        name="transfers.html",
        context={
            "from_account": from_account,
            "to_account": to_account,
            "amount": amount,
            "requires_confirmation": True,
        },
    )


@app.post("/transfers/confirm", response_class=HTMLResponse)
async def transfers_confirm(
    request: Request,
    from_account: str = Form(...),
    to_account: str = Form(...),
    amount: str = Form(...),
):
    """Commits irreversible transfer and returns reference receipt."""
    return templates.TemplateResponse(
        request=request,
        name="transfers.html",
        context={
            "requires_confirmation": False,
            "success_message": f"Transfer of ${amount} from {from_account} to {to_account} completed. Reference: TX-89211.",
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin exception review queue."""
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={},
    )


def run():
    """Entry point for direct standalone execution."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run()
