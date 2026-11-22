"""Rauchtest der Oberfläche/Schnittstellen (Flask-Testclient) – Datenbank, Anmeldung und Claude sind Attrappen."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_ki  # noqa: E402  (stellt Pfade und psycopg-Hülle bereit)

from ki import schutz  # noqa: E402


@unittest.skipUnless(schutz._bp, "Bauportal-Kern (BPCORE) nicht gefunden")
class App(unittest.TestCase):
    def setUp(self):
        from bpcore import db, sso
        import bpcore.ki_verbrauch as kv
        self.con, tx = test_ki.datenbank()
        self.addCleanup(self.con.close)
        from ki import agent, werkzeuge
        self.bp = test_ki.BauportalAttrappe()
        for mod, name, wert in ((kv, "merke", lambda *a: None), (db, "tx", tx), (werkzeuge, "Bauportal", lambda tok: self.bp),
                                (sso, "lookup", lambda c, t: {"id": 1, "name": "mknauff", "is_admin": 1} if t == "gut" else None),
                                (agent, "zug", agent.zug)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, wert)
        from ki import app as ki_app
        self.agent, self.c = agent, ki_app.app.test_client()
        self.c.set_cookie("bp_sso", "gut")

    def test_ablauf(self):
        anon = self.c.__class__(self.c.application)
        self.assertEqual(anon.get("/api/me").status_code, 401)
        self.assertEqual(self.c.get("/api/me").json["name"], "mknauff")
        gid = self.c.post("/api/gespraeche").json["id"]
        antworten = iter([[test_ki.tu(1, "lv_anlegen", {"projekt_id": 7, "name": "Dach"})], [{"type": "text", "text": "Vorgemerkt."}]])
        zug = self.agent.zug
        self.agent.zug = lambda *a, **k: zug(*a, **{**k, "claude": lambda n: {"content": (x := next(antworten)), "usage": {},
                                                                               "stop_reason": "tool_use" if x[0]["type"] == "tool_use" else "end_turn"}})
        self.assertTrue(self.c.post(f"/api/gespraeche/{gid}/nachricht", json={"text": "LV Dach bitte"}).json["laeuft"])
        for _ in range(50):
            s = self.c.get(f"/api/gespraeche/{gid}/stand").json
            if not s["laeuft"]:
                break
            time.sleep(0.05)
        self.assertEqual(s["fehler"], "")
        g = self.c.get(f"/api/gespraeche/{gid}").json
        self.assertEqual(g["vorschau"][0]["titel"], "Neues LV „Dach“")
        self.assertEqual([e["wer"] for e in g["eintraege"]], ["ich", "werkzeug", "claude"])
        r = self.c.post(f"/api/gespraeche/{gid}/ausfuehren").json
        self.assertEqual(len(self.bp.lv), 2)
        self.assertTrue(self.c.post(f"/api/schritte/{r['schritt']}/rueckgaengig").json["ok"])
        self.assertEqual(len(self.bp.lv), 1)
        self.assertEqual(self.c.put("/api/deckel", json={"eur": 20}).json["deckel"], 20)
        r = self.c.get("/")
        self.assertIn("Bauportal", r.get_data(as_text=True))
        r.close()


if __name__ == "__main__":
    unittest.main()
