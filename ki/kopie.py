"""„Echte Projekte neu holen“: die neueste Datenbanksicherung des Bauportals vom Dateiserver in diese Instanz einspielen.

Das Bauportal (Docker 2) legt seine Sicherungen ohnehin auf den Dateiserver (Verwaltung → Sicherung → „Kopie auf dem Dateiserver“,
Ordner …\\Datenbank\\bau-JJJJMMTT-HHMMSS-<art>.dump). Von dort liest diese Instanz – mit dem Baustein des Bauportals (bautagebuch/core/remote.py),
nur lesend. Kein Zugang zu Docker 2 nötig. Stand = letzte Sicherung (nachts; für einen frischeren Stand im Bauportal „jetzt sichern“).

Ablauf (nur Administrator, läuft im Hintergrund):
  1. neueste bau-*.dump aus dem Ordner „Datenbank“ der Sicherungs-Freigabe lesen
  2. pg_restore --clean in die eigene Datenbank (eigene Tabellen kia_… bleiben, sie sind nicht in der Sicherung)
  3. Außenverbindungen trennen – sonst würde die Kopie mit echten Zugängen arbeiten:
     Microsoft 365 / IMAP (Posteingang würde Mails abholen), Bauherrenportal (würde echte Freigaben hinausschieben),
     Mail-Kennwort. Mails, Termine und Schreiben auf den Dateiserver sperrt zusätzlich TESTSERVER=1.
Wo die Sicherungen liegen: Umgebungsvariablen KOPIE_PFAD (\\\\Server\\Freigabe\\Ordner, wie im Bauportal eingetragen), KOPIE_BENUTZER,
KOPIE_KENNWORT, KOPIE_DOMAIN – für die erste Kopie nötig. Danach kennt die Kopie Pfad und Zugang selbst (dieselben Einstellungen wie das Bauportal).
Die Sitzungen werden mitkopiert: angemeldet wird man mit dem gewohnten Namen und Kennwort.
"""
import os
import re
import subprocess
import sys
import threading
import time

ZUSTAND = {"laeuft": False, "schritt": "", "fehler": "", "fertig": "", "sicherung": ""}
NAME = re.compile(r"^bau-\d{8}-\d{6}-[a-z0-9-]+\.dump$")   # wie bautagebuch/core/backups.NAME

TRENNEN = [
    "DELETE FROM settings WHERE key IN ('m365_tenant','m365_client','m365_secret','mail_pass')",
    "DELETE FROM bp_store WHERE key IN ('bauherren:einst','bauherren:schluessel') OR key LIKE 'postfaecher:%'",
]


def _zugang(db_tx):
    """Pfad und Zugang: aus den Umgebungsvariablen, sonst aus den (mitkopierten) Einstellungen des Bauportals."""
    conf = {}
    try:
        with db_tx() as c:
            conf = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings WHERE key IN "
                                                            "('backup_remote','store_user','store_pass','store_domain')").fetchall()}
    except Exception:   # noqa: BLE001 – leere Datenbank vor der ersten Kopie
        pass
    E = os.environ.get
    return {"pfad": E("KOPIE_PFAD") or conf.get("backup_remote", ""), "user": E("KOPIE_BENUTZER") or conf.get("store_user", ""),
            "pass": E("KOPIE_KENNWORT") or conf.get("store_pass", ""), "domain": E("KOPIE_DOMAIN") or conf.get("store_domain", "")}


def quelle(db_tx):
    """Ordner der Sicherungen als Ziel-Objekt des Bauportals (Smb oder eingebundener Ordner)."""
    z = _zugang(db_tx)
    if not z["pfad"]:
        raise RuntimeError("Es ist nicht eingetragen, wo die Sicherungen des Bauportals liegen (KOPIE_PFAD in Portainer).")
    if "/app/bautagebuch" not in sys.path:
        sys.path.insert(0, "/app/bautagebuch")
    from core import remote
    m = re.match(r"^\\\\([^\\]+)\\([^\\]+)\\?(.*)$", z["pfad"].strip().replace("/", "\\"))
    if m:
        return remote.Smb(m.group(1), m.group(2), m.group(3).replace("\\", "/").strip("/"), z["user"], z["pass"], z["domain"])
    if z["pfad"].startswith("/"):
        return remote.Folder(z["pfad"])
    raise RuntimeError("KOPIE_PFAD bitte als \\\\Server\\Freigabe\\Ordner eintragen.")


def _lauf(dsn, db_tx):
    datei = "/tmp/produktion.dump"
    try:
        ZUSTAND.update(schritt="suche die neueste Sicherung …", fehler="")
        q = quelle(db_tx)
        namen = sorted(e["name"] for e in q.listdir("Datenbank") if not e["dir"] and NAME.match(e["name"]))
        if not namen:
            raise RuntimeError("Im Ordner „Datenbank“ der Sicherung liegt keine bau-…dump.")
        ZUSTAND.update(schritt=f"hole {namen[-1]} …", sicherung=namen[-1])
        with open(datei, "wb") as f:
            f.write(q.read("Datenbank/" + namen[-1]))
        ZUSTAND["schritt"] = "spiele die Kopie ein …"
        p = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "-d", dsn, datei],
                           stderr=subprocess.PIPE, timeout=3600, env={**os.environ})
        fehler = [z for z in p.stderr.decode("utf-8", "replace").splitlines() if "error" in z.lower() and "does not exist" not in z]
        if p.returncode and len(fehler) > 5:
            raise RuntimeError("Einspielen fehlgeschlagen: " + " | ".join(fehler[:3]))
        ZUSTAND["schritt"] = "trenne Außenverbindungen …"
        with db_tx() as c:
            for s in TRENNEN:
                c.execute(s)
        ZUSTAND.update(fertig=time.strftime("%d.%m.%Y %H:%M"), schritt="")
    except Exception as e:   # noqa: BLE001 – Meldung für die Oberfläche
        ZUSTAND.update(fehler=str(e), schritt="")
    finally:
        ZUSTAND["laeuft"] = False
        try:
            os.remove(datei)
        except OSError:
            pass


def starten(dsn, db_tx):
    if ZUSTAND["laeuft"]:
        return False
    if not _zugang(db_tx)["pfad"]:   # gleich an die Oberfläche melden
        raise RuntimeError("Es ist nicht eingetragen, wo die Sicherungen des Bauportals liegen (KOPIE_PFAD in Portainer).")
    ZUSTAND["laeuft"] = True
    threading.Thread(target=_lauf, args=(dsn, db_tx), daemon=True).start()
    return True


if __name__ == "__main__":   # erste Kopie ohne Oberfläche: docker exec bauportal_ki_agent python -m ki.kopie
    from bpcore import db
    ZUSTAND["laeuft"] = True
    _lauf(db.DSN, db.tx)
    print(ZUSTAND["fehler"] or f"Kopie fertig ({ZUSTAND['fertig']}, {ZUSTAND['sicherung']})")
