/*
 * Paytm Business Autopilot: connects the app to the FastAPI backend.
 *
 * Load this AFTER app.js. On page load it asks the backend for the current
 * state. If the backend answers, every screen shows server-computed numbers and
 * every action (approve, fast-forward, add customer, profile, ...) is saved in
 * the database. If the backend is not running or the network drops, the app
 * keeps working exactly as before, in browser-only demo mode.
 *
 * To point at a backend on another address, set this before the script tag:
 *   <script>window.AUTOPILOT_API_BASE = "https://your-backend.onrender.com";</script>
 */
(() => {
  "use strict";

  const BASE = String(
    window.AUTOPILOT_API_BASE || (location.protocol === "file:" ? "http://127.0.0.1:8000" : "")
  ).replace(/\/$/, "");
  const TIMEOUT_MS = 8000;
  let live = false;
  let server = null;
  let voiceStartedAt = 0;

  const A = window.__autopilotApp;
  if (!A) {
    console.warn("backend-bridge.js: app.js hook not found. Load backend-bridge.js after app.js.");
    return;
  }
  const state = () => A.state;
  const toast = (m) => A.toast(m);

  // ------------------------------------------------------------ helpers
  const byId = (id) => document.getElementById(id);
  const setText = (id, value) => {
    const el = byId(id);
    if (el && value !== undefined && value !== null) el.textContent = String(value);
  };
  const esc = (v) =>
    String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const rupee = (n) => "₹" + Math.round(Number(n) || 0).toLocaleString("en-IN");
  const pc = (r) => Math.round((Number(r) || 0) * 100) + "%";
  const lang = () => String(state().language || "en-IN").slice(0, 2);
  const numericId = (feId) => Number(String(feId).replace(/^c-/, ""));

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(BASE + path, {
        method: options.method || "GET",
        headers: options.body ? { "Content-Type": "application/json" } : {},
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: controller.signal,
      });
      let data = null;
      try {
        data = await res.json();
      } catch (e) {
        data = null;
      }
      if (!res.ok) {
        const msg = data && typeof data.detail === "string" ? data.detail : "Check the values and try again.";
        const err = new Error(msg);
        err.status = res.status;
        err.data = data;
        throw err;
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  // ------------------------------------------------------------ status line
  function showStatus() {
    let el = byId("backend-status");
    if (!el) {
      el = document.createElement("div");
      el.id = "backend-status";
      el.className = "demo-line";
      el.setAttribute("role", "status");
      const anchor = document.querySelector(".demo-line");
      if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(el, anchor.nextSibling);
      else document.body.prepend(el);
    }
    el.classList.toggle("is-live", live);
    el.textContent = live
      ? "Live data: connected to the Autopilot backend. Changes are saved on the server."
      : "Browser-only demo: the backend is not running. Changes are saved on this device only.";
  }

  function goOffline() {
    live = false;
    showStatus();
  }

  // ------------------------------------------------------------ apply server state
  function applyServer(snapshot) {
    if (!snapshot || !snapshot.frontend || !snapshot.live) return;
    server = snapshot.live;
    const f = snapshot.frontend;
    const st = state();
    st.customers = f.customers;
    st.messages = f.messages;
    st.experiment = f.experiment;
    st.activity = f.activity;
    st.profile = Object.assign({}, st.profile, f.profile);
    st.autonomy = f.autonomy === "auto" ? "autopilot" : "ask";
    st.daysForward = f.daysForward;
    for (const l of ["en", "hi", "kn"]) {
      if (A.speech[l] && server.briefing.speech && server.briefing.speech[l]) A.speech[l].text = server.briefing.speech[l];
    }
    for (const id of Array.from(A.selected)) {
      if (!st.customers.some((c) => c.id === id)) A.selected.delete(id);
    }
    try {
      A.save(); // keep a local copy so the app still has the latest data if the backend stops
    } catch (e) {
      /* storage full or blocked: not fatal */
    }
  }

  // ------------------------------------------------------------ server-computed screens
  function renderLive() {
    if (!live || !server) return;
    const b = server.briefing;
    const m = server.metrics;
    const r = server.latest_result;
    const run = server.running;
    const o = server.opportunities;
    const L = server.learned;
    const l = lang();

    setText("metric-revenue", rupee(m.revenue_week));
    setText("metric-lift", r ? rupee(r.net_incremental) : "No result yet");
    setText(
      "metric-lift-detail",
      r
        ? `${r.offer_returned} of ${r.offer_count} offer, ${r.control_returned} of ${r.control_count} held back`
        : "First result arrives 7 days after an offer"
    );
    setText("metric-repeat", `${m.repeat_rate_pct}%`);
    setText("metric-consent", `${m.consent_pct}%`);

    setText("briefing-quote", (b.quote && (b.quote[l] || b.quote.en)) || "");
    const approvalWord = (A.speech[l] && A.speech[l].approval) || "yes";
    if (b.status === "awaiting_approval") {
      setText("briefing-sub", `Predicted net extra revenue: ${rupee(b.predicted_net)}. Say ${approvalWord} to approve.`);
    } else if (b.status === "running" && run) {
      setText(
        "briefing-sub",
        `Offer sent to ${run.offer_messages.length} customers, ${run.control_messages.length} held back. Result on ${run.report_on_label}.`
      );
    } else {
      setText("briefing-sub", "Nothing to approve tonight.");
    }

    const formula = byId("briefing-formula");
    if (formula) {
      formula.innerHTML =
        b.status === "nothing_to_do"
          ? "<b>No offer needed tonight</b>"
          : `<b>${b.offer_count} offer customers</b><span class="equals">times</span>` +
            `<b>(${pc(b.offer_rate)} offer minus ${pc(b.natural_rate)} natural)</b><span class="equals">times</span>` +
            `<b>${rupee(b.basket)}</b><span class="equals">minus</span><b>${rupee(b.discount_total)} discount</b>` +
            `<span class="equals">equals</span><b>${rupee(b.predicted_net)} net</b>`;
    }
    setText(
      "step-detect",
      `Payment recency and frequency find ${b.eligible_count} opted-in, non-seasonal customers outside their usual return window.`
    );
    setText("step-formula", `${b.offer_count} offer customers, ${b.control_count} held back, predicted net ${rupee(b.predicted_net)}.`);
    setText(
      "step-ask",
      `${b.eligible_count} eligible customers are split into ${b.offer_count} offer and ${b.control_count} held back. Outreach is consent-gated.`
    );
    setText(
      "briefing-state",
      run
        ? `Offer running, result on ${run.report_on_label}`
        : b.status === "nothing_to_do"
        ? "Nothing to approve tonight"
        : server.days_advanced
        ? `${Math.floor(server.days_advanced / 7)} weeks later, briefing recalculated`
        : "Briefing ready"
    );
    const btn = byId("approve-button");
    if (btn) {
      btn.disabled = b.status !== "awaiting_approval";
      if (run) btn.textContent = l === "hi" ? "ऑफ़र भेजा गया" : l === "kn" ? "ಆಫರ್ ಕಳುಹಿಸಲಾಗಿದೆ" : "Offer sent";
    }

    if (o.slow_day) {
      const s = o.slow_day;
      const text = `${s.day} is ${s.pct_below_usual}% below a usual day. ${s.formula}. ${s.assumption}`;
      setText("quiet-value", rupee(s.predicted_net_per_week));
      setText("quiet-formula", text);
      setText("slow-value", rupee(s.predicted_net_per_week));
      setText("slow-formula", text);
    }
    setText("ticket-count", o.payment_issues.open_tickets);

    const dots = (id, filled, total) => {
      const el = byId(id);
      if (el) el.innerHTML = Array.from({ length: total }, (_, i) => `<i class="${i < filled ? "filled" : ""}"></i>`).join("");
    };
    if (r) {
      dots("result-offer-dots", r.offer_returned, r.offer_count);
      dots("result-control-dots", r.control_returned, r.control_count);
      setText("result-offer-count", `${r.offer_returned} of ${r.offer_count}`);
      setText("result-control-count", `${r.control_returned} of ${r.control_count}`);
      setText("result-net", rupee(r.net_incremental));
    }
    const learnedRate = (L.items || []).find((x) => x.key === "return_rate");
    setText(
      "learning-note",
      learnedRate
        ? `${learnedRate.text}, because ${String(learnedRate.evidence).toLowerCase()}. The next prediction uses ${pc(L.expected_return_rate)}.`
        : `The next prediction uses an expected return rate of ${pc(L.expected_return_rate)}.`
    );
    if (o.slow_day) setText("quiet-title", `${o.slow_day.day} is your slowest day`);
    setText("seasonal-count", `${b.skipped_seasonal_count} customers`);
    setText(
      "seasonal-copy",
      `The agent skipped a discount for ${b.skipped_seasonal_count} seasonal customers who only come during festivals. A discount now would pay for visits that happen anyway.`
    );

    setText("learned-busiest", L.busiest_day || "Not enough data yet");
    setText("learned-slowest", L.slowest_day || "Not enough data yet");
    setText("learned-average", rupee(L.average_bill));
    setText("learned-regular", L.regular_customers);
    setText(
      "learned-preference",
      L.items && L.items.length
        ? L.items.map((x) => `${x.text} (${String(x.evidence).toLowerCase()})`).join(". ") + "."
        : "No learned preferences yet."
    );
    setText("learned-return", pc(L.expected_return_rate));
    setText("settings-briefing", `Briefing arrives at ${b.briefing_time_label}`);
    document.querySelectorAll("[data-i18n='briefingDelivery']").forEach((x) => {
      x.textContent = `Soundbox or WhatsApp at ${b.briefing_time_label}`;
    });
  }

  // ------------------------------------------------------------ wrap app.js functions
  const NAMES = ["renderAll", "renderMessages", "showWhy", "approve", "listen", "age", "newWeek", "reset",
    "confirmPending", "confirmSend", "saveProfile"];
  const original = {};
  for (const name of NAMES) original[name] = A.fn(name);
  const call = (name, ...args) => A.fn(name)(...args);

  A.replace("renderAll", function () {
    original.renderAll();
    renderLive();
  });

  A.replace("renderMessages", function () {
    if (!live || !server || !server.results_experiment) return original.renderMessages();
    const body = byId("messages-body");
    if (!body) return;
    const e = server.results_experiment;
    const rows = A.messageTab === "control" ? e.control_messages : e.offer_messages;
    body.innerHTML =
      rows
        .map(
          (m) =>
            `<tr><td><strong>${esc(m.customer_name)}</strong><br><span class="subtle">${esc(m.phone_masked)}</span></td>` +
            `<td><span class="badge ${m.returned_at ? "good" : ""}">${esc(m.status_label)}</span></td>` +
            `<td>${m.returned_at ? "Came back " + esc(A.date(m.returned_at)) : m.group === "control" ? "Not messaged" : esc(A.date(m.created_at))}</td>` +
            `<td>${m.group === "control" ? "Control" : "Offer"}</td></tr>`
        )
        .join("") || `<tr><td colspan="4" class="empty">No messages yet.</td></tr>`;
  });

  A.replace("showWhy", function () {
    if (!live || !server) return original.showWhy();
    const b = server.briefing;
    const r = server.latest_result;
    const picked = (b.eligible_preview || [])
      .map((c) => `${esc(c.name)}: usually every ${esc(c.usual_gap_days)} days, now ${esc(c.days_since_last)} days`)
      .join("<br>");
    byId("why-body").innerHTML =
      `<p>${b.eligible_count} customers who agreed to receive offers usually come every few days but have not paid in more than 3 weeks.</p>` +
      `<div class="dot-row"><div><strong>Prediction</strong><p>${esc(b.formula)}</p><p class="subtle">${esc(b.rate_source)}</p></div></div>` +
      (r
        ? `<div class="dot-row"><div><strong>Last result</strong><p>${esc(r.formula)}</p><p class="subtle">${esc(r.baseline_explained)}</p></div></div>`
        : "") +
      (picked ? `<div class="dot-row"><div><strong>Who was picked</strong><p>${picked}</p></div></div>` : "");
    A.openDrawer("why-drawer");
  });

  async function runAction(request, fallback, successToast) {
    try {
      const res = await request();
      applyServer(res.state);
      call("renderAll");
      if (successToast) toast(typeof successToast === "function" ? successToast(res) : successToast);
      return res;
    } catch (err) {
      if (err && err.status) {
        toast(err.message); // the backend said no, for example consent or a running offer
        return null;
      }
      goOffline();
      toast("Backend not reachable. Continuing in browser-only demo mode.");
      if (fallback) fallback();
      return null;
    }
  }

  function toastWithUndo(text, undo) {
    toast(text + " Tap here to undo.");
    const n = byId("toast");
    if (!n) return;
    let used = false;
    const tf = A.toastFn;
    clearTimeout(tf.timer);
    tf.timer = setTimeout(() => n.classList.remove("show"), 10000);
    n.onclick = () => {
      if (used) return;
      used = true;
      n.onclick = null;
      undo();
    };
    setTimeout(() => {
      if (!used) n.onclick = null;
    }, 10000);
  }

  A.replace("listen", function () {
    voiceStartedAt = Date.now();
    return original.listen();
  });

  A.replace("approve", function () {
    if (!live) return original.approve();
    const check = byId("consent-check");
    if (check && !check.checked) {
      toast("Approval blocked because consent is required.");
      return;
    }
    const via = Date.now() - voiceStartedAt < 15000 ? "voice" : "tap";
    voiceStartedAt = 0;
    runAction(
      () => api("/api/experiments", { method: "POST", body: { approved_via: via, language: lang() } }),
      original.approve
    ).then((res) => {
      if (!res) return;
      const run = server.running;
      const count = run ? run.offer_messages.length : 0;
      toastWithUndo(`Offer sent to ${count} customers.`, () =>
        runAction(() => api(`/api/experiments/${res.experiment_id}/cancel`, { method: "POST" }), null, "Offer cancelled. Nothing was sent.")
      );
    });
  });

  A.replace("age", function () {
    if (!live) return original.age();
    runAction(
      () => api("/api/demo/fast-forward", { method: "POST", body: { days: 21 } }),
      original.age,
      (res) => `Moved forward 3 weeks. ${res.new_payments} new payments${res.completed ? ", and the offer result is in" : ""}.`
    );
  });

  A.replace("newWeek", function () {
    if (!live) return original.newWeek();
    runAction(
      () => api("/api/demo/new-week", { method: "POST" }),
      original.newWeek,
      (res) => `New week generated with ${res.new_payments} payments${res.completed ? ", and the offer result is in" : ""}.`
    );
  });

  A.replace("reset", function () {
    if (!live) return original.reset();
    runAction(() => api("/api/demo/reset", { method: "POST" }), original.reset, "Demo reset to its starting data.").then(
      (res) => {
        if (res) A.navigate("tonight");
      }
    );
  });

  A.replace("confirmPending", function () {
    if (!live) return original.confirmPending();
    const c = state().customers.find((x) => x.consent === "pending");
    if (!c) {
      toast("No pending customer is waiting to join.");
      return;
    }
    runAction(
      () => api(`/api/customers/${numericId(c.id)}/confirm-consent`, { method: "POST" }),
      original.confirmPending,
      `${c.name} replied YES and can now receive offers.`
    );
  });

  A.replace("confirmSend", function () {
    if (!live) return original.confirmSend();
    const review = A.pendingReview;
    if (!review.length) return;
    const ids = review.map((c) => numericId(c.id));
    runAction(
      () => api("/api/messages/send", { method: "POST", body: { customer_ids: ids, language: lang() } }),
      original.confirmSend
    ).then((res) => {
      if (!res) return;
      A.closeDrawers();
      A.selected.clear();
      call("renderAll");
      A.navigate("results");
      const skipped = res.skipped && res.skipped.length ? ` ${res.skipped.length} skipped by your rules.` : "";
      toastWithUndo(`Offer sent to ${res.message_ids.length} customers.${skipped}`, () =>
        runAction(
          () => api("/api/messages/undo", { method: "POST", body: { message_ids: res.message_ids } }),
          null,
          "Messages undone. Nothing was delivered."
        )
      );
    });
  });

  A.replace("saveProfile", function (e) {
    const before = JSON.stringify(state().profile);
    original.saveProfile(e);
    if (!live || JSON.stringify(state().profile) === before) return;
    const p = state().profile;
    runAction(
      () =>
        api("/api/profile", {
          method: "PATCH",
          body: {
            store_name: p.storeName,
            owner_name: p.ownerName,
            category: p.category,
            address: p.address,
            opening_time: p.openingTime,
            closing_time: p.closingTime,
            day_off: p.dayOff,
            languages: p.languages,
          },
        }),
      null
    );
  });

  // Add customer form: send to the backend first (capture phase runs before app.js's handler).
  document.addEventListener(
    "submit",
    (e) => {
      if (!live || !e.target.matches("#add-form")) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      const form = e.target;
      const name = byId("new-name").value.trim();
      const phone = byId("new-number").value.trim();
      setText("add-error", "");
      api("/api/customers", { method: "POST", body: { name, phone, source: "manual" } })
        .then((res) => {
          applyServer(res.state);
          call("renderAll");
          form.reset();
          A.closeDrawers();
          toast(`${name} added. Waiting for them to reply YES.`);
        })
        .catch((err) => {
          if (err && err.status) {
            setText("add-error", err.message);
          } else {
            goOffline();
            toast("Backend not reachable. Press Add again to save on this device.");
          }
        });
    },
    true
  );

  // Autonomy and agent rules: let app.js update the screen, then save on the server.
  document.addEventListener(
    "change",
    (e) => {
      if (!live || !e.target.matches('input[name="autonomy"]')) return;
      const value = e.target.value === "autopilot" ? "auto" : "ask";
      setTimeout(() => runAction(() => api("/api/profile", { method: "PATCH", body: { autonomy: value } }), null), 0);
    },
    true
  );
  document.addEventListener(
    "click",
    (e) => {
      if (!live || !e.target.closest('[data-action="saveRules"]')) return;
      const digits = String((byId("discount-rule") || {}).value || "").replace(/\D/g, "");
      if (!digits) return;
      setTimeout(() => runAction(() => api("/api/profile", { method: "PATCH", body: { discount_cap: Number(digits) } }), null), 0);
    },
    true
  );

  // ------------------------------------------------------------ start
  async function connect() {
    try {
      const snapshot = await api("/api/state");
      live = true;
      applyServer(snapshot);
      call("renderAll");
    } catch (err) {
      live = false;
      console.info("Autopilot backend not reachable, using browser-only demo mode.", err && err.message);
    }
    showStatus();
  }

  window.AutopilotBackend = {
    api,
    base: BASE || location.origin,
    isLive: () => live,
    reconnect: connect,
    state: () => server,
  };

  connect();
})();
