"""Tests des Claude-Agenten ohne Claude und ohne Bauportal: Attrappen für beide, SQLite statt PostgreSQL.

Der Kern des Bauportals (bpcore.anonym, bpcore.ki_verbrauch) wird ECHT benutzt, wenn er gefunden wird
(Umgebungsvariable BPCORE = Ordner shared/ des Bauportal-Repos; im Workflow ausgecheckt).
"""
import contextlib
import copy
import json
import os
import sqlite3
import sys
import types
import unittest

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HIER))
if os.environ.get("BPCORE"):
    sys.path.insert(0, os.environ["BPCORE"])

# bpcore.db braucht psycopg – für die Tests eine leere Hülle, alles andere vom echten Kern
if "psycopg" not in sys.modules:
    sys.modules["psycopg"] = types.SimpleNamespace(OperationalError=Exception, IntegrityError=Exception, connect=None)

from ki import agent, schutz, werkzeuge  # noqa: E402


# ------------------------------------------------------------------------------------------------ Attrappen
def datenbank():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, client TEXT, street TEXT, zip TEXT, city TEXT, updated TEXT);
    CREATE TABLE contacts (id INTEGER PRIMARY KEY, name TEXT, trade TEXT, art TEXT, contact_person TEXT, phone TEXT, mobile TEXT,
        email TEXT, street TEXT, extra TEXT, updated TEXT);
    CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, vorname TEXT, nachname TEXT, email TEXT, updated TEXT);
    CREATE TABLE bp_store (key TEXT PRIMARY KEY, data TEXT, updated TEXT, updated_by TEXT);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE ki_protokoll (zeit TEXT, wer TEXT, app TEXT, anbieter TEXT, zweck TEXT, gesendet TEXT, antwort TEXT, ersetzt TEXT, bild INT, dauer_ms INT);
    CREATE TABLE ki_verbrauch (tag TEXT, anbieter TEXT, modell TEXT, ein INT, aus INT, n INT, PRIMARY KEY (tag, anbieter, modell));
    INSERT INTO projects VALUES (7, 'P123 Erweiterung Tierheim Hafenstraße', 'Tierschutzverein Rheinufer e.V.', 'Grillostraße 24', '45141', 'Essen', '1');
    INSERT INTO contacts VALUES (3, 'Bauunternehmung Wilbers GmbH', 'Rohbau', '', 'Karl Wilbers', '0201 123456', '', 'info@wilbers-bau.de', 'Am Hang 3', '{}', '1');
    INSERT INTO users VALUES (1, 'mknauff', 'Matthias', 'Knauff', 'mk@gk3.de', '1');
    """)
    con.executescript(agent.SCHEMA.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY"))

    @contextlib.contextmanager
    def tx():
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
    return con, tx


class BauportalAttrappe:
    """Antwortet wie die AVA-Schnittstellen der Bauportal-Kopie, Daten im Speicher."""

    def __init__(self):
        self.lv = {11: {"id": 11, "project_id": 7, "nr": "01", "name": "Rohbau", "gewerk": "Rohbau", "rev": 1, "angebote": [], "vergabe": {},
                        "data": {"items": [{"id": "t1", "typ": "titel", "lvl": 1, "oz": "01", "kurz": "Erdarbeiten"},
                                           {"id": "p1", "typ": "pos", "oz": "01.0010", "kurz": "Oberboden abtragen", "lang": "Oberboden …", "menge": 120, "einheit": "m2", "art": "N"}],
                                 "vorbem": ""}}}
        self.papierkorb, self.aufrufe, self.naechste = [], [], 50
        self.kat = {"id": 900, "typ": "pos", "oz": "012.0100", "kurz": "Baugrube ausheben BK 3-5", "lang": "Langtext aus den Baudaten", "einheit": "m3", "pdb": 900}

    def __call__(self, m, pfad, daten=None):
        self.aufrufe.append((m, pfad, copy.deepcopy(daten)))
        if m == "GET" and pfad == "/api/projekte":
            return [{"id": 7, "name": "P123 Erweiterung Tierheim Hafenstraße", "archived": False}]
        if m == "GET" and pfad.startswith("/api/lv?project_id="):
            return [{"id": i, "nr": x["nr"], "name": x["name"], "gewerk": x["gewerk"]} for i, x in self.lv.items()]
        if m == "GET" and pfad == "/api/baudaten/kataloge":
            return [{"key": "gk3", "name": "Eigene", "n": 1}]
        if m == "GET" and pfad.startswith("/api/katalog/bd-gk3/suche"):
            return {"data": {"items": [{"id": "g01", "typ": "titel", "kurz": "Erdarbeiten"}, dict(self.kat)]}}
        if pfad.startswith("/api/lv/"):
            i = int(pfad.rsplit("/", 1)[1])
            if i not in self.lv:
                raise werkzeuge.Fehler("Leistungsverzeichnis nicht gefunden")
            if m == "GET":
                return copy.deepcopy(self.lv[i])
            if m == "DELETE":
                self.papierkorb.append(self.lv.pop(i))
                return {"ok": True}
            if m == "PUT":
                x = self.lv[i]
                if "rev" in daten and daten["rev"] != x["rev"] and not daten.get("force"):
                    raise werkzeuge.Fehler("Inzwischen geändert")
                if "data" in daten:
                    x["data"] = copy.deepcopy(daten["data"])
                    if daten.get("nummerieren"):
                        for k, z in enumerate(x["data"]["items"]):
                            z["oz"] = z.get("oz") or f"N{k}"
                for k in ("name", "gewerk", "nr"):
                    if daten.get(k):
                        x[k] = daten[k]
                x["rev"] += 1
                return copy.deepcopy(x)
        if m == "POST" and pfad == "/api/lv":
            self.naechste += 1
            self.lv[self.naechste] = {"id": self.naechste, "project_id": daten["project_id"], "nr": "02", "name": daten["name"], "gewerk": daten.get("gewerk", ""),
                                      "rev": 1, "angebote": [], "vergabe": {}, "data": {"items": [], "vorbem": ""}}
            return copy.deepcopy(self.lv[self.naechste])
        raise werkzeuge.Fehler(f"unbekannt: {m} {pfad}")


def claude_nach_drehbuch(drehbuch, gesehen):
    """Claude-Attrappe: gibt der Reihe nach die vorbereiteten Antworten zurück und merkt sich, was gesendet wurde."""
    it = iter(drehbuch)

    def f(nachrichten):
        gesehen.append(json.dumps(nachrichten, ensure_ascii=False))
        inhalt = next(it)(nachrichten) if callable(drehbuch[0]) else next(it)
        werkzeug = any(b.get("type") == "tool_use" for b in inhalt)
        return {"content": inhalt, "stop_reason": "tool_use" if werkzeug else "end_turn", "usage": {"input_tokens": 1000, "output_tokens": 200}}
    return f


def tu(i, name, eingabe):
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": eingabe}


# ------------------------------------------------------------------------------------------------ Tests
class Werkzeuge(unittest.TestCase):
    def setUp(self):
        self.bp, self.cache, self.schritt = BauportalAttrappe(), {}, []

    def test_lesen_kurz_und_katalog_bleibt_im_buero(self):
        r = werkzeuge.lesen(self.bp, "katalog_suche", {"text": "Baugrube"}, self.cache)
        self.assertEqual(r["treffer"][0]["ref"], "kat:gk3:900")
        self.assertIn("kat:gk3:900", self.cache)   # ganzer Eintrag bleibt hier, Claude bekommt nur Verweis und Auszug

    def test_vormerken_aendert_nichts(self):
        werkzeuge.vormerken(self.bp, "positionen_loeschen", {"lv_id": 11, "ids": ["p1"]}, self.schritt, self.cache)
        self.assertEqual(len(self.bp.lv[11]["data"]["items"]), 2)
        self.assertFalse([a for a in self.bp.aufrufe if a[0] != "GET"])

    def test_vormerken_prueft_zeilen_und_platzhalter(self):
        with self.assertRaises(werkzeuge.Fehler):
            werkzeuge.vormerken(self.bp, "positionen_loeschen", {"lv_id": 11, "ids": ["gibtsnicht"]}, self.schritt, self.cache)
        with self.assertRaises(werkzeuge.Fehler):
            werkzeuge.vormerken(self.bp, "positionen_einfuegen", {"lv_id": "neu:3", "eintraege": []}, self.schritt, self.cache)
        with self.assertRaises(werkzeuge.Fehler):   # Katalogverweise nur aus einer Suche dieses Gesprächs
            werkzeuge.vormerken(self.bp, "positionen_einfuegen", {"lv_id": 11, "eintraege": [{"katalog_ref": "kat:x:1"}]}, self.schritt, self.cache)

    def test_neues_lv_mit_katalogposition_und_zurueck(self):
        werkzeuge.lesen(self.bp, "katalog_suche", {"text": "Baugrube"}, self.cache)
        werkzeuge.vormerken(self.bp, "lv_anlegen", {"projekt_id": 7, "name": "Erdarbeiten", "gewerk": "Erdbau"}, self.schritt, self.cache)
        werkzeuge.vormerken(self.bp, "positionen_einfuegen", {"lv_id": "neu:1", "eintraege": [
            {"typ": "titel", "kurz": "Baugrube"}, {"katalog_ref": "kat:gk3:900", "menge": 350}]}, self.schritt, self.cache)
        erg, rueck = werkzeuge.ausfuehren(self.bp, self.schritt, self.cache)
        neu = self.bp.lv[erg[0]["lv_id"]]
        pos = neu["data"]["items"][1]
        self.assertEqual((pos["kurz"], pos["lang"], pos["menge"], pos["pdb"]), ("Baugrube ausheben BK 3-5", "Langtext aus den Baudaten", 350, 900))
        self.assertTrue(pos["oz"])   # neues LV wird nummeriert
        self.assertEqual(werkzeuge.rueckgaengig(self.bp, rueck), [])
        self.assertNotIn(neu["id"], self.bp.lv)
        self.assertEqual(self.bp.papierkorb[-1]["id"], neu["id"])   # Papierkorb des Bauportals, nicht endgültig gelöscht

    def test_aendern_und_zurueck_stellt_inhalt_wieder_her(self):
        vorher = copy.deepcopy(self.bp.lv[11]["data"])
        werkzeuge.vormerken(self.bp, "positionen_aendern", {"lv_id": 11, "aenderungen": [{"id": "p1", "menge": 99}]}, self.schritt, self.cache)
        werkzeuge.vormerken(self.bp, "positionen_einfuegen", {"lv_id": 11, "nach_id": "t1", "eintraege": [{"kurz": "Neu", "menge": 1, "einheit": "St"}]}, self.schritt, self.cache)
        erg, rueck = werkzeuge.ausfuehren(self.bp, self.schritt, self.cache)
        items = self.bp.lv[11]["data"]["items"]
        self.assertEqual([x["kurz"] for x in items], ["Erdarbeiten", "Neu", "Oberboden abtragen"])
        self.assertEqual(items[2]["menge"], 99)
        self.assertEqual(len(rueck), 1)   # Inhalt des LV nur einmal gemerkt (Stand vor dem ganzen Schritt)
        werkzeuge.rueckgaengig(self.bp, rueck)
        self.assertEqual(self.bp.lv[11]["data"], vorher)

    def test_kein_nummerieren_wenn_lv_draussen_war(self):
        self.bp.lv[11]["angebote"] = [{"id": 1}]
        werkzeuge.vormerken(self.bp, "positionen_einfuegen", {"lv_id": 11, "eintraege": [{"kurz": "Neu"}]}, self.schritt, self.cache)
        werkzeuge.ausfuehren(self.bp, self.schritt, self.cache)
        put = [a for a in self.bp.aufrufe if a[0] == "PUT"][-1][2]
        self.assertFalse(put["nummerieren"])

    def test_fehler_mittendrin_nimmt_alles_zurueck(self):
        werkzeuge.vormerken(self.bp, "lv_anlegen", {"projekt_id": 7, "name": "X"}, self.schritt, self.cache)
        werkzeuge.vormerken(self.bp, "positionen_loeschen", {"lv_id": 11, "ids": ["p1"]}, self.schritt, self.cache)
        self.bp.lv[11]["rev"] = 5   # inzwischen hat jemand im Bauportal gespeichert
        orig = self.bp.__call__

        def stoeren(m, pfad, daten=None):
            if m == "PUT" and pfad == "/api/lv/11" and not daten.get("force"):
                raise werkzeuge.Fehler("Inzwischen von Kollegin geändert")
            return orig(m, pfad, daten)
        with self.assertRaises(werkzeuge.Fehler) as f:
            werkzeuge.ausfuehren(stoeren, self.schritt, self.cache)
        self.assertIn("zurückgenommen", str(f.exception))
        self.assertEqual(set(self.bp.lv), {11})   # neu angelegtes LV wieder weg


class Anonymisierung(unittest.TestCase):
    def setUp(self):
        self.con, self.tx = datenbank()
        self.addCleanup(self.con.close)

    def test_zusatzmuster_und_gespraechsweite_kennungen(self):
        t = schutz.Gespraech(self.tx)
        a = t.hin("Baustelle in 45141 Essen, Gemarkung Altendorf Flur 3, Pläne unter \\\\server\\Projekte\\P123, siehe https://x.de/a")
        for klar in ("45141 Essen", "Altendorf", "\\\\server", "https://x.de"):
            self.assertNotIn(klar, a)
        b = t.hin("Nochmal 45141 Essen")
        k = [x for x in t.k2t if t.k2t[x] == "45141 Essen"][0]
        self.assertIn(k, a)
        self.assertIn(k, b)   # dieselbe Kennung im ganzen Gespräch
        self.assertEqual(t.zurueck(b), "Nochmal 45141 Essen")

    def test_mengen_sind_keine_orte(self):
        t = schutz.Gespraech(self.tx)
        self.assertEqual(t.hin("insgesamt 12500 Euro und 45000 Stück"), "insgesamt 12500 Euro und 45000 Stück")

    @unittest.skipUnless(schutz._bp, "Bauportal-Kern (BPCORE) nicht gefunden")
    def test_namen_aus_der_datenbank(self):
        t = schutz.Gespraech(self.tx)
        a = t.hin("Firma Wilbers soll im Projekt Erweiterung Tierheim Hafenstraße anbieten, Herr Karl Wilbers ruft an: 0201 123456")
        for klar in ("Wilbers", "Tierheim Hafenstraße", "123456"):
            self.assertNotIn(klar, a)
        self.assertEqual(t.zurueck(a), "Firma Wilbers soll im Projekt Erweiterung Tierheim Hafenstraße anbieten, Herr Karl Wilbers ruft an: 0201 123456")
        self.assertEqual(t.zurueck_tief({"x": [a]}), {"x": [t.zurueck(a)]})


@unittest.skipUnless(schutz._bp, "Bauportal-Kern (BPCORE) nicht gefunden")
class Zug(unittest.TestCase):
    def setUp(self):
        self.con, self.tx = datenbank()
        self.addCleanup(self.con.close)
        self.gid = self.con.execute("INSERT INTO kia_gespraech(user_id, titel, verlauf, zuordnung, cache, offen) VALUES(1,'','[]','{}','{}','[]') RETURNING id").fetchone()[0]
        self.con.commit()
        self.bp = BauportalAttrappe()
        import bpcore.ki_verbrauch as kv
        kv.merke = lambda *a: None   # schreibt sonst über bpcore.db (PostgreSQL)

    def test_ganzer_zug_anonym_hin_klar_zurueck(self):
        gesehen = []

        def runde2(nachrichten):   # Claude benutzt die Kennung des Projekts aus dem Ergebnis
            erg = nachrichten[-1]["content"][0]["content"]
            kenn = [w for w in erg.replace('"', " ").split() if w.startswith("projekt#")][0]
            return [tu(2, "lv_anlegen", {"projekt_id": 7, "name": f"Rohbau für {kenn}"})]
        claude = claude_nach_drehbuch([lambda n: [tu(1, "projekte", {})], runde2,
                                       lambda n: [{"type": "text", "text": "Vorgemerkt – bitte prüfen und ausführen."}]], gesehen)
        neu = agent.zug(self.tx, self.gid, {"id": 1}, self.bp, "Leg für Tierheim Hafenstraße ein LV an, Firma Wilbers", claude=claude)
        alles = "".join(gesehen)
        for klar in ("Tierheim Hafenstraße", "Wilbers"):
            self.assertNotIn(klar, alles)   # nichts davon ging hinaus
        g = agent.laden(self.con, self.gid, 1)
        offen = json.loads(g["offen"])
        self.assertEqual(offen[0]["eingabe"]["name"], "Rohbau für P123 Erweiterung Tierheim Hafenstraße")   # Klartext für die Ausführung
        self.assertEqual(neu[0]["text"], "Leg für Tierheim Hafenstraße ein LV an, Firma Wilbers")   # Anzeige im Klartext
        self.assertEqual(self.con.execute("SELECT count(*) FROM ki_protokoll").fetchone()[0], 3)
        self.assertGreater(agent.monat(self.con), 0)
        self.assertEqual(len(self.bp.lv), 1)   # vorgemerkt, nicht angelegt
        r = agent.ausfuehren(self.con, self.gid, {"id": 1}, self.bp)
        self.assertEqual(len(self.bp.lv), 2)
        self.assertEqual(agent.zuruecknehmen(self.con, r["schritt"], {"id": 1}, self.bp), [])
        self.assertEqual(len(self.bp.lv), 1)

    def test_deckel(self):
        self.con.execute("INSERT INTO bp_store(key, data) VALUES('kia:deckel', '0')")
        with self.assertRaises(agent.Deckel):
            agent.zug(self.tx, self.gid, {"id": 1}, self.bp, "Hallo", claude=lambda n: self.fail("darf nicht senden"))


class Kopie(unittest.TestCase):
    """Neueste Sicherung vom Dateiserver – mit dem Baustein des Bauportals (remote.Folder statt SMB)."""

    def test_neueste_sicherung_und_trennen(self):
        import tempfile
        from ki import kopie
        if not os.environ.get("BPCORE"):
            self.skipTest("Bauportal nicht gefunden")
        sys.path.insert(0, os.path.join(os.path.dirname(os.environ["BPCORE"]), "bautagebuch"))
        con, tx = datenbank()
        self.addCleanup(con.close)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Datenbank"))
            for n in ("bau-20261120-020000-nacht.dump", "bau-20261122-020000-nacht.dump", "notiz.txt"):
                with open(os.path.join(d, "Datenbank", n), "wb") as f:
                    f.write(n.encode() * 100)
            os.environ["KOPIE_PFAD"] = d
            self.addCleanup(os.environ.pop, "KOPIE_PFAD")
            gesehen = []
            orig = kopie.subprocess.run

            def run(cmd, **k):
                with open(cmd[-1], "rb") as f:
                    gesehen.append(f.read()[:30])
                return types.SimpleNamespace(returncode=0, stderr=b"")
            kopie.subprocess.run = run
            self.addCleanup(setattr, kopie.subprocess, "run", orig)
            con.execute("INSERT INTO settings VALUES ('m365_secret', 'geheim'), ('mail_from', 'buero@gk3.de')")
            con.execute("INSERT INTO bp_store(key, data) VALUES ('bauherren:einst', '{}'), ('postfaecher:1', '{}'), ('anderes', '1')")
            kopie.ZUSTAND["laeuft"] = True
            kopie._lauf("dsn", tx)
        self.assertEqual(kopie.ZUSTAND["fehler"], "")
        self.assertEqual(kopie.ZUSTAND["sicherung"], "bau-20261122-020000-nacht.dump")
        self.assertTrue(gesehen[0].startswith(b"bau-20261122"))
        self.assertEqual([r[0] for r in con.execute("SELECT key FROM settings").fetchall()], ["mail_from"])
        self.assertEqual([r[0] for r in con.execute("SELECT key FROM bp_store").fetchall()], ["anderes"])


class Stack(unittest.TestCase):
    def test_stack_hat_schutzschalter(self):
        import yaml
        with open(os.path.join(os.path.dirname(HIER), "server", "stack.yml"), encoding="utf-8") as f:
            s = yaml.safe_load(f)
        ki = s["services"]["ki"]
        self.assertEqual(ki["environment"]["TESTSERVER"], "1")
        self.assertEqual(s["name"], "bauportal-ki")
        self.assertFalse([n for n, v in s["volumes"].items() if "test" in str(v.get("name", "")) or "bauplattform" in str(v.get("name", ""))])


if __name__ == "__main__":
    unittest.main()
