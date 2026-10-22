"""FastAPI app for Paytm Business Autopilot.

Run from the project folder (the one that contains index.html):

    python -m uvicorn backend.main:app --reload

Then open http://127.0.0.1:8000 for the app and http://127.0.0.1:8000/docs for the API.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import core, db, seed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_EXTENSIONS = {".html", ".css", ".js", ".json", ".webmanifest", ".png", ".jpg", ".jpeg", ".svg", ".ico",
                     ".woff", ".woff2", ".mp3", ".wav", ".ogg"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed.ensure_seeded()
    yield


app = FastAPI(
    title="Paytm Business Autopilot API",
    version="1.0.0",
    description="Demo backend with synthetic data. No real messages or payments are sent.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(core.ApiError)
async def api_error_handler(request: Request, exc: core.ApiError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, **exc.extra})


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": "One of the values is not valid. Check numbers and dates."})


# ---------------------------------------------------------------- request bodies

class NewCustomer(BaseModel):
    name: str
    phone: str
    source: str = "manual"
    amount: Optional[int] = None


class ApproveOffer(BaseModel):
    approved_via: str = "tap"
    language: str = "en"


class SendSelected(BaseModel):
    customer_ids: List[int]
    language: str = "en"


class UndoMessages(BaseModel):
    message_ids: List[int]


class Advance(BaseModel):
    days: int = 21


class ProfileChanges(BaseModel):
    store_name: Optional[str] = None
    owner_name: Optional[str] = None
    category: Optional[str] = None
    address: Optional[str] = None
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    day_off: Optional[str] = None
    languages: Optional[List[str]] = None
    autonomy: Optional[str] = None
    discount_cap: Optional[int] = None
    contact_limit_days: Optional[int] = None
    quiet_hours_start: Optional[str] = None


def snapshot(message: str, **extra) -> dict:
    with db.reading() as conn:
        return {"ok": True, "message": message, **extra, "state": core.frontend_state(conn)}


# ---------------------------------------------------------------- read endpoints

@app.get("/api/health", tags=["System"])
def health():
    with db.reading() as conn:
        return core.health(conn)


@app.get("/api/state", tags=["System"])
def get_state():
    """Everything the frontend needs in one call: customers, briefing, metrics, results, log, profile."""
    with db.reading() as conn:
        return core.frontend_state(conn)


@app.get("/api/briefing", tags=["Tonight"])
def get_briefing():
    with db.reading() as conn:
        b = core.compute_briefing(conn)
        b.pop("eligible_ids", None)
        return b


@app.get("/api/metrics", tags=["Tonight"])
def get_metrics():
    with db.reading() as conn:
        at = core.demo_now(conn)
        return core.compute_metrics(conn, at, core.load_customers(conn, at))


@app.get("/api/opportunities", tags=["Tonight"])
def get_opportunities():
    with db.reading() as conn:
        return core.frontend_state(conn)["live"]["opportunities"]


@app.get("/api/customers", tags=["Customers"])
def get_customers(
    segment: str = Query("all", description="all, lapsed_regular, active_regular, seasonal or new"),
    consent: str = Query("all", description="all, opted_in, pending or none"),
    q: str = Query("", description="Name, or the last 4 digits of the number"),
    sort: str = Query("days_since_last", description="days_since_last, visits, avg_bill or name"),
    order: str = Query("desc", description="asc or desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    with db.reading() as conn:
        return core.list_customers(conn, segment, consent, q, sort, order, page, page_size)


@app.get("/api/customers/{customer_id}", tags=["Customers"])
def get_customer(customer_id: int):
    with db.reading() as conn:
        return core.customer_detail(conn, customer_id)


@app.get("/api/experiments", tags=["Results"])
def get_experiments():
    with db.reading() as conn:
        rows = conn.execute("SELECT * FROM experiments ORDER BY id DESC").fetchall()
        return [core.experiment_public(conn, r) for r in rows]


@app.get("/api/experiments/{experiment_id}", tags=["Results"])
def get_experiment(experiment_id: int):
    with db.reading() as conn:
        row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        if not row:
            raise core.ApiError(404, "Offer not found.")
        return core.experiment_public(conn, row)


@app.get("/api/results/latest", tags=["Results"])
def get_latest_result():
    with db.reading() as conn:
        return core.experiment_public(conn, core.latest_completed(conn))


@app.get("/api/messages", tags=["Results"])
def get_messages(experiment_id: Optional[int] = None, group: str = "all"):
    with db.reading() as conn:
        return core.list_messages(conn, experiment_id, group)


@app.get("/api/profile", tags=["Profile"])
def get_profile():
    with db.reading() as conn:
        return core.get_profile(conn)


@app.get("/api/log", tags=["Log"])
def get_log(limit: int = Query(100, ge=1, le=500)):
    with db.reading() as conn:
        return core.get_log(conn, limit)


# ---------------------------------------------------------------- write endpoints (all return the new state)

@app.post("/api/customers", status_code=201, tags=["Customers"])
def create_customer(body: NewCustomer):
    with db.writing() as conn:
        customer_id = core.add_customer(conn, body.name, body.phone, body.source, body.amount)
    return snapshot("Customer added.", customer_id=customer_id)


@app.post("/api/customers/simulate-join", status_code=201, tags=["Customers"])
def simulate_join():
    with db.writing() as conn:
        customer_id = core.simulate_join(conn)
    return snapshot("A customer tapped Join after paying.", customer_id=customer_id)


@app.post("/api/customers/{customer_id}/confirm-consent", tags=["Customers"])
def confirm_consent(customer_id: int):
    with db.writing() as conn:
        core.confirm_consent(conn, customer_id)
    return snapshot("Customer replied YES.")


@app.delete("/api/customers/{customer_id}", tags=["Customers"])
def delete_customer(customer_id: int):
    with db.writing() as conn:
        core.remove_customer(conn, customer_id)
    return snapshot("Customer data removed.")


@app.post("/api/experiments", status_code=201, tags=["Tonight"])
def approve_offer(body: ApproveOffer):
    with db.writing() as conn:
        experiment_id = core.start_experiment(conn, body.approved_via, body.language)
    return snapshot("Offer approved.", experiment_id=experiment_id)


@app.post("/api/experiments/{experiment_id}/cancel", tags=["Tonight"])
def cancel_offer(experiment_id: int):
    with db.writing() as conn:
        core.cancel_experiment(conn, experiment_id)
    return snapshot("Offer cancelled.")


@app.post("/api/messages/send", status_code=201, tags=["Results"])
def send_selected(body: SendSelected):
    with db.writing() as conn:
        result = core.send_direct(conn, body.customer_ids, body.language)
    return snapshot("Offer sent to selected customers.", **result)


@app.post("/api/messages/undo", tags=["Results"])
def undo_messages(body: UndoMessages):
    with db.writing() as conn:
        removed = core.undo_direct(conn, body.message_ids)
    return snapshot("Messages undone.", removed=removed)


@app.patch("/api/profile", tags=["Profile"])
def patch_profile(body: ProfileChanges):
    changes = body.model_dump(exclude_none=True) if hasattr(body, "model_dump") else body.dict(exclude_none=True)
    with db.writing() as conn:
        core.update_profile(conn, changes)
    return snapshot("Profile saved.")


@app.post("/api/profile/reset-learning", tags=["Profile"])
def reset_learning():
    with db.writing() as conn:
        core.reset_learning(conn)
    return snapshot("What the agent learned was reset.")


@app.post("/api/demo/fast-forward", tags=["Demo controls"])
def fast_forward(body: Optional[Advance] = None):
    days = body.days if body is not None else 21
    with db.writing() as conn:
        result = core.advance(conn, days, "Fast-forwarded the demo")
    return snapshot(f"Moved forward {days} days.", new_payments=result["new_payments"],
                    completed=len(result["completed_experiments"]))


@app.post("/api/demo/new-week", tags=["Demo controls"])
def new_week():
    with db.writing() as conn:
        result = core.advance(conn, 7, "Started a new week")
    return snapshot("New week generated.", new_payments=result["new_payments"],
                    completed=len(result["completed_experiments"]))


@app.post("/api/demo/reset", tags=["Demo controls"])
def reset_demo():
    seed.reset()
    return snapshot("Demo reset to its starting data.")


# ---------------------------------------------------------------- frontend files

@app.get("/", include_in_schema=False)
def index():
    page = PROJECT_ROOT / "index.html"
    if page.is_file():
        return FileResponse(page, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "index.html not found next to the backend folder. The API is at /docs."})


@app.get("/{path:path}", include_in_schema=False)
def static_file(path: str):
    """Serve the frontend files from the project folder. Never serves the backend code or the database."""
    parts = Path(path).parts
    if (not path or path.startswith("api/") or any(p.startswith(".") for p in parts)
            or (parts and parts[0] in ("backend", "_backup_before_fixes", "node_modules"))):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    target = (PROJECT_ROOT / path).resolve()
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    if target.suffix.lower() not in STATIC_EXTENSIONS or not target.is_file():
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    return FileResponse(target, headers={"Cache-Control": "no-cache"})
