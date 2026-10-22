# Backend guide: Paytm Business Autopilot

The app now has a real backend: **Python + FastAPI + SQLite**. Every number on screen (eligible customers, the offer prediction, the measured result, weekly revenue, the slow day) is calculated on the server from a database of synthetic payments. If the backend is not running, the app still works exactly as before, in browser-only mode, so the demo can never break.

## What is in the folder

```
PAYTM PRO/
  index.html            the app (now also loads backend-bridge.js)
  app.js                the app logic (unchanged except a small hook at the end)
  style.css
  backend-bridge.js     connects the app to the backend, falls back to browser-only mode
  backend/
    main.py             the API (FastAPI routes)
    core.py             all business logic: segments, briefing, offers, results, learning
    seed.py             the synthetic store: 200 customers, about 2,000 payments, last week's offer
    db.py               SQLite tables
    requirements.txt
    smoke_test.py       checks a running server end to end
    tests/test_core.py  12 logic tests
  render.yaml           one-click hosting on Render (optional)
```

The database file is created automatically at `backend/data/autopilot.db` the first time the server starts.

## Step 1: Install Python (once)

1. Download Python 3.11 or newer from python.org and run the installer.
2. On the first installer screen, tick **"Add python.exe to PATH"**, then click Install.
3. Open a new terminal (Windows Terminal or PowerShell) and check:

```powershell
python --version
```

You should see `Python 3.11.x` or newer.

## Step 2: Open the project folder in a terminal

```powershell
cd "$env:USERPROFILE\OneDrive\Desktop\PAYTM PRO"
```

Tip: in VS Code, open the folder and use Terminal > New Terminal; it opens in the right place.

## Step 3: Create a virtual environment and install (once)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
```

If PowerShell says running scripts is disabled, run this once, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Step 4: Start the backend

Always run this from the `PAYTM PRO` folder (not from inside `backend`):

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --reload
```

You should see `Uvicorn running on http://127.0.0.1:8000`.

## Step 5: Open the app

Open **http://127.0.0.1:8000** in Chrome or Edge.

Under the demo line at the top you will see either:

- "Live data: connected to the Autopilot backend." The backend is working.
- "Browser-only demo: the backend is not running." The app is running on its own.

The API documentation with a "Try it out" button for every endpoint is at **http://127.0.0.1:8000/docs**. This page is worth showing judges.

Because the app is served from `127.0.0.1`, the browser treats it as secure, so the microphone works there too.

## Step 6: Check everything works

Leave the server running. Open a second terminal in the same folder and run:

```powershell
.\.venv\Scripts\Activate.ps1
python backend\smoke_test.py
```

You should see 16 passed. It resets the demo data at the end.

To run the logic tests:

```powershell
python -m unittest backend.tests.test_core -v
```

## What each button does now

| In the app | What happens on the server |
|---|---|
| Page load | Loads customers, briefing, metrics, results, log and profile from `/api/state` |
| Yes, send offer / saying "haan" | Creates a real offer: 7 random customers get it, 7 are held back. Logged as by voice or by tap |
| Tap the toast within 10 seconds | Cancels the offer. Nothing is sent |
| Fast-forward 3 weeks | Moves the demo clock 21 days, generates new payments, finishes the running offer, measures the true lift against the held-back group, and updates the expected return rate |
| New week | Moves the clock 7 days with new payments |
| Add customer | Validates the Indian mobile number, rejects duplicates, saves as "waiting for YES" |
| Simulate: customer taps Join | That customer becomes opted in on the server |
| Review and send (Customers screen) | Sends only to customers who pass consent and the once-per-30-days rule, with Undo |
| Profile save | Closing time moves the briefing time; day off is removed from the slow-day calculation |
| Autonomy: Autopilot within rules | On the next New week or Fast-forward, the agent sends the offer by itself within your rules |
| Reset demo | Rebuilds the same starting data every time |

### The starting story (same after every reset)

- Tonight: 14 eligible customers, 7 get the offer, 7 held back. The prediction is 7 × (35% − 10%) × ₹500 − ₹49 = ₹826.
- Last week: 3 of 7 came back with the offer, 1 of 7 came back on their own, so ₹1,500 − ₹500 − ₹60 = ₹940 net.
- 6 seasonal customers are skipped on purpose.

After you approve and fast-forward, the simulated result is 2 of 7 vs 1 of 7, about ₹490. That is below the prediction, so the agent lowers its expected return rate from 35% to 30%. This is honest, and a good line for judges: "it learns in both directions."

## Step 7 (optional): Put it online with Render

Only do this if you have time. The local version is enough for the demo.

1. Push the whole folder to GitHub (the `.gitignore` already skips the database and virtual environment).
2. Go to render.com, sign in with GitHub, click **New > Blueprint**, and pick your repository. Render reads `render.yaml` and sets everything up.
3. After the build, open the `onrender.com` link. The app and API are both on that address.

Things to know about Render's free plan:

- It sleeps after 15 minutes without visits. The first visit then takes about 50 seconds, so open the link 2 minutes before you present.
- The database resets when the server restarts. That is fine for a demo, since it reseeds the same story.

To use the GitHub Pages version of the app with the Render backend, add this line in `index.html` just before `<script src="app.js">`:

```html
<script>window.AUTOPILOT_API_BASE = "https://YOUR-APP.onrender.com";</script>
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named backend` | You started uvicorn from inside the `backend` folder. Go up one folder to `PAYTM PRO` and run it again |
| `uvicorn is not recognized` | Use `python -m uvicorn ...` and make sure the virtual environment is activated |
| Port 8000 already in use | `python -m uvicorn backend.main:app --port 8001` and open `http://127.0.0.1:8001` |
| App says "Browser-only demo" | The server is not running, or you opened `index.html` by double-clicking. Open `http://127.0.0.1:8000` instead |
| Want a fresh start | Use Reset demo in the app, or stop the server and delete `backend\data\autopilot.db` |

## Demo day checklist

1. Start the server 10 minutes before. Open the app and check that the status line says "Live data".
2. Click Reset demo so you start from the known story.
3. Keep the `/docs` page open in a second tab to show the real API.
4. If anything fails, stop the server. The app switches to browser-only mode and keeps working.
