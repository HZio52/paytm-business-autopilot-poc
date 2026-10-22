"""Business logic for the Paytm Business Autopilot demo backend.

Pure Python (standard library only), so it can be tested without FastAPI.
Every number shown to the merchant is computed here from the payments table:
segments, eligibility, the offer prediction, measured lift, weekly metrics,
and the slow-day opportunity.

The demo has its own clock (settings.demo_now). Fast-forward and New week move
that clock and generate synthetic payments, so the whole app ages together.
"""

from __future__ import annotations

import json
import math
import random
import re
from datetime import datetime, time, timedelta
from statistics import median

from . import db

FMT = "%Y-%m-%dT%H:%M:%S"
SEED_NOW = datetime(2026, 10, 22, 20, 30)
BASE_EXPECTED_RATE = 0.30
NATURAL_RATE = 0.10
LAPSE_DAYS = 21            # same rule as the frontend: more than 21 days = lapsed
SEASONAL_GAP_DAYS = 45     # usual gap of 45+ days = seasonal visitor
CANCEL_WINDOW_MIN = 30
SLOW_DAY_RECOVERY = 0.25
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
LANGS = ("en", "hi", "kn")
BULLET = "•"
RUPEE = "₹"
TIMES = "×"
MINUS = "−"


class ApiError(Exception):
    """Raised for problems the merchant can fix. Converted to an HTTP error by main.py."""

    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


# ---------------------------------------------------------------- helpers

def parse(value: str) -> datetime:
    return datetime.strptime(value, FMT)


def fmt(value: datetime) -> str:
    return value.replace(microsecond=0).strftime(FMT)


