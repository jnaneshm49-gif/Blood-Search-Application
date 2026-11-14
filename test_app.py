"""End-to-end tests. Run:  python -m unittest -v test_app"""
import io
import os
import re
import tempfile
import unittest

from app import create_app, db_connect, normalize_group
from seed import seed_demo

MANGALURU = (12.8697, 74.8424)  # Hampankatta


def csrf_from(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html.decode()).group(1)


class BloodLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app(self.db_path, {"TESTING": True})
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            self.creds = seed_demo(self.db_path)
        self.client = self.app.test_client()
        _, self.fm_user, self.fm_pw, self.fm_key = self.creds["hospitals"][0]

    def tearDown(self):
        self.tmp.cleanup()

    # ---- helpers ----
    def search(self, group="A+", lat=MANGALURU[0], lng=MANGALURU[1], radius=25, client=None):
        c = client or self.client
        return c.get("/api/search", query_string={"group": group, "lat": lat, "lng": lng, "radius": radius})

    def login(self, client, path, user, pw):
        token = csrf_from(client.get(path).data)
        return client.post(path, data={"username": user, "password": pw, "csrf_token": token})

    def staff_client(self):
        c = self.app.test_client()
        r = self.login(c, "/staff/login", self.fm_user, self.fm_pw)
        self.assertEqual(r.status_code, 302)
        return c

    def token(self, client, path="/staff"):
        return csrf_from(client.get(path).data)

    # ---- public search ----
    def test_pages_load(self):
        for path in ("/", "/staff/login", "/admin/login", "/healthz"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_search_a_positive_sorted_and_flagged(self):
        data = self.search().get_json()
        self.assertEqual(data["group"], "A+")
        names = [r["name"] for r in data["results"]]
        avail = [r for r in data["results"] if r["availability"] == "available"]
        self.assertTrue(all(a["distance_km"] <= b["distance_km"]
                            for a, b in zip([r for r in avail if r["freshness"] != "stale"],
                                            [r for r in avail if r["freshness"] != "stale"][1:])))
        fm = next(r for r in data["results"] if r["name"].startswith("Father Muller"))
        self.assertEqual(fm["units"], 6)
        self.assertEqual(fm["freshness"], "fresh")
        self.assertIn("min ago", fm["updated_label"])
        # KMC has 0 A+ -> not "available"; K.S. Hegde never reported -> no_data
        kmc = next(r for r in data["results"] if r["name"].startswith("KMC"))
        self.assertEqual(kmc["availability"], "none")
        hegde = next(r for r in data["results"] if r["name"].startswith("K.S. Hegde"))
        self.assertEqual(hegde["availability"], "no_data")
        # stale (30 h old) available hospital must rank after every fresh one
        indiana = next(r for r in data["results"] if r["name"].startswith("Indiana"))
        self.assertEqual(indiana["freshness"], "stale")
        last_fresh = max(names.index(r["name"]) for r in avail if r["freshness"] != "stale")
        self.assertGreater(names.index(indiana["name"]), last_fresh)

    def test_radius_filters_far_hospitals(self):
        near = {r["name"] for r in self.search(radius=10).get_json()["results"]}
        far = {r["name"] for r in self.search(radius=50).get_json()["results"]}
        self.assertNotIn("Srinivas Institute of Medical Sciences, Mukka", near)
        self.assertIn("Srinivas Institute of Medical Sciences, Mukka", far)

    def test_search_validation(self):
        self.assertEqual(self.search(group="Z+").status_code, 400)
        self.assertEqual(self.client.get("/api/search?group=A%2B").status_code, 400)
        self.assertEqual(self.search(lat=999).status_code, 400)
        self.assertEqual(self.client.get("/api/search?group=A%2B&lat=x&lng=y").status_code, 400)

    def test_group_normalisation(self):
        self.assertEqual(normalize_group("a+"), "A+")
        self.assertEqual(normalize_group("A "), "A+")          # raw '+' decoded to space
        self.assertEqual(normalize_group("o negative"), "O-")
        self.assertEqual(normalize_group("AB POS"), "AB+")
        self.assertIsNone(normalize_group("C+"))
        self.assertEqual(self.client.get("/api/search?group=A+&lat=12.87&lng=74.84").status_code, 200)

    def test_unverified_hospital_hidden(self):
        db = db_connect(self.db_path)
        db.execute("UPDATE hospitals SET verified=0 WHERE username='fathermuller'")
        db.commit(); db.close()
        names = [r["name"] for r in self.search().get_json()["results"]]
        self.assertFalse(any(n.startswith("Father Muller") for n in names))

    def test_xss_payload_is_escaped_in_admin_page(self):
        db = db_connect(self.db_path)
        db.execute("UPDATE hospitals SET name=? WHERE username='wenlock'", ("<script>alert(1)</script>",))
        db.commit(); db.close()
        admin = self.app.test_client()
        self.login(admin, "/admin/login", *self.creds["admin"])
        html = admin.get("/admin").data.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    # ---- staff auth & dashboard ----
    def test_dashboard_requires_login(self):
        self.assertEqual(self.client.get("/staff").status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_login_wrong_password_and_throttle(self):
        c = self.app.test_client()
        for _ in range(5):
            r = self.login(c, "/staff/login", self.fm_user, "wrong")
            self.assertEqual(r.status_code, 401)
        r = self.login(c, "/staff/login", self.fm_user, self.fm_pw)   # correct, but locked
        self.assertEqual(r.status_code, 429)

    def test_post_without_csrf_rejected(self):
        c = self.staff_client()
        r = c.post("/staff/update", data={"units_A+": "9"})
        self.assertEqual(r.status_code, 400)

    def test_staff_update_changes_public_search(self):
        c = self.staff_client()
        tok = self.token(c)
        r = c.post("/staff/update", data={"csrf_token": tok, "units_A+": "11", "units_B-": "4"})
        self.assertEqual(r.status_code, 302)
        fm = next(x for x in self.search().get_json()["results"] if x["name"].startswith("Father Muller"))
        self.assertEqual(fm["units"], 11)
        self.assertEqual(fm["updated_label"], "just now")
        # untouched groups are unchanged
        o_pos = next(x for x in self.search(group="O+").get_json()["results"] if x["name"].startswith("Father Muller"))
        self.assertEqual(o_pos["units"], 10)

    def test_staff_update_rejects_bad_numbers_atomically(self):
        c = self.staff_client()
        tok = self.token(c)
        c.post("/staff/update", data={"csrf_token": tok, "units_A+": "99", "units_O+": "-3"})
        fm = next(x for x in self.search().get_json()["results"] if x["name"].startswith("Father Muller"))
        self.assertEqual(fm["units"], 6)  # nothing saved

    def test_csv_upload_ok_and_bad(self):
        c = self.staff_client()
        tok = self.token(c)
        good = io.BytesIO(b"blood_group,units\nA+,21\no-,5\n")
        c.post("/staff/upload", data={"csrf_token": tok, "file": (good, "stock.csv")},
               content_type="multipart/form-data")
        fm = next(x for x in self.search().get_json()["results"] if x["name"].startswith("Father Muller"))
        self.assertEqual(fm["units"], 21)

        bad = io.BytesIO(b"blood_group,units\nA+,abc\n")
        c.post("/staff/upload", data={"csrf_token": self.token(c), "file": (bad, "bad.csv")},
               content_type="multipart/form-data")
        fm = next(x for x in self.search().get_json()["results"] if x["name"].startswith("Father Muller"))
        self.assertEqual(fm["units"], 21)  # unchanged by the bad file

        nohdr = io.BytesIO(b"foo,bar\n1,2\n")
        r = c.post("/staff/upload", data={"csrf_token": self.token(c), "file": (nohdr, "x.csv")},
                   content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"headers", r.data)

    def test_sample_csv_download(self):
        c = self.staff_client()
        r = c.get("/staff/sample.csv")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"blood_group,units"))

    # ---- API push ----
    def test_api_push_requires_valid_key(self):
        body = {"stock": [{"blood_group": "A+", "units": 3}]}
        self.assertEqual(self.client.post("/api/stock", json=body).status_code, 401)
        self.assertEqual(self.client.post("/api/stock", json=body, headers={"X-API-Key": "nope"}).status_code, 401)

    def test_api_push_updates_and_validates(self):
        h = {"X-API-Key": self.fm_key}
        r = self.client.post("/api/stock", json={"stock": [{"blood_group": "A+", "units": 17}]}, headers=h)
        self.assertEqual(r.status_code, 200)
        fm = next(x for x in self.search().get_json()["results"] if x["name"].startswith("Father Muller"))
        self.assertEqual(fm["units"], 17)
        # dict form also accepted
        self.assertEqual(self.client.post("/api/stock", json={"stock": {"A+": 18}}, headers=h).status_code, 200)
        # invalid -> 400 and nothing saved
        r = self.client.post("/api/stock", json={"stock": [{"blood_group": "A+", "units": 5},
                                                           {"blood_group": "Q+", "units": 1}]}, headers=h)
        self.assertEqual(r.status_code, 400)
        fm = next(x for x in self.search().get_json()["results"] if x["name"].startswith("Father Muller"))
        self.assertEqual(fm["units"], 18)
        for bad in ({"stock": []}, {"nope": 1}, {"stock": [{"blood_group": "A+", "units": True}]},
                    {"stock": [{"blood_group": "A+", "units": 2.5}]}, {"stock": [{"blood_group": "A+", "units": 9999}]}):
            self.assertEqual(self.client.post("/api/stock", json=bad, headers=h).status_code, 400, bad)

    def test_api_get_own_stock(self):
        r = self.client.get("/api/stock", headers={"X-API-Key": self.fm_key})
        self.assertEqual(r.status_code, 200)
        self.assertEqual({s["blood_group"]: s["units"] for s in r.get_json()["stock"]}["A+"], 6)

    def test_api_key_regeneration_invalidates_old(self):
        c = self.staff_client()
        r = c.post("/staff/api-key", data={"csrf_token": self.token(c)})
        self.assertEqual(r.status_code, 200)
        new_key = re.search(r"bl_[A-Za-z0-9_-]+", r.data.decode()).group(0)
        body = {"stock": {"A+": 1}}
        self.assertEqual(self.client.post("/api/stock", json=body, headers={"X-API-Key": self.fm_key}).status_code, 401)
        self.assertEqual(self.client.post("/api/stock", json=body, headers={"X-API-Key": new_key}).status_code, 200)

    # ---- passwords ----
    def test_password_change(self):
        c = self.staff_client()
        c.post("/staff/password", data={"csrf_token": self.token(c), "current": self.fm_pw,
                                        "new": "brand-new-pass", "confirm": "brand-new-pass"})
        c2 = self.app.test_client()
        self.assertEqual(self.login(c2, "/staff/login", self.fm_user, self.fm_pw).status_code, 401)
        self.assertEqual(self.login(c2, "/staff/login", self.fm_user, "brand-new-pass").status_code, 302)

    # ---- admin ----
    def test_admin_add_verify_reset(self):
        a = self.app.test_client()
        self.assertEqual(self.login(a, "/admin/login", *self.creds["admin"]).status_code, 302)
        tok = self.token(a, "/admin")
        form = {"csrf_token": tok, "name": "Test Blood Bank", "kind": "Blood bank", "address": "x", "area": "Bejai",
                "lat": "12.886", "lng": "74.842", "phone": "+91 824 111 2222",
                "username": "testbank", "password": "longenough1"}
        self.assertEqual(a.post("/admin/hospitals", data=form).status_code, 302)       # unverified
        self.assertEqual(a.post("/admin/hospitals", data=form).status_code, 400)       # duplicate username
        bad = dict(form, username="other", lat="500")
        self.assertEqual(a.post("/admin/hospitals", data=bad).status_code, 400)

        # unverified -> hidden even if it had stock
        s = self.app.test_client()
        self.assertEqual(self.login(s, "/staff/login", "testbank", "longenough1").status_code, 302)
        s.post("/staff/update", data={"csrf_token": self.token(s), "units_A+": "5"})
        self.assertFalse(any(r["name"] == "Test Blood Bank" for r in self.search().get_json()["results"]))

        conn = db_connect(self.db_path)
        hid = conn.execute("SELECT id FROM hospitals WHERE username='testbank'").fetchone()[0]
        conn.close()
        a.post(f"/admin/hospitals/{hid}/verify", data={"csrf_token": self.token(a, "/admin")})
        self.assertTrue(any(r["name"] == "Test Blood Bank" for r in self.search().get_json()["results"]))

        r = a.post(f"/admin/hospitals/{hid}/reset-password", data={"csrf_token": self.token(a, "/admin")})
        self.assertIn(b"Temporary password", r.data)

    def test_hospital_cannot_open_admin(self):
        c = self.staff_client()
        self.assertEqual(c.get("/admin").status_code, 302)

    def test_security_headers(self):
        r = self.client.get("/")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.search().headers["Cache-Control"], "no-store")

    def test_stock_log_written(self):
        c = self.staff_client()
        c.post("/staff/update", data={"csrf_token": self.token(c), "units_A+": "7"})
        html = c.get("/staff").data.decode()
        self.assertIn("6 &rarr; 7", html)


if __name__ == "__main__":
    unittest.main()
