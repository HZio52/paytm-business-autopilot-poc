"""Deterministic synthetic data for the demo store (Meera Stores, Whitefield).

Same seed every time, so every reset gives the same starting story:
14 eligible lapsed regulars tonight (7 offer, 7 held back), last week's offer
returned 3 of 7 vs 1 of 7 for a measured net of 940 rupees, and 6 seasonal
customers the agent deliberately skips.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from . import core, db

FIRST_NAMES = ["Asha", "Ramesh", "Priya", "Suresh", "Lakshmi", "Vikram", "Nandini", "Arjun", "Kavya", "Imran",
               "Divya", "Manoj", "Neha", "Rahul", "Pooja", "Girish", "Anita", "Deepak", "Shalini", "Mohan",
               "Sahana", "Kiran", "Tarun", "Rekha", "Vivek", "Swati", "Harish", "Anjali", "Nikhil", "Sunita",
               "Ashok", "Ritu", "Mahesh", "Ishita", "Prakash", "Jyoti", "Rohan", "Maya", "Sanjay", "Varun",
               "Keerthi", "Abdul", "Shreya", "Ganesh", "Usha", "Ravi", "Fatima", "Santosh", "Bindu", "Lokesh"]
LAST_NAMES = ["Rao", "Kumar", "Nair", "Babu", "Devi", "Shah", "Mehta", "Iyer", "Khan", "Menon", "Patil",
              "Joshi", "Das", "Singh", "Shetty", "Verma", "Reddy", "Gupta", "Gowda", "Jain", "Pillai", "Bose",
              "Bhat", "Hegde", "Kamath", "Murthy", "Naidu", "Prasad", "Kulkarni", "Thomas"]


def _names(rng: random.Random, count: int) -> list:
    combos = [f"{f} {l}" for f in FIRST_NAMES for l in LAST_NAMES]
    rng.shuffle(combos)
    return combos[:count]


def _phone(rng: random.Random, used: set) -> str:
    while True:
        p = f"{rng.choice('9876')}{rng.randint(0, 999999999):09d}"
        if p not in used:
            used.add(p)
            return p


def _visit_time(day: datetime, rng: random.Random) -> datetime:
    return day.replace(hour=rng.randint(9, 19), minute=rng.choice((0, 15, 30, 45)), second=0, microsecond=0)


def _history(rng, last_visit: datetime, gap: int, visits: int, basket: int, jitter: int = 1) -> list:
    """Payments going backwards from last_visit, one every `gap` days (plus or minus jitter)."""
    times = [last_visit]
    t = last_visit
    for _ in range(visits - 1):
        t = t - timedelta(days=max(1, gap + rng.randint(-jitter, jitter)))
        nudged = t + timedelta(days=1) if (t.weekday() == 1 and rng.random() < 0.35) else t
        times.append(nudged if nudged < times[-1] else t)
    times.reverse()
    return [(_visit_time(x, rng), max(50, core.rnd(basket * rng.uniform(0.85, 1.15) / 10) * 10)) for x in times]


def seed(conn) -> None:
    rng = random.Random(42)
    now = core.SEED_NOW
    db.clear_all(conn)
    db.set_setting(conn, "demo_now", core.fmt(now))
    db.set_setting(conn, "days_advanced", 0)
    db.save_profile(conn, dict(core.DEFAULT_PROFILE))

    names = _names(rng, 200)
    used_phones = set()
    exp_created = now - timedelta(days=9)

    def add(name, consent, gap, basket, active, payments, last_contacted=None, source="seed"):
        created = payments[0][0] if payments else now
        cur = conn.execute(
            "INSERT INTO customers (name, phone, consent, source, created_at, last_contacted_at, "
            "sim_gap_days, sim_basket, sim_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, _phone(rng, used_phones), consent, source, core.fmt(created),
             core.fmt(last_contacted) if last_contacted else None, gap, basket, 1 if active else 0),
        )
        cid = cur.lastrowid
        conn.executemany("INSERT INTO payments (customer_id, amount, paid_at) VALUES (?, ?, ?)",
                         [(cid, amount, core.fmt(t)) for t, amount in payments])
        return cid

    def pick_consent(opted: float, pending: float) -> str:
        r = rng.random()
        return "opted_in" if r < opted else "pending" if r < opted + pending else "none"

    idx = 0

    def next_name():
        nonlocal idx
        idx += 1
        return names[idx - 1]

    # 1. Last week's experiment: 14 lapsed regulars, 7 offer and 7 held back.
    offer_ids, control_ids = [], []
    for i in range(14):
        gap, basket = rng.randint(4, 8), rng.choice((450, 480, 500, 520, 550))
        last = _visit_time(exp_created - timedelta(days=rng.randint(22, 30)), rng)
        hist = _history(rng, last, gap, rng.randint(6, 12), basket)
        is_offer = i < 7
        returns = (is_offer and i < 3) or (not is_offer and i == 7)
        cid = add(next_name(), "opted_in", gap, basket, returns, hist,
                  last_contacted=exp_created if is_offer else None)
        (offer_ids if is_offer else control_ids).append((cid, returns))

    # 2. Lapsed regulars who were never contacted: 8 eligible, 2 pending, 2 no consent.
    for consent in ["opted_in"] * 8 + ["pending"] * 2 + ["none"] * 2:
        gap, basket = rng.randint(4, 8), rng.choice((450, 480, 500, 520, 550))
        last = _visit_time(now - timedelta(days=rng.randint(22, 40)), rng)
        add(next_name(), consent, gap, basket, False, _history(rng, last, gap, rng.randint(5, 12), basket))

    # 3. Regulars who are about to stop coming (they lapse after a fast-forward).
    for _ in range(8):
        gap, basket = rng.randint(4, 7), rng.choice((420, 460, 500, 540, 580))
        last = _visit_time(now - timedelta(days=rng.randint(8, 13)), rng)
        add(next_name(), "opted_in", gap, basket, False, _history(rng, last, gap, rng.randint(6, 12), basket))

    # 4. Seasonal visitors: come every 2 to 4 months around festivals.
    for consent in ["opted_in"] * 6 + ["none"] * 3 + ["pending"] * 3:
        gap, basket = rng.randint(70, 110), rng.choice((700, 850, 950, 1200))
        last = _visit_time(now - timedelta(days=rng.randint(25, 60)), rng)
        add(next_name(), consent, gap, basket, False, _history(rng, last, gap, rng.randint(2, 4), basket, jitter=5))

    # 5. New customers: one or two visits in the last three weeks.
    for consent in ["opted_in"] * 10 + ["pending"] * 4 + ["none"] * 2:
        gap, basket = rng.randint(6, 10), rng.randrange(250, 650, 10)
        last = _visit_time(now - timedelta(days=rng.randint(0, 6)), rng)
        if last > now:
            last = last - timedelta(days=1)
        visits = rng.randint(1, 2)
        add(next_name(), consent, gap, basket, True, _history(rng, last, 5, visits, basket),
            source="paytm" if consent == "opted_in" else "seed")

    # 6. Occasional shoppers: every 10 to 16 days.
    for _ in range(40):
        gap, basket = rng.randint(10, 16), rng.randrange(300, 800, 10)
        last = _visit_time(now - timedelta(days=rng.randint(0, gap - 2)), rng)
        if last > now:
            last = last - timedelta(days=1)
        add(next_name(), pick_consent(0.6, 0.15), gap, basket, True,
            _history(rng, last, gap, rng.randint(4, 10), basket))

    # 7. Everyday regulars: the rest of the 200.
    while idx < 200:
        gap, basket = rng.randint(3, 8), rng.randrange(250, 700, 10)
        last = _visit_time(now - timedelta(days=rng.randint(0, gap - 1)), rng)
        if last > now:
            last = last - timedelta(days=1)
        add(next_name(), pick_consent(0.75, 0.10), gap, basket, True,
            _history(rng, last, gap, rng.randint(8, 20), basket))

    # Last week's experiment record and its messages.
    cur = conn.execute(
        "INSERT INTO experiments (created_at, status, offer_amount, basket, expected_rate, natural_rate, "
        "predicted_net, measure_after_days, approved_via, completed_at) "
        "VALUES (?, 'completed', 20, 500, 0.30, 0.10, 658, 7, 'voice', ?)",
        (core.fmt(exp_created), core.fmt(exp_created + timedelta(days=7))),
    )
    exp_id = cur.lastrowid
    return_days = iter((2, 3, 5))
    read_flags = iter(("read", "delivered", "read", "delivered"))
    for cid, returns in offer_ids:
        row = conn.execute("SELECT name, phone FROM customers WHERE id = ?", (cid,)).fetchone()
        first = row["name"].split()[0]
        text = core.customer_message("en", first, "Meera Stores", 20, exp_created + timedelta(days=5))
        if returns:
            back = (exp_created + timedelta(days=next(return_days))).replace(hour=rng.randint(10, 19), minute=0)
            conn.execute("INSERT INTO payments (customer_id, amount, paid_at) VALUES (?, 500, ?)", (cid, core.fmt(back)))
            status, returned_at, amount, updated = "returned", core.fmt(back), 500, core.fmt(back)
        else:
            status, returned_at, amount = next(read_flags), None, None
            updated = core.fmt(exp_created + timedelta(hours=2))
        conn.execute(
            "INSERT INTO messages (experiment_id, customer_id, customer_name, phone_masked, grp, language, text, "
            "status, created_at, updated_at, returned_at, return_amount) VALUES (?, ?, ?, ?, 'offer', 'en', ?, ?, ?, ?, ?, ?)",
            (exp_id, cid, row["name"], core.mask_phone(row["phone"]), text, status, core.fmt(exp_created),
             updated, returned_at, amount),
        )
    for cid, returns in control_ids:
        row = conn.execute("SELECT name, phone FROM customers WHERE id = ?", (cid,)).fetchone()
        returned_at = amount = None
        if returns:
            back = (exp_created + timedelta(days=4)).replace(hour=17, minute=0)
            conn.execute("INSERT INTO payments (customer_id, amount, paid_at) VALUES (?, 500, ?)", (cid, core.fmt(back)))
            returned_at, amount = core.fmt(back), 500
        conn.execute(
            "INSERT INTO messages (experiment_id, customer_id, customer_name, phone_masked, grp, language, text, "
            "status, created_at, updated_at, returned_at, return_amount) "
            "VALUES (?, ?, ?, ?, 'control', 'en', NULL, 'held_back', ?, ?, ?, ?)",
            (exp_id, cid, row["name"], core.mask_phone(row["phone"]), core.fmt(exp_created),
             returned_at or core.fmt(exp_created), returned_at, amount),
        )
    exp = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
    result = core.measure(conn, exp)
    conn.execute("UPDATE experiments SET result = ? WHERE id = ?", (json.dumps(result), exp_id))

    # Payment support questions waiting for drafted replies.
    for i in range(8):
        conn.execute("INSERT INTO tickets (created_at, kind, status, customer_name) VALUES (?, 'duplicate_debit', 'open', ?)",
                     (core.fmt(now - timedelta(hours=30 - i * 3)), names[100 + i]))

    # Activity log that tells last week's story.
    log = [
        (exp_created - timedelta(minutes=4), "Agent", "Prepared the evening briefing",
         "14 customers eligible, 7 offer and 7 held back", "Predicted ₹658 net"),
        (exp_created, "Merchant", "Approved the offer by voice", "7 offer, 7 held back, consent checked",
         "Predicted ₹658 net"),
        (exp_created, "Agent", "Sent the offer to 7 customers", "7 customers not messaged, to compare fairly",
         f"Result on {core.date_label(exp_created + timedelta(days=7))}"),
        (exp_created + timedelta(days=7), "Agent", "Offer result is in", "3 of 7 offer vs 1 of 7 held back",
         f"{core.inr(result['net_incremental'])} net incremental"),
        (exp_created + timedelta(days=7), "Agent", "Updated what I expect",
         "Expected offer return rate raised from 30% to 35%", "Used in the next prediction"),
        (now - timedelta(days=2), "Agent", "Skipped seasonal customers",
         "6 seasonal customers only come during festivals", "No discount sent"),
        (now - timedelta(hours=6), "Agent", "Drafted replies for payment questions",
         "8 duplicate-debit questions", "Waiting for your review"),
    ]
    for at, who, event, decision, outcome in log:
        db.add_log(conn, core.fmt(at), who, event, decision, outcome)

    b = core.compute_briefing(conn, now)
    db.add_log(conn, core.fmt(now), "Agent", "Prepared tonight's briefing",
               f"{b['eligible_count']} customers eligible, {b['offer_count']} offer and {b['control_count']} held back",
               f"Predicted {core.inr(b['predicted_net'])} net. Waiting for your answer")


def ensure_seeded() -> None:
    with db.writing() as conn:
        db.init_schema(conn)
        if db.get_setting(conn, "demo_now") is None:
            seed(conn)


def reset() -> None:
    with db.writing() as conn:
        db.init_schema(conn)
        seed(conn)