def rnd(x: float) -> int:
    """Round half away from zero (Python's round() uses banker's rounding)."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def inr(n) -> str:
    n = rnd(n)
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{sign}{RUPEE}{s}"


def pct(rate: float) -> str:
    return f"{rnd(rate * 100)}%"


def mask_phone(phone: str) -> str:
    return f"+91 {phone[:2]}{BULLET * 3} {BULLET * 2}{phone[-3:]}"


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def time_label(value: str) -> str:
    t = parse_hhmm(value)
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {'PM' if t.hour >= 12 else 'AM'}"


def briefing_time(profile: dict) -> str:
    t = parse_hhmm(profile["closing_time"])
    minutes = (t.hour * 60 + t.minute - 30) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def date_label(value: datetime) -> str:
    return f"{value.day} {value.strftime('%b')}"


def demo_now(conn) -> datetime:
    return parse(db.get_setting(conn, "demo_now", fmt(SEED_NOW)))


def first_name(profile: dict) -> str:
    return (profile.get("owner_name") or "Meera").split()[0]


def local_names(profile: dict) -> dict:
    name = first_name(profile)
    if name.lower() == "meera":
        return {"en": name, "hi": "मीरा", "kn": "ಮೀರಾ"}
    return {"en": name, "hi": name, "kn": name}


# ---------------------------------------------------------------- profile

DEFAULT_PROFILE = {
    "store_name": "Meera Stores",
    "owner_name": "Meera Shetty",
    "category": "Grocery",
    "address": "Whitefield, Bengaluru",
    "opening_time": "08:00",
    "closing_time": "21:00",
    "day_off": "none",
    "languages": ["en", "hi", "kn"],
    "autonomy": "ask",
    "discount_cap": 20,
    "contact_limit_days": 30,
    "quiet_hours_start": "21:00",
    "expected_return_rate": 0.35,
    "natural_return_rate": NATURAL_RATE,
    "upi_masked": f"meera{BULLET * 4}@paytm",
    "bank_masked": f"HDFC {BULLET * 4} 4821",
    "soundbox_id": f"SBX-{BULLET * 4}-7731",
    "whatsapp_masked": f"+91 98{BULLET * 3} {BULLET * 2}210",
    "owner_phone_masked": f"+91 98{BULLET * 3} {BULLET * 2}210",
    "member_since": "October 2026",
    "learned": [
        {"key": "no_discount_seasonal", "text": "You prefer no discounts for seasonal customers",
         "evidence": "Learned from 2 of your replies"},
        {"key": "return_rate", "text": "Expected offer return rate raised from 30% to 35%",
         "evidence": "3 of 7 came back in the last offer"},
    ],
}

HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def get_profile(conn) -> dict:
    profile = dict(DEFAULT_PROFILE)
    profile.update(db.get_profile(conn))
    return profile


def update_profile(conn, changes: dict) -> dict:
    profile = get_profile(conn)
    at = demo_now(conn)
    clean = {}

    for key in ("store_name", "owner_name", "category", "address"):
        if changes.get(key) is not None:
            value = str(changes[key]).strip()
            if key in ("store_name", "owner_name") and not value:
                raise ApiError(422, "Store name and owner name are required.", {"field": key})
            if len(value) > 80:
                raise ApiError(422, "Keep this under 80 characters.", {"field": key})
            clean[key] = value or DEFAULT_PROFILE[key]

    for key in ("opening_time", "closing_time", "quiet_hours_start"):
        if changes.get(key) is not None:
            value = str(changes[key]).strip()
            if not HHMM_RE.match(value):
                raise ApiError(422, "Use a time like 21:00.", {"field": key})
            clean[key] = value

    opening = clean.get("opening_time", profile["opening_time"])
    closing = clean.get("closing_time", profile["closing_time"])
    if closing <= opening:
        raise ApiError(422, "Closing time must be after opening time.", {"field": "closing_time"})

    if changes.get("day_off") is not None:
        value = str(changes["day_off"]).strip()
        if value.lower() == "none":
            value = "none"
        elif value.capitalize() in WEEKDAYS:
            value = value.capitalize()
        else:
            raise ApiError(422, "Choose a day of the week or none.", {"field": "day_off"})
        clean["day_off"] = value

    if changes.get("languages") is not None:
        langs = [str(x) for x in changes["languages"]]
        if not langs or any(x not in LANGS for x in langs):
            raise ApiError(422, "Choose at least one of English, Hindi or Kannada.", {"field": "languages"})
        clean["languages"] = list(dict.fromkeys(langs))

    if changes.get("autonomy") is not None:
        if changes["autonomy"] not in ("ask", "auto"):
            raise ApiError(422, "Autonomy must be 'ask' or 'auto'.", {"field": "autonomy"})
        clean["autonomy"] = changes["autonomy"]

    if changes.get("discount_cap") is not None:
        cap = int(changes["discount_cap"])
        if not 5 <= cap <= 100:
            raise ApiError(422, f"Keep the discount between {RUPEE}5 and {RUPEE}100.", {"field": "discount_cap"})
        clean["discount_cap"] = cap

    if changes.get("contact_limit_days") is not None:
        days = int(changes["contact_limit_days"])
        if not 7 <= days <= 90:
            raise ApiError(422, "Contact limit must be between 7 and 90 days.", {"field": "contact_limit_days"})
        clean["contact_limit_days"] = days

    now_s = fmt(at)
    for key, value in clean.items():
        old = profile.get(key)
        if old == value:
            continue
        if key == "closing_time":
            profile[key] = value
            db.add_log(conn, now_s, "Merchant", f"Changed closing time to {time_label(value)}",
                       "Profile updated", f"Briefing now arrives at {time_label(briefing_time(profile))}")
        elif key == "day_off":
            db.add_log(conn, now_s, "Merchant", f"Changed weekly day off to {'none' if value == 'none' else value}",
                       "Profile updated", "Slow-day calculation updated")
        elif key == "autonomy":
            db.add_log(conn, now_s, "Merchant", "Changed autonomy setting",
                       "Ask me every time" if value == "ask" else "Send on its own within my rules", "Setting saved")
        elif key == "discount_cap":
            db.add_log(conn, now_s, "Merchant", f"Changed discount cap to {inr(value)}",
                       "Agent rules updated", "Used in the next prediction")
        elif key == "contact_limit_days":
            db.add_log(conn, now_s, "Merchant", f"Changed contact limit to once every {value} days",
                       "Agent rules updated", "Applied to eligibility")
        profile[key] = value

    other = [k for k in clean if k not in ("closing_time", "day_off", "autonomy", "discount_cap", "contact_limit_days")]
    if any(profile.get(k) != db.get_profile(conn).get(k, DEFAULT_PROFILE.get(k)) for k in other):
        db.add_log(conn, now_s, "Merchant", "Updated store details", "Profile updated", "Saved")

    db.save_profile(conn, profile)
    return profile


def reset_learning(conn) -> None:
    profile = get_profile(conn)
    profile["expected_return_rate"] = BASE_EXPECTED_RATE
    profile["learned"] = []
    db.save_profile(conn, profile)
    db.add_log(conn, fmt(demo_now(conn)), "Merchant", "Reset what the agent learned",
               f"Expected return rate back to {pct(BASE_EXPECTED_RATE)}, preferences cleared",
               "Next prediction uses the starting assumptions")


# ---------------------------------------------------------------- customers

def load_customers(conn, at: datetime) -> list:
    customers = conn.execute("SELECT * FROM customers ORDER BY id").fetchall()
    payments = conn.execute(
        "SELECT customer_id, amount, paid_at FROM payments WHERE paid_at <= ? ORDER BY paid_at, id",
        (fmt(at),),
    ).fetchall()
    by_customer = {}
    for p in payments:
        by_customer.setdefault(p["customer_id"], []).append((parse(p["paid_at"]), p["amount"]))
    return [customer_view(c, by_customer.get(c["id"], []), at) for c in customers]


def customer_view(row, history: list, at: datetime) -> dict:
    visits = len(history)
    last = history[-1][0] if history else None
    days_since = (at - last).days if last else None
    gaps = [(history[i][0] - history[i - 1][0]).days for i in range(1, visits)]
    usual_gap = median(gaps) if gaps else None
    avg_bill = rnd(sum(a for _, a in history) / visits) if visits else 0
    seasonal = visits >= 2 and usual_gap is not None and usual_gap >= SEASONAL_GAP_DAYS
    if seasonal:
        segment = "seasonal"
    elif visits <= 2:
        segment = "new"
    elif days_since is not None and days_since > LAPSE_DAYS:
        segment = "lapsed_regular"
    else:
        segment = "active_regular"
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"],
        "phone_masked": mask_phone(row["phone"]),
        "consent": row["consent"],
        "source": row["source"],
        "created_at": row["created_at"],
        "last_contacted_at": row["last_contacted_at"],
        "visits": visits,
        "last_payment_at": fmt(last) if last else None,
        "days_since_last": days_since,
        "usual_gap_days": rnd(usual_gap) if usual_gap is not None else None,
        "avg_bill": avg_bill,
        "segment": segment,
        "seasonal": seasonal,
        "history": history,
    }


SEGMENT_LABEL = {"seasonal": "Seasonal", "new": "New", "lapsed_regular": "Lapsed regular",
                 "active_regular": "Active regular"}


def block_reason(c: dict, at: datetime, profile: dict, running_ids: set):
    if c["consent"] == "none":
        return "No consent. Can't be contacted."
    if c["consent"] == "pending":
        return "Waiting for the customer to reply YES."
    if c["last_contacted_at"]:
        days = (at - parse(c["last_contacted_at"])).days
        if days < profile["contact_limit_days"]:
            return f"Contacted {days} days ago. Limit is once every {profile['contact_limit_days']} days."
    if c["id"] in running_ids:
        return "Part of an offer that is still running."
    return None


def public_customer(c: dict, at: datetime, profile: dict, running_ids: set) -> dict:
    reason = block_reason(c, at, profile, running_ids)
    if reason is None and c["seasonal"]:
        reason = "Seasonal customer. The agent skips discounts for festival-only visitors."
    return {
        "id": c["id"],
        "name": c["name"],
        "phone_masked": c["phone_masked"],
        "consent": c["consent"],
        "source": c["source"],
        "segment": c["segment"],
        "segment_label": SEGMENT_LABEL[c["segment"]],
        "visits": c["visits"],
        "last_payment_at": c["last_payment_at"],
        "days_since_last": c["days_since_last"],
        "usual_gap_days": c["usual_gap_days"],
        "avg_bill": c["avg_bill"],
        "contactable": reason is None,
        "not_contactable_reason": reason,
    }


PHONE_RE = re.compile(r"^[6-9]\d{9}$")


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not PHONE_RE.match(digits):
        raise ApiError(422, "Enter a 10-digit mobile number starting with 6, 7, 8 or 9.", {"field": "phone"})
    return digits


def add_customer(conn, name: str, phone: str, source: str = "manual", amount: int | None = None) -> int:
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 60:
        raise ApiError(422, "Enter the customer's name (2 to 60 characters).", {"field": "name"})
    if source not in ("manual", "paytm"):
        raise ApiError(422, "Source must be 'manual' or 'paytm'.", {"field": "source"})
    digits = normalize_phone(phone)
    amount = 500 if amount is None else int(amount)
    if not 1 <= amount <= 100000:
        raise ApiError(422, f"Bill amount must be between {RUPEE}1 and {RUPEE}1,00,000.", {"field": "amount"})
    existing = conn.execute("SELECT id, name FROM customers WHERE phone = ?", (digits,)).fetchone()
    if existing:
        raise ApiError(409, f"{existing['name']} is already saved with this number.",
                       {"customer_id": existing["id"], "field": "phone"})
    at = demo_now(conn)
    consent = "opted_in" if source == "paytm" else "pending"
    cur = conn.execute(
        "INSERT INTO customers (name, phone, consent, source, created_at, sim_gap_days, sim_basket, sim_active) "
        "VALUES (?, ?, ?, ?, ?, 7, ?, 1)",
        (name, digits, consent, source, fmt(at), amount),
    )
    customer_id = cur.lastrowid
    conn.execute("INSERT INTO payments (customer_id, amount, paid_at) VALUES (?, ?, ?)",
                 (customer_id, amount, fmt(at)))
    if source == "paytm":
        db.add_log(conn, fmt(at), "System", "Customer joined after paying",
                   f"{name} tapped Join on the Paytm payment screen", "Can receive approved offers")
    else:
        db.add_log(conn, fmt(at), "Merchant", "Added a customer",
                   f"{name} added at the counter", "Waiting for the customer to reply YES")
    return customer_id


SPARE_NAMES = ["Farhan Ali", "Bhavana Rao", "Chetan Kulkarni", "Revathi Iyer", "Naveen Gowda", "Sneha Hegde",
               "Ajay Nair", "Pallavi Joshi", "Yusuf Sheikh", "Geetha Murthy", "Dinesh Shetty", "Aarti Kamath"]


def simulate_join(conn) -> int:
    at = demo_now(conn)
    rng = random.Random(f"join-{fmt(at)}-{conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]}")
    used = {r["name"] for r in conn.execute("SELECT name FROM customers")}
    choices = [n for n in SPARE_NAMES if n not in used] or [f"New customer {len(used) + 1}"]
    name = choices[0]
    while True:
        phone = f"{rng.choice('9876')}{rng.randint(0, 999999999):09d}"
        if not conn.execute("SELECT 1 FROM customers WHERE phone = ?", (phone,)).fetchone():
            break
    return add_customer(conn, name, phone, source="paytm", amount=rng.randrange(200, 800, 10))


def confirm_consent(conn, customer_id: int) -> None:
    row = conn.execute("SELECT name, consent FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not row:
        raise ApiError(404, "Customer not found.")
    if row["consent"] != "pending":
        raise ApiError(409, f"{row['name']} is not waiting for consent.")
    conn.execute("UPDATE customers SET consent = 'opted_in' WHERE id = ?", (customer_id,))
    db.add_log(conn, fmt(demo_now(conn)), "System", "Customer replied YES",
               f"{row['name']} agreed to receive offers", "Can receive approved offers")


def remove_customer(conn, customer_id: int) -> None:
    row = conn.execute("SELECT name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not row:
        raise ApiError(404, "Customer not found.")
    conn.execute("UPDATE messages SET customer_name = 'Removed customer', phone_masked = '' WHERE customer_id = ?",
                 (customer_id,))
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    db.add_log(conn, fmt(demo_now(conn)), "Merchant", "Removed a customer's data",
               "Name, number and payment history deleted", "Past results keep anonymous counts only")


def list_customers(conn, segment="all", consent="all", q="", sort="days_since_last", order="desc",
                   page=1, page_size=25) -> dict:
    at = demo_now(conn)
    profile = get_profile(conn)
    running_ids = running_customer_ids(conn)
    allc = load_customers(conn, at)
    counts = {"all": len(allc)}
    for key in SEGMENT_LABEL:
        counts[key] = sum(1 for c in allc if c["segment"] == key)
    rows = allc
    if segment != "all":
        if segment not in SEGMENT_LABEL:
            raise ApiError(422, "Unknown segment.")
        rows = [c for c in rows if c["segment"] == segment]
    if consent != "all":
        if consent not in ("opted_in", "pending", "none"):
            raise ApiError(422, "Unknown consent state.")
        rows = [c for c in rows if c["consent"] == consent]
    q = (q or "").strip().lower()
    if q:
        if q.isdigit():
            rows = [c for c in rows if len(q) <= 4 and c["phone"].endswith(q)]
        else:
            rows = [c for c in rows if q in c["name"].lower()]
    keys = {
        "days_since_last": lambda c: (c["days_since_last"] is None, c["days_since_last"] or 0),
        "visits": lambda c: c["visits"],
        "avg_bill": lambda c: c["avg_bill"],
        "name": lambda c: c["name"].lower(),
    }
    if sort not in keys:
        raise ApiError(422, "Sort by days_since_last, visits, avg_bill or name.")
    rows = sorted(rows, key=keys[sort], reverse=(order == "desc"))
    page_size = max(1, min(int(page_size), 200))
    total = len(rows)
    pages = max(1, math.ceil(total / page_size))
    page = max(1, min(int(page), pages))
    items = rows[(page - 1) * page_size: page * page_size]
    return {
        "items": [public_customer(c, at, profile, running_ids) for c in items],
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "counts": counts,
    }


def customer_detail(conn, customer_id: int) -> dict:
    at = demo_now(conn)
    profile = get_profile(conn)
    c = next((x for x in load_customers(conn, at) if x["id"] == customer_id), None)
    if not c:
        raise ApiError(404, "Customer not found.")
    out = public_customer(c, at, profile, running_customer_ids(conn))
    out["payments"] = [{"paid_at": fmt(t), "amount": a} for t, a in reversed(c["history"])][:30]
    out["messages"] = [message_public(m) for m in conn.execute(
        "SELECT * FROM messages WHERE customer_id = ? ORDER BY id DESC", (customer_id,))]
    return out


# ---------------------------------------------------------------- experiments

def running_experiment(conn):
    return conn.execute("SELECT * FROM experiments WHERE status = 'running' ORDER BY id DESC LIMIT 1").fetchone()


def latest_completed(conn):
    return conn.execute("SELECT * FROM experiments WHERE status = 'completed' ORDER BY id DESC LIMIT 1").fetchone()


def running_customer_ids(conn) -> set:
    rows = conn.execute(
        "SELECT m.customer_id FROM messages m JOIN experiments e ON e.id = m.experiment_id "
        "WHERE e.status = 'running' AND m.customer_id IS NOT NULL"
    ).fetchall()
    return {r["customer_id"] for r in rows}


def compute_briefing(conn, at=None, customers=None, profile=None) -> dict:
    at = at or demo_now(conn)
    profile = profile or get_profile(conn)
    customers = customers if customers is not None else load_customers(conn, at)
    running = running_experiment(conn)
    running_ids = running_customer_ids(conn)

    lapsed = [c for c in customers if c["segment"] == "lapsed_regular"]
    eligible, blocked = [], {"no_consent": 0, "pending": 0, "contacted_recently": 0, "in_running_offer": 0}
    for c in lapsed:
        reason = block_reason(c, at, profile, running_ids)
        if reason is None:
            eligible.append(c)
        elif c["consent"] == "none":
            blocked["no_consent"] += 1
        elif c["consent"] == "pending":
            blocked["pending"] += 1
        elif c["id"] in running_ids:
            blocked["in_running_offer"] += 1
        else:
            blocked["contacted_recently"] += 1
    skipped_seasonal = [c for c in customers if c["segment"] == "seasonal" and c["consent"] == "opted_in"
                        and c["days_since_last"] is not None and c["days_since_last"] > LAPSE_DAYS]

    eligible.sort(key=lambda c: (-(c["days_since_last"] or 0), c["id"]))
    n = len(eligible)
    offer_n = n // 2
    control_n = n - offer_n
    cap = int(profile["discount_cap"])
    r_o = float(profile["expected_return_rate"])
    r_n = float(profile["natural_return_rate"])
    basket = rnd(sum(c["avg_bill"] for c in eligible) / n / 10) * 10 if n else 0
    gross = rnd(offer_n * (r_o - r_n) * basket)
    discount = rnd(offer_n * r_o * cap)
    net = gross - discount

    if running:
        status = "running"
    elif offer_n >= 1 and control_n >= 1:
        status = "awaiting_approval"
    else:
        status = "nothing_to_do"

    formula = (f"{offer_n} {TIMES} ({pct(r_o)} {MINUS} {pct(r_n)}) {TIMES} {inr(basket)} "
               f"{MINUS} {inr(discount)} discount = {inr(net)} net")
    texts = briefing_texts(profile, status, n, offer_n, control_n, cap, running)
    return {
        "status": status,
        "briefing_time": briefing_time(profile),
        "briefing_time_label": time_label(briefing_time(profile)),
        "lapsed_count": len(lapsed),
        "eligible_count": n,
        "offer_count": offer_n,
        "control_count": control_n,
        "basket": basket,
        "offer_rate": r_o,
        "natural_rate": r_n,
        "discount_per_customer": cap,
        "discount_total": discount,
        "gross_lift": gross,
        "predicted_net": net,
        "formula": formula,
        "rate_source": "Rates come from this store's own past offers and natural returns.",
        "blocked": blocked,
        "skipped_seasonal_count": len(skipped_seasonal),
        "skipped_seasonal_names": [c["name"] for c in skipped_seasonal],
        "eligible_ids": [c["id"] for c in eligible],
        "eligible_names": [c["name"] for c in eligible],
        "eligible_preview": [{"id": c["id"], "name": c["name"], "usual_gap_days": c["usual_gap_days"],
                              "days_since_last": c["days_since_last"]} for c in eligible[:20]],
        "quote": texts["quote"],
        "speech": texts["speech"],
    }


def briefing_texts(profile, status, n, offer_n, control_n, cap, running) -> dict:
    nm = local_names(profile)
    if status == "running" and running:
        report = date_label(parse(running["created_at"]) + timedelta(days=running["measure_after_days"]))
        quote = {
            "en": f"{nm['en']}, the offer is out. I will report the real result on {report}.",
            "hi": f"{nm['hi']} जी, ऑफ़र भेज दिया गया है। असली नतीजा {report} को बताऊँगी।",
            "kn": f"{nm['kn']} ಅವರೇ, ಆಫರ್ ಕಳುಹಿಸಲಾಗಿದೆ. ನಿಜವಾದ ಫಲಿತಾಂಶವನ್ನು {report} ರಂದು ತಿಳಿಸುತ್ತೇನೆ.",
        }
        return {"quote": quote, "speech": dict(quote)}
    if status == "nothing_to_do":
        quote = {
            "en": f"{nm['en']}, no regular customer needs an offer tonight. I will keep watching your payments.",
            "hi": f"{nm['hi']} जी, आज रात किसी नियमित ग्राहक को ऑफ़र की ज़रूरत नहीं है।",
            "kn": f"{nm['kn']} ಅವರೇ, ಇಂದು ರಾತ್ರಿ ಯಾವ ನಿಯಮಿತ ಗ್ರಾಹಕರಿಗೂ ಆಫರ್ ಅಗತ್ಯವಿಲ್ಲ.",
        }
        return {"quote": quote, "speech": dict(quote)}
    quote = {
        "en": (f"{nm['en']}, {n} regular customers have not returned in 3 weeks. Should I send a {RUPEE}{cap} "
               f"offer to {offer_n} of them and hold back {control_n} to measure fairly?"),
        "hi": (f"{nm['hi']} जी, {n} नियमित ग्राहक 3 हफ़्तों से वापस नहीं आए हैं। क्या मैं {offer_n} ग्राहकों को "
               f"{RUPEE}{cap} की छूट भेजूँ और सही तुलना के लिए {control_n} को छोड़ दूँ?"),
        "kn": (f"{nm['kn']} ಅವರೇ, {n} ನಿಯಮಿತ ಗ್ರಾಹಕರು 3 ವಾರಗಳಿಂದ ಮರಳಿ ಬಂದಿಲ್ಲ. {offer_n} ಗ್ರಾಹಕರಿಗೆ "
               f"{RUPEE}{cap} ರಿಯಾಯಿತಿ ಕಳುಹಿಸಿ, ಸರಿಯಾದ ಹೋಲಿಕೆಗಾಗಿ {control_n} ಜನರನ್ನು ಬಿಡಲೇ?"),
    }
    speech = {
        "en": (f"{nm['en']}, {n} regular customers have not returned in three weeks. Should I send a {cap} rupee "
               f"offer to {offer_n} of them, and hold back {control_n} to measure fairly? Say yes to approve."),
        "hi": (f"{nm['hi']} जी, {n} नियमित ग्राहक तीन हफ़्तों से वापस नहीं आए हैं। क्या मैं {offer_n} ग्राहकों को "
               f"{cap} रुपये की छूट भेजूँ, और सही तुलना के लिए {control_n} को छोड़ दूँ? मंज़ूरी के लिए हां बोलिए।"),
        "kn": (f"{nm['kn']} ಅವರೇ, {n} ನಿಯಮಿತ ಗ್ರಾಹಕರು ಮೂರು ವಾರಗಳಿಂದ ಮರಳಿ ಬಂದಿಲ್ಲ. {offer_n} ಗ್ರಾಹಕರಿಗೆ {cap} ರೂಪಾಯಿ "
               f"ರಿಯಾಯಿತಿ ಕಳುಹಿಸಿ, ಸರಿಯಾದ ಹೋಲಿಕೆಗಾಗಿ {control_n} ಜನರನ್ನು ಬಿಡಲೇ? ಅನುಮೋದಿಸಲು ಹೌದು ಎಂದು ಹೇಳಿ."),
    }
    return {"quote": quote, "speech": speech}


def customer_message(lang: str, first: str, store: str, cap: int, valid_until: datetime) -> str:
    until = date_label(valid_until)
    if lang == "hi":
        return (f"{first} जी, हमें आपकी याद आई। {until} तक {store} पर अगली ख़रीद पर {RUPEE}{cap} की छूट पाइए। "
                f"ऑफ़र एक बार के लिए है। ऑफ़र बंद करने के लिए STOP लिखें।")
    if lang == "kn":
        return (f"{first} ಅವರೇ, ನಿಮ್ಮನ್ನು ಮಿಸ್ ಮಾಡಿಕೊಂಡೆವು. {until} ಒಳಗೆ {store} ನಲ್ಲಿ ಮುಂದಿನ ಖರೀದಿಗೆ {RUPEE}{cap} "
                f"ರಿಯಾಯಿತಿ ಪಡೆಯಿರಿ. ಒಂದು ಬಾರಿ ಮಾತ್ರ. ಆಫರ್ ನಿಲ್ಲಿಸಲು STOP ಎಂದು ಉತ್ತರಿಸಿ.")
    return (f"We miss you, {first}. Come back by {until} and get {RUPEE}{cap} off your next purchase at {store}. "
            f"One use only. Reply STOP to stop these offers.")


def send_time(at: datetime, profile: dict):
    quiet = parse_hhmm(profile["quiet_hours_start"])
    morning = time(9, 0)
    if morning <= at.time() < quiet:
        return at, "sent"
    next_day = at.date() + timedelta(days=1) if at.time() >= quiet else at.date()
    return datetime.combine(next_day, morning), "queued"


def start_experiment(conn, approved_via: str = "tap", language: str = "en") -> int:
    if approved_via not in ("tap", "voice", "autopilot"):
        raise ApiError(422, "approved_via must be tap, voice or autopilot.")
    if language not in LANGS:
        raise ApiError(422, "language must be en, hi or kn.")
    at = demo_now(conn)
    profile = get_profile(conn)
    customers = load_customers(conn, at)
    b = compute_briefing(conn, at, customers, profile)
    if b["status"] == "running":
        raise ApiError(409, "An offer is already running. Wait for its result or cancel it first.")
    if b["status"] != "awaiting_approval":
        raise ApiError(409, "There are not enough eligible customers for a fair offer tonight.")

    by_id = {c["id"]: c for c in customers}
    ids = list(b["eligible_ids"])
    random.Random(f"split-{fmt(at)}-{len(ids)}").shuffle(ids)
    offer_ids, control_ids = ids[:b["offer_count"]], ids[b["offer_count"]:]
    when, status = send_time(at, profile)
    cap = int(profile["discount_cap"])
    cur = conn.execute(
        "INSERT INTO experiments (created_at, status, offer_amount, basket, expected_rate, natural_rate, "
        "predicted_net, measure_after_days, approved_via) VALUES (?, 'running', ?, ?, ?, ?, ?, 7, ?)",
        (fmt(at), cap, b["basket"], b["offer_rate"], b["natural_rate"], b["predicted_net"], approved_via),
    )
    exp_id = cur.lastrowid
    for cid in offer_ids:
        c = by_id[cid]
        text = customer_message(language, c["name"].split()[0], profile["store_name"], cap, when + timedelta(days=5))
        conn.execute(
            "INSERT INTO messages (experiment_id, customer_id, customer_name, phone_masked, grp, language, text, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'offer', ?, ?, ?, ?, ?)",
            (exp_id, cid, c["name"], c["phone_masked"], language, text, status, fmt(when), fmt(when)),
        )
        conn.execute("UPDATE customers SET last_contacted_at = ? WHERE id = ?", (fmt(at), cid))
    for cid in control_ids:
        c = by_id[cid]
        conn.execute(
            "INSERT INTO messages (experiment_id, customer_id, customer_name, phone_masked, grp, language, text, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'control', ?, NULL, 'held_back', ?, ?)",
            (exp_id, cid, c["name"], c["phone_masked"], language, fmt(at), fmt(at)),
        )
    via = {"tap": "by tap", "voice": "by voice", "autopilot": "on autopilot within your rules"}[approved_via]
    who = "Agent" if approved_via == "autopilot" else "Merchant"
    db.add_log(conn, fmt(at), who, f"Approved the offer {via}",
               f"{len(offer_ids)} offer, {len(control_ids)} held back, consent checked",
               f"Predicted {inr(b['predicted_net'])} net")
    report = date_label(at + timedelta(days=7))
    if status == "sent":
        db.add_log(conn, fmt(at), "Agent", f"Sent the offer to {len(offer_ids)} customers",
                   f"{len(control_ids)} customers not messaged, to compare fairly", f"Result on {report}")
    else:
        db.add_log(conn, fmt(at), "Agent", f"Queued the offer for {len(offer_ids)} customers",
                   f"Quiet hours: messages go out at 9:00 AM on {date_label(when)}", f"Result on {report}")
    return exp_id


def cancel_experiment(conn, exp_id: int) -> None:
    exp = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    if not exp:
        raise ApiError(404, "Offer not found.")
    if exp["status"] != "running":
        raise ApiError(409, "Only a running offer can be cancelled.")
    at = demo_now(conn)
    if at - parse(exp["created_at"]) > timedelta(minutes=CANCEL_WINDOW_MIN):
        raise ApiError(409, "This offer can no longer be cancelled. The messages were already delivered.")
    conn.execute("UPDATE experiments SET status = 'cancelled' WHERE id = ?", (exp_id,))
    conn.execute("UPDATE messages SET status = 'cancelled', updated_at = ? WHERE experiment_id = ?",
                 (fmt(at), exp_id))
    conn.execute(
        "UPDATE customers SET last_contacted_at = NULL WHERE last_contacted_at = ? AND id IN "
        "(SELECT customer_id FROM messages WHERE experiment_id = ? AND grp = 'offer')",
        (exp["created_at"], exp_id),
    )
    db.add_log(conn, fmt(at), "Merchant", "Cancelled the offer", "No customer was charged or messaged",
               "Briefing is ready again")


def measure(conn, exp) -> dict:
    msgs = conn.execute("SELECT * FROM messages WHERE experiment_id = ?", (exp["id"],)).fetchall()
    offer = [m for m in msgs if m["grp"] == "offer" and m["status"] != "cancelled"]
    control = [m for m in msgs if m["grp"] == "control" and m["status"] != "cancelled"]
    k_o = sum(1 for m in offer if m["returned_at"])
    k_c = sum(1 for m in control if m["returned_at"])
    revenue = sum((m["return_amount"] or exp["basket"]) for m in offer if m["returned_at"])
    control_rate = k_c / len(control) if control else 0.0
    baseline = rnd(control_rate * len(offer) * exp["basket"])
    discount = k_o * exp["offer_amount"]
    net = revenue - baseline - discount
    return {
        "experiment_id": exp["id"],
        "offer_count": len(offer),
        "control_count": len(control),
        "offer_returned": k_o,
        "control_returned": k_c,
        "offer_revenue": revenue,
        "natural_baseline": baseline,
        "discount_cost": discount,
        "net_incremental": net,
        "predicted_net": exp["predicted_net"],
        "beat_prediction": net > exp["predicted_net"],
        "formula": (f"{inr(revenue)} offer revenue {MINUS} {inr(baseline)} natural baseline "
                    f"{MINUS} {inr(discount)} discount = {inr(net)} net"),
        "baseline_explained": (f"{k_c} of {len(control)} held-back customers came back on their own, so "
                               f"{k_c}/{len(control)} {TIMES} {len(offer)} {TIMES} {inr(exp['basket'])} "
                               f"= {inr(baseline)} would have come anyway."),
    }


def apply_learning(conn, k_o: int, n_o: int, at: datetime) -> None:
    if n_o == 0:
        return
    profile = get_profile(conn)
    old = round(float(profile["expected_return_rate"]), 2)
    observed = k_o / n_o
    if observed > old + 0.005:
        new = min(round(observed, 2), round(old + 0.05, 2))
    elif observed < old - 0.005:
        new = max(round(observed, 2), round(old - 0.05, 2), round(float(profile["natural_return_rate"]) + 0.05, 2))
    else:
        return
    if new == old:
        return
    word = "raised" if new > old else "lowered"
    profile["expected_return_rate"] = new
    learned = [x for x in profile.get("learned", []) if x.get("key") != "return_rate"]
    learned.append({"key": "return_rate", "text": f"Expected offer return rate {word} from {pct(old)} to {pct(new)}",
                    "evidence": f"{k_o} of {n_o} came back in the last offer"})
    profile["learned"] = learned
    db.save_profile(conn, profile)
    db.add_log(conn, fmt(at), "Agent", "Updated what I expect",
               f"Expected offer return rate {word} from {pct(old)} to {pct(new)}", "Used in the next prediction")


def complete_experiment(conn, exp, rng: random.Random) -> dict:
    created = parse(exp["created_at"])
    days = exp["measure_after_days"]
    msgs = conn.execute("SELECT * FROM messages WHERE experiment_id = ? ORDER BY id", (exp["id"],)).fetchall()
    offer = [m for m in msgs if m["grp"] == "offer" and m["customer_id"] is not None]
    control = [m for m in msgs if m["grp"] == "control" and m["customer_id"] is not None]
    k_o = min(len(offer), rnd(len(offer) * exp["expected_rate"]))
    k_c = min(len(control), rnd(len(control) * exp["natural_rate"]))
    offer_back = {m["id"] for m in rng.sample(offer, k_o)}
    control_back = {m["id"] for m in rng.sample(control, k_c)}

    def record_return(m):
        cust = conn.execute("SELECT sim_basket FROM customers WHERE id = ?", (m["customer_id"],)).fetchone()
        amount = rnd((cust["sim_basket"] if cust else exp["basket"]) / 10) * 10
        when = (created + timedelta(days=rng.randint(1, days - 1))).replace(hour=rng.randint(10, 19), minute=0, second=0)
        conn.execute("INSERT INTO payments (customer_id, amount, paid_at) VALUES (?, ?, ?)",
                     (m["customer_id"], amount, fmt(when)))
        conn.execute("UPDATE customers SET sim_active = 1 WHERE id = ?", (m["customer_id"],))
        conn.execute("UPDATE messages SET status = 'returned', returned_at = ?, return_amount = ?, updated_at = ? "
                     "WHERE id = ?", (fmt(when), amount, fmt(when), m["id"]))

    for m in offer:
        if m["id"] in offer_back:
            record_return(m)
        else:
            status = "read" if rng.random() < 0.6 else "delivered"
            conn.execute("UPDATE messages SET status = ?, updated_at = ? WHERE id = ?",
                         (status, fmt(created + timedelta(hours=2)), m["id"]))
    for m in control:
        if m["id"] in control_back:
            record_return(m)

    end = created + timedelta(days=days)
    result = measure(conn, exp)
    conn.execute("UPDATE experiments SET status = 'completed', completed_at = ?, result = ? WHERE id = ?",
                 (fmt(end), json.dumps(result), exp["id"]))
    db.add_log(conn, fmt(end), "Agent", "Offer result is in",
               f"{result['offer_returned']} of {result['offer_count']} offer vs "
               f"{result['control_returned']} of {result['control_count']} held back",
               f"{inr(result['net_incremental'])} net incremental")
    apply_learning(conn, result["offer_returned"], result["offer_count"], end)
    return result


# ---------------------------------------------------------------- direct sends

def send_direct(conn, customer_ids: list, language: str = "en") -> dict:
    if language not in LANGS:
        raise ApiError(422, "language must be en, hi or kn.")
    if not customer_ids:
        raise ApiError(422, "Select at least one customer.")
    at = demo_now(conn)
    profile = get_profile(conn)
    by_id = {c["id"]: c for c in load_customers(conn, at)}
    running_ids = running_customer_ids(conn)
    when, status = send_time(at, profile)
    cap = int(profile["discount_cap"])
    sent, skipped = [], []
    for cid in dict.fromkeys(int(x) for x in customer_ids):
        c = by_id.get(cid)
        if not c:
            skipped.append({"id": cid, "reason": "Customer not found."})
            continue
        reason = block_reason(c, at, profile, running_ids)
        if reason is None and c["seasonal"]:
            reason = "Seasonal customer. The agent skips discounts for festival-only visitors."
        if reason:
            skipped.append({"id": cid, "name": c["name"], "reason": reason})
            continue
        text = customer_message(language, c["name"].split()[0], profile["store_name"], cap, when + timedelta(days=5))
        cur = conn.execute(
            "INSERT INTO messages (experiment_id, customer_id, customer_name, phone_masked, grp, language, text, "
            "status, created_at, updated_at) VALUES (NULL, ?, ?, ?, 'direct', ?, ?, ?, ?, ?)",
            (cid, c["name"], c["phone_masked"], language, text, status, fmt(when), fmt(when)),
        )
        conn.execute("UPDATE customers SET last_contacted_at = ? WHERE id = ?", (fmt(at), cid))
        sent.append(cur.lastrowid)
    if not sent:
        raise ApiError(409, "None of the selected customers can be contacted right now.", {"skipped": skipped})
    db.add_log(conn, fmt(at), "Merchant", f"Sent the offer to {len(sent)} selected customers",
               f"{len(skipped)} skipped by your rules" if skipped else "All selected customers had consent",
               f"Undo available for {CANCEL_WINDOW_MIN} minutes")
    return {"message_ids": sent, "skipped": skipped, "status": status}


def undo_direct(conn, message_ids: list) -> int:
    at = demo_now(conn)
    removed = 0
    for mid in dict.fromkeys(int(x) for x in message_ids):
        m = conn.execute("SELECT * FROM messages WHERE id = ? AND grp = 'direct'", (mid,)).fetchone()
        if not m or m["status"] not in ("sent", "queued"):
            continue
        if at - parse(m["created_at"]) > timedelta(minutes=CANCEL_WINDOW_MIN) and m["status"] != "queued":
            continue
        conn.execute("DELETE FROM messages WHERE id = ?", (mid,))
        if m["customer_id"]:
            conn.execute("UPDATE customers SET last_contacted_at = NULL WHERE id = ?", (m["customer_id"],))
        removed += 1
    if not removed:
        raise ApiError(409, "These messages can no longer be undone.")
    db.add_log(conn, fmt(at), "Merchant", "Undid sent messages", f"{removed} messages removed", "Nothing was delivered")
    return removed


# ---------------------------------------------------------------- demo clock

def _nudge(dt: datetime, rng: random.Random) -> datetime:
    """Synthetic shopping pattern: Tuesdays are quieter, some visits move to Wednesday."""
    if dt.weekday() == 1 and rng.random() < 0.35:
        return dt + timedelta(days=1)
    return dt


def advance(conn, days: int, reason: str = "Fast-forwarded the demo") -> dict:
    days = int(days)
    if not 1 <= days <= 90:
        raise ApiError(422, "Move the clock forward between 1 and 90 days.")
    start = demo_now(conn)
    end = start + timedelta(days=days)
    profile = get_profile(conn)
    rng = random.Random(f"advance-{fmt(start)}-{days}")
    day_off = profile.get("day_off", "none")

    new_payments = 0
    for c in conn.execute("SELECT id, sim_gap_days, sim_basket FROM customers WHERE sim_active = 1").fetchall():
        last = conn.execute("SELECT MAX(paid_at) AS m FROM payments WHERE customer_id = ?", (c["id"],)).fetchone()["m"]
        t = parse(last) if last else start
        gap = max(1, int(c["sim_gap_days"]))
        while True:
            t = t + timedelta(days=max(1, gap + rng.randint(-1, 1)))
            if t.date() > end.date():
                break
            visit = _nudge(t, rng).replace(hour=rng.randint(9, 19), minute=rng.choice((0, 15, 30, 45)), second=0)
            if day_off != "none" and WEEKDAYS[visit.weekday()] == day_off:
                visit += timedelta(days=1)
            if start < visit <= end:
                amount = max(50, rnd(c["sim_basket"] * rng.uniform(0.85, 1.15) / 10) * 10)
                conn.execute("INSERT INTO payments (customer_id, amount, paid_at) VALUES (?, ?, ?)",
                             (c["id"], amount, fmt(visit)))
                new_payments += 1

    completed = []
    for exp in conn.execute("SELECT * FROM experiments WHERE status = 'running' ORDER BY id").fetchall():
        if parse(exp["created_at"]) + timedelta(days=exp["measure_after_days"]) <= end:
            completed.append(complete_experiment(conn, exp, rng))
        else:
            conn.execute("UPDATE messages SET status = 'delivered', updated_at = ? WHERE experiment_id = ? "
                         "AND grp = 'offer' AND status IN ('sent', 'queued')", (fmt(end), exp["id"]))
    conn.execute("UPDATE messages SET status = 'delivered', updated_at = ? WHERE grp = 'direct' "
                 "AND status IN ('sent', 'queued')", (fmt(end),))

    db.set_setting(conn, "demo_now", fmt(end))
    db.set_setting(conn, "days_advanced", int(db.get_setting(conn, "days_advanced", "0")) + days)
    db.add_log(conn, fmt(end), "Merchant", reason, f"Demo clock moved forward {days} days",
               f"{new_payments} new synthetic payments")

    b = compute_briefing(conn, end)
    if b["status"] == "awaiting_approval" and get_profile(conn)["autonomy"] == "auto":
        start_experiment(conn, approved_via="autopilot")
    elif b["status"] == "awaiting_approval":
        db.add_log(conn, fmt(end), "Agent", "Prepared tonight's briefing",
                   f"{b['eligible_count']} customers eligible, {b['offer_count']} offer and {b['control_count']} held back",
                   f"Predicted {inr(b['predicted_net'])} net. Waiting for your answer")
    return {"days": days, "new_payments": new_payments, "completed_experiments": completed}


# ---------------------------------------------------------------- metrics & opportunities

def payments_between(conn, start: datetime, end: datetime):
    return conn.execute("SELECT customer_id, amount, paid_at FROM payments WHERE paid_at > ? AND paid_at <= ?",
                        (fmt(start), fmt(end))).fetchall()


def compute_metrics(conn, at: datetime, customers: list) -> dict:
    week = payments_between(conn, at - timedelta(days=7), at)
    prev = payments_between(conn, at - timedelta(days=14), at - timedelta(days=7))
    rev_week = sum(p["amount"] for p in week)
    rev_prev = sum(p["amount"] for p in prev)
    change = round((rev_week - rev_prev) / rev_prev * 100, 1) if rev_prev else None

    def repeat_rate(start, end):
        counts = {}
        for p in payments_between(conn, start, end):
            counts[p["customer_id"]] = counts.get(p["customer_id"], 0) + 1
        return round(sum(1 for v in counts.values() if v >= 2) / len(counts) * 100, 1) if counts else 0.0

    rep_now = repeat_rate(at - timedelta(days=30), at)
    rep_before = repeat_rate(at - timedelta(days=60), at - timedelta(days=30))
    total = len(customers)
    opted = sum(1 for c in customers if c["consent"] == "opted_in")
    return {
        "revenue_week": rev_week,
        "revenue_prev_week": rev_prev,
        "revenue_change_pct": change,
        "payments_week": len(week),
        "repeat_rate_pct": rep_now,
        "repeat_change_pts": round(rep_now - rep_before, 1),
        "consent_pct": rnd(opted / total * 100) if total else 0,
        "opted_in_count": opted,
        "pending_count": sum(1 for c in customers if c["consent"] == "pending"),
        "total_customers": total,
    }


def weekday_averages(conn, at: datetime, weeks: int = 8) -> list:
    totals = [0] * 7
    for p in payments_between(conn, at - timedelta(days=7 * weeks), at):
        totals[parse(p["paid_at"]).weekday()] += p["amount"]
    return [t / weeks for t in totals]


def slow_day(conn, at: datetime, profile: dict, basket: int) -> dict | None:
    avgs = weekday_averages(conn, at)
    day_off = profile.get("day_off", "none")
    days = [i for i in range(7) if not (day_off != "none" and WEEKDAYS[i] == day_off)]
    if len(days) < 2 or not any(avgs[i] for i in days):
        return None
    slow_i = min(days, key=lambda i: avgs[i])
    busy_i = max(days, key=lambda i: avgs[i])
    usual = median([avgs[i] for i in days if i != slow_i])
    gap = usual - avgs[slow_i]
    if gap <= 0:
        return None
    basket = basket or 500
    gross = rnd(gap * SLOW_DAY_RECOVERY)
    discount = rnd(gross / basket * int(profile["discount_cap"]))
    net = gross - discount
    return {
        "day": WEEKDAYS[slow_i],
        "busiest_day": WEEKDAYS[busy_i],
        "slow_day_average": rnd(avgs[slow_i]),
        "usual_day_average": rnd(usual),
        "pct_below_usual": rnd(gap / usual * 100),
        "predicted_net_per_week": net,
        "formula": (f"({inr(usual)} usual day {MINUS} {inr(avgs[slow_i])} {WEEKDAYS[slow_i]}) {TIMES} 25% recovered "
                    f"{MINUS} {inr(discount)} discount = {inr(net)} a week"),
        "assumption": "Assumes a slow-day offer wins back a quarter of the gap. Not measured yet.",
        "excluded_day_off": None if day_off == "none" else day_off,
    }


def payment_issues(conn) -> dict:
    open_count = conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'open'").fetchone()[0]
    minutes = open_count * 15
    return {
        "open_tickets": open_count,
        "kind": "Duplicate debit questions",
        "time_saved_hours": round(minutes / 60, 1),
        "formula": f"{open_count} questions {TIMES} 15 minutes each = {round(minutes / 60, 1)} hours saved",
    }


# ---------------------------------------------------------------- messages & results

STATUS_LABEL = {"queued": "Queued", "sent": "Sent", "delivered": "Delivered", "read": "Read",
                "returned": "Returned", "held_back": "Not messaged", "cancelled": "Cancelled"}


def message_public(m) -> dict:
    return {
        "id": m["id"],
        "experiment_id": m["experiment_id"],
        "customer_id": m["customer_id"],
        "customer_name": m["customer_name"],
        "phone_masked": m["phone_masked"],
        "group": m["grp"],
        "language": m["language"],
        "text": m["text"],
        "status": m["status"],
        "status_label": "Came back on their own" if m["grp"] == "control" and m["returned_at"]
        else STATUS_LABEL.get(m["status"], m["status"]),
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
        "returned_at": m["returned_at"],
        "return_amount": m["return_amount"],
    }


def list_messages(conn, experiment_id=None, group="all") -> list:
    sql, args = "SELECT * FROM messages WHERE 1 = 1", []
    if experiment_id is not None:
        sql += " AND experiment_id = ?"
        args.append(int(experiment_id))
    if group != "all":
        if group not in ("offer", "control", "direct"):
            raise ApiError(422, "group must be offer, control, direct or all.")
        sql += " AND grp = ?"
        args.append(group)
    sql += " ORDER BY id DESC"
    return [message_public(m) for m in conn.execute(sql, args)]


def experiment_public(conn, exp) -> dict | None:
    if exp is None:
        return None
    msgs = conn.execute("SELECT * FROM messages WHERE experiment_id = ? ORDER BY grp DESC, id", (exp["id"],)).fetchall()
    created = parse(exp["created_at"])
    return {
        "id": exp["id"],
        "status": exp["status"],
        "created_at": exp["created_at"],
        "report_on": fmt(created + timedelta(days=exp["measure_after_days"])),
        "report_on_label": date_label(created + timedelta(days=exp["measure_after_days"])),
        "approved_via": exp["approved_via"],
        "offer_amount": exp["offer_amount"],
        "basket": exp["basket"],
        "expected_rate": exp["expected_rate"],
        "natural_rate": exp["natural_rate"],
        "predicted_net": exp["predicted_net"],
        "result": json.loads(exp["result"]) if exp["result"] else None,
        "offer_messages": [message_public(m) for m in msgs if m["grp"] == "offer"],
        "control_messages": [message_public(m) for m in msgs if m["grp"] == "control"],
        "cancellable_until": fmt(created + timedelta(minutes=CANCEL_WINDOW_MIN)) if exp["status"] == "running" else None,
    }


def get_log(conn, limit: int = 100) -> list:
    rows = conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))
    return [{"id": r["id"], "at": r["at"], "who": r["who"], "event": r["event"], "decision": r["decision"],
             "outcome": r["outcome"]} for r in rows]


# ---------------------------------------------------------------- snapshot for the frontend

FE_CONSENT = {"opted_in": "opted-in", "pending": "pending", "none": "none"}


def _fe_source(c) -> str:
    if c["source"] == "paytm":
        return "Paytm opt-in"
    if c["source"] == "manual":
        return "Manual, Join confirmed" if c["consent"] == "opted_in" else "Manual"
    return {"opted_in": "Paytm opt-in", "pending": "Manual", "none": "None"}[c["consent"]]


def frontend_state(conn) -> dict:
    """Everything the browser app needs, in one consistent read."""
    at = demo_now(conn)
    profile = get_profile(conn)
    customers = load_customers(conn, at)
    b = compute_briefing(conn, at, customers, profile)
    running = running_experiment(conn)
    latest = latest_completed(conn)
    latest_result = json.loads(latest["result"]) if latest and latest["result"] else None
    results_exp = experiment_public(conn, running or latest)
    metrics = compute_metrics(conn, at, customers)
    slow = slow_day(conn, at, profile, b["basket"])
    issues = payment_issues(conn)
    regulars = [c for c in customers if c["segment"] in ("active_regular", "lapsed_regular")]
    all_bills = [a for c in customers for _, a in c["history"]]

    fe_customers = []
    for c in customers:
        last_contact = (at - parse(c["last_contacted_at"])).days if c["last_contacted_at"] else 45
        fe_customers.append({
            "id": f"c-{c['id']}",
            "name": c["name"],
            "number": c["phone_masked"],
            "lastVisitDays": c["days_since_last"] if c["days_since_last"] is not None else 0,
            "visits": c["visits"],
            "avgBasket": c["avg_bill"],
            "consent": FE_CONSENT[c["consent"]],
            "source": _fe_source(c),
            "seasonal": c["seasonal"],
            "lastMessageDays": last_contact,
            "history": [{"daysAgo": (at - t).days, "amount": a} for t, a in reversed(c["history"])],
        })

    if running:
        offer_ids = [f"c-{m['customer_id']}" for m in results_exp["offer_messages"] if m["customer_id"]]
        control_ids = [f"c-{m['customer_id']}" for m in results_exp["control_messages"] if m["customer_id"]]
    else:
        ids = b["eligible_ids"]
        offer_ids = [f"c-{i}" for i in ids[:b["offer_count"]]]
        control_ids = [f"c-{i}" for i in ids[b["offer_count"]:]]

    fe_messages = []
    msg_rows = conn.execute(
        "SELECT * FROM messages WHERE grp IN ('offer', 'direct') AND (experiment_id = ? OR grp = 'direct') ORDER BY id",
        (results_exp["id"] if results_exp else -1,),
    ).fetchall()
    for m in msg_rows:
        fe_messages.append({
            "id": f"m-{m['id']}",
            "serverId": m["id"],
            "customerId": f"c-{m['customer_id']}" if m["customer_id"] else None,
            "customerName": m["customer_name"],
            "number": m["phone_masked"],
            "status": STATUS_LABEL.get(m["status"], m["status"]),
            "createdAt": m["created_at"],
            "updatedAt": m["updated_at"],
            "text": m["text"] or "",
            "experiment": "Offer group" if m["grp"] == "offer" else "Merchant selected",
        })

    fe_activity = [{"time": r["at"], "who": r["who"], "event": r["event"], "decision": r["decision"],
                    "outcome": r["outcome"]}
                   for r in conn.execute("SELECT * FROM activity_log ORDER BY id")]

    days_advanced = int(db.get_setting(conn, "days_advanced", "0"))
    public_briefing = {k: v for k, v in b.items() if k not in ("eligible_ids",)}
    return {
        "frontend": {
            "customers": fe_customers,
            "messages": fe_messages,
            "experiment": {
                "offerIds": offer_ids,
                "controlIds": control_ids,
                "offerReturns": latest_result["offer_returned"] if latest_result else 0,
                "controlReturns": latest_result["control_returned"] if latest_result else 0,
                "basket": b["basket"] or (latest["basket"] if latest else 500),
                "discount": int(profile["discount_cap"]),
                "offerRate": float(profile["expected_return_rate"]),
                "naturalRate": float(profile["natural_return_rate"]),
            },
            "activity": fe_activity,
            "profile": {
                "storeName": profile["store_name"],
                "ownerName": profile["owner_name"],
                "category": profile["category"],
                "address": profile["address"],
                "openingTime": profile["opening_time"],
                "closingTime": profile["closing_time"],
                "dayOff": profile["day_off"],
                "languages": list(profile["languages"]),
                "learnedReset": not profile.get("learned"),
            },
            "autonomy": profile["autonomy"],
            "daysForward": days_advanced // 7,
        },
        "live": {
            "demo_now": fmt(at),
            "days_advanced": days_advanced,
            "briefing": public_briefing,
            "metrics": metrics,
            "latest_result": latest_result,
            "running": experiment_public(conn, running) if running else None,
            "results_experiment": results_exp,
            "opportunities": {
                "win_back": {"eligible_count": b["eligible_count"], "predicted_net": b["predicted_net"],
                             "formula": b["formula"], "status": b["status"]},
                "slow_day": slow,
                "payment_issues": issues,
            },
            "learned": {
                "expected_return_rate": float(profile["expected_return_rate"]),
                "natural_return_rate": float(profile["natural_return_rate"]),
                "items": profile.get("learned", []),
                "busiest_day": slow["busiest_day"] if slow else None,
                "slowest_day": slow["day"] if slow else None,
                "average_bill": rnd(sum(all_bills) / len(all_bills)) if all_bills else 0,
                "regular_customers": len(regulars),
            },
            "rules": {
                "autonomy": profile["autonomy"],
                "discount_cap": int(profile["discount_cap"]),
                "contact_limit_days": int(profile["contact_limit_days"]),
                "quiet_hours_start": profile["quiet_hours_start"],
            },
            "profile": {k: v for k, v in profile.items()},
        },
    }


def health(conn) -> dict:
    return {
        "status": "ok",
        "version": "1.0.0",
        "demo_now": fmt(demo_now(conn)),
        "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        "payments": conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
    }
