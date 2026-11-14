"""Demo data for BloodLink Mangaluru.

EVERYTHING HERE IS FICTIONAL SAMPLE DATA for demonstration:
  * stock counts are invented,
  * phone numbers are placeholders (not real),
  * coordinates are approximate.
Replace it with real, verified hospital records before any real-world use.
"""
import os
import secrets
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from app import BLOOD_GROUPS, db_connect, hash_key, new_api_key, set_stock

DEMO_HOSPITALS = [
    {
        "name": "Father Muller Medical College Hospital", "kind": "Hospital",
        "address": "Kankanady, Mangaluru", "area": "Kankanady",
        "lat": 12.8667, "lng": 74.8546, "phone": "+91 824 000 0001",
        "username": "fathermuller", "minutes_ago": 12,
        "stock": {"A+": 6, "A-": 1, "B+": 8, "B-": 0, "AB+": 3, "AB-": 0, "O+": 10, "O-": 2},
    },
    {
        "name": "Wenlock District Hospital", "kind": "Hospital",
        "address": "Hampankatta, Mangaluru", "area": "Hampankatta",
        "lat": 12.8696, "lng": 74.8426, "phone": "+91 824 000 0002",
        "username": "wenlock", "minutes_ago": 95,
        "stock": {"A+": 14, "A-": 2, "B+": 11, "B-": 1, "AB+": 4, "AB-": 1, "O+": 18, "O-": 3},
    },
    {
        "name": "KMC Hospital, Attavar", "kind": "Hospital",
        "address": "Attavar, Mangaluru", "area": "Attavar",
        "lat": 12.8725, "lng": 74.8415, "phone": "+91 824 000 0003",
        "username": "kmcattavar", "minutes_ago": 40,
        "stock": {"A+": 0, "A-": 0, "B+": 5, "B-": 0, "AB+": 2, "AB-": 0, "O+": 7, "O-": 1},
    },
    {
        "name": "A.J. Hospital & Research Centre", "kind": "Hospital",
        "address": "Kuntikana, Mangaluru", "area": "Kuntikana",
        "lat": 12.8919, "lng": 74.8453, "phone": "+91 824 000 0004",
        "username": "ajhospital", "minutes_ago": 5 * 60,
        "stock": {"A+": 4, "A-": 0, "B+": 6, "B-": 1, "AB+": 1, "AB-": 0, "O+": 9, "O-": 0},
    },
    {
        "name": "Yenepoya Medical College Hospital", "kind": "Hospital",
        "address": "Deralakatte, Mangaluru", "area": "Deralakatte",
        "lat": 12.8093, "lng": 74.8664, "phone": "+91 824 000 0005",
        "username": "yenepoya", "minutes_ago": 3 * 60,
        "stock": {"A+": 9, "A-": 1, "B+": 7, "B-": 0, "AB+": 2, "AB-": 1, "O+": 12, "O-": 2},
    },
    {
        "name": "Indiana Hospital & Heart Institute", "kind": "Hospital",
        "address": "Pumpwell, Mangaluru", "area": "Pumpwell",
        "lat": 12.8641, "lng": 74.8660, "phone": "+91 824 000 0006",
        "username": "indiana", "minutes_ago": 30 * 60,   # deliberately stale
        "stock": {"A+": 2, "A-": 0, "B+": 3, "B-": 0, "AB+": 0, "AB-": 0, "O+": 4, "O-": 1},
    },
    {
        "name": "K.S. Hegde Medical Academy Hospital", "kind": "Hospital",
        "address": "Deralakatte, Mangaluru", "area": "Deralakatte",
        "lat": 12.8113, "lng": 74.8613, "phone": "+91 824 000 0007",
        "username": "kshegde", "minutes_ago": None,     # has never reported any stock
        "stock": {},
    },
    {
        "name": "Srinivas Institute of Medical Sciences, Mukka", "kind": "Hospital",
        "address": "Mukka, Surathkal", "area": "Mukka",
        "lat": 12.9963, "lng": 74.7939, "phone": "+91 824 000 0008",
        "username": "srinivas", "minutes_ago": 20,
        "stock": {"A+": 7, "A-": 1, "B+": 5, "B-": 0, "AB+": 2, "AB-": 0, "O+": 8, "O-": 1},
    },
]


def seed_demo(db_path):
    """Insert demo data if the database has no hospitals yet.

    Returns a dict of generated credentials (or None if data already existed).
    Credentials are random and are also written to instance/demo_credentials.txt.
    """
    db = db_connect(db_path)
    try:
        if db.execute("SELECT COUNT(*) FROM hospitals").fetchone()[0]:
            return None

        now = datetime.now(timezone.utc)
        creds = {"admin": None, "hospitals": []}

        admin_pw = secrets.token_urlsafe(9)
        db.execute(
            "INSERT OR IGNORE INTO admins (username, password_hash) VALUES (?,?)",
            ("admin", generate_password_hash(admin_pw)),
        )
        creds["admin"] = ("admin", admin_pw)

        for h in DEMO_HOSPITALS:
            pw, api_key = secrets.token_urlsafe(9), new_api_key()
            cur = db.execute(
                "INSERT INTO hospitals (name, kind, address, area, lat, lng, phone, username, "
                "password_hash, api_key_hash, verified, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                (h["name"], h["kind"], h["address"], h["area"], h["lat"], h["lng"], h["phone"],
                 h["username"], generate_password_hash(pw), hash_key(api_key),
                 now.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            if h["minutes_ago"] is not None:
                stamp = (now - timedelta(minutes=h["minutes_ago"])).strftime("%Y-%m-%dT%H:%M:%SZ")
                for grp in BLOOD_GROUPS:
                    set_stock(db, cur.lastrowid, grp, h["stock"][grp], "seed", stamp)
            creds["hospitals"].append((h["name"], h["username"], pw, api_key))
        db.commit()
    finally:
        db.close()

    path = os.path.join(os.path.dirname(db_path), "demo_credentials.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("BloodLink demo credentials (generated once; DEMO DATA ONLY)\n\n")
        fh.write(f"ADMIN     username: {creds['admin'][0]}   password: {creds['admin'][1]}\n\n")
        for name, user, pw, key in creds["hospitals"]:
            fh.write(f"{name}\n  username: {user}   password: {pw}\n  API key : {key}\n\n")

    print("\n" + "=" * 64)
    print(" Demo data created (fictional stock numbers).")
    print(f" Admin login            : admin / {creds['admin'][1]}")
    fm = creds["hospitals"][0]
    print(f" Father Muller staff    : {fm[1]} / {fm[2]}")
    print(f" All credentials + keys : {path}")
    print("=" * 64 + "\n")
    return creds
