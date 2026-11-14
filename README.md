# BloodLink Mangaluru

Emergency blood-stock finder. A relative or responder picks a blood group and a location and instantly sees nearby hospitals / blood banks that have it, nearest first, with how fresh each number is.

Stack: **Python, Flask, SQLite, HTML/CSS/JavaScript, Bootstrap 5** (Bootstrap is bundled in `static/vendor`, so the pages work without a CDN or internet for assets).

## Run it

```bash
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

On first start the app creates the SQLite database in `instance/` and loads **fictional demo data** (8 Mangaluru-area hospitals). It prints a random admin login and a Father Muller staff login; all credentials and API keys are saved in `instance/demo_credentials.txt`.

Run the tests: `python -m unittest -v test_app`

## How stock gets into the system

Each hospital has its own login. Three ways to update, all writing to the same `blood_stock` table:

| Method | Who it suits | Where |
|---|---|---|
| **Staff dashboard** (the main path) | Blood-bank staff, 10 seconds per update | `/staff/login` |
| **CSV upload** | Hospitals that can export from inventory software | Dashboard -> Upload CSV (`blood_group,units`) |
| **API push** | Hospitals whose software can call a URL | `POST /api/stock` with `X-API-Key` |

API example:

```bash
curl -X POST http://127.0.0.1:5000/api/stock \
  -H "X-API-Key: bl_xxxxxxxx" -H "Content-Type: application/json" \
  -d '{"stock":[{"blood_group":"A+","units":6},{"blood_group":"O-","units":2}]}'
```

Saving always refreshes the "updated" time, even for unchanged numbers, so staff can re-confirm stock quickly.

## Pages

| URL | Purpose |
|---|---|
| `/` | Public emergency search (no login) |
| `/staff/login`, `/staff` | Hospital staff: update stock, CSV, API key, password, activity log |
| `/admin/login`, `/admin` | Administrator: add hospitals, verify/hide them, reset passwords |
| `GET /api/search?group=A%2B&lat=..&lng=..&radius=25` | Public JSON search |
| `GET /api/stock` (with key) | A hospital reads back its own stock |

## Safety features built in

- **"Updated X ago" on every result.** Over 6 h is shown amber, over 24 h red with a "may be wrong, call to confirm" warning, and stale entries rank below fresh ones.
- Prominent **Call** and **Directions** buttons on every card; a footer reminder to confirm by phone.
- **Only verified hospitals appear publicly.** The administrator controls this.
- Location by GPS **or** by picking an area (GPS needs HTTPS or localhost; the area list is the fallback).
- Passwords hashed; API keys stored only as hashes and shown once; CSRF protection on all forms; login lockout after 5 failures; strict Content-Security-Policy; all inputs validated; uploads and updates are all-or-nothing.

## Before real-world use (important)

1. **Replace the demo data.** Stock numbers, phone numbers and coordinates in `seed.py` are invented or approximate. Add real hospitals through `/admin` with their real blood-bank phone numbers and exact coordinates.
2. **Get each hospital's agreement.** The app is only as good as the staff keeping it updated. Agree who updates it, how often, and who is responsible for accuracy.
3. **Create a real admin** and stop auto-demo: `BLOODLINK_SEED=0`, then `flask --app app create-admin yourname`.
4. **Deploy properly:** a WSGI server (e.g. `gunicorn app:create_app()` or `waitress`), HTTPS, `BLOODLINK_HTTPS=1`, a fixed `BLOODLINK_SECRET_KEY`, and `BLOODLINK_DEMO=0` to hide the demo banner. Behind a reverse proxy, configure `ProxyFix` so the login lockout sees real client IPs. The lockout counter is in memory, so use a single worker or move it to a shared store.
5. **Back up** `instance/bloodlink.db`.
6. Hospital data and patient searches can be sensitive. The app stores no patient information and does not log searches; keep it that way. Check applicable Indian rules on health data and blood bank regulation before launch. This README is not legal advice.

You can also look at **eRaktKosh**, India's national blood bank portal, as a source or integration target for licensed blood banks. Confirm its access terms yourself.

## Files

```
app.py            Flask app: routes, validation, auth, API
seed.py           Fictional demo data + credential generation
test_app.py       23 end-to-end tests
templates/        base, index (search), login, staff_dashboard, admin, error
static/           search.js, dashboard.js, common.js, style.css, vendor/ (Bootstrap)
instance/         created at runtime: bloodlink.db, secret_key, demo_credentials.txt
```

## Database

- `hospitals(id, name, kind, address, area, lat, lng, phone, username, password_hash, api_key_hash, verified, created_at)`
- `blood_stock(hospital_id, blood_group, units, updated_at)`: one row per hospital per group
- `stock_log(...)`: history of every change and its source (dashboard / csv / api)
- `admins(username, password_hash)`
