"""„Echte Projekte neu holen“: Kopie der Produktivdatenbank (Docker 2) in die Datenbank dieser Instanz.

Ablauf (nur Administrator, läuft im Hintergrund):
  1. per SSH auf Docker 2: pg_dump im Datenbank-Container → Datei hier (die Produktion wird nur gelesen)
  2. pg_restore --clean in die eigene Datenbank (eigene Tabellen kia_… bleiben, sie sind nicht in der Sicherung)
  3. Außenverbindungen trennen – sonst würde die Kopie mit echten Zugängen arbeiten:
     Microsoft 365 / IMAP (Posteingang würde Mails abholen), Bauherrenportal (würde echte Freigaben hinausschieben),
     Mail-Kennwort. Mails, Termine und die Ablage sperrt zusätzlich TESTSERVER=1.
Die Sitzungen werden mitkopiert: angemeldet wird man mit dem gewohnten Namen und Kennwort.
"""
import os
import shlex
import subprocess
import threading
import time

ZUSTAND = {"laeuft": False, "schritt": "", "fehler": "", "fertig": ""}

TRENNEN = [
    "DELETE FROM settings WHERE key IN ('m365_tenant','m365_client','m365_secret','mail_pass')",
    "DELETE FROM bp_store WHERE key IN ('bauherren:einst','bauherren:schluessel') OR key LIKE 'postfaecher:%'",
]


def befehl_dump():
    ziel = os.environ.get("PROD_SSH", "").strip()
    if not ziel:
        raise RuntimeError("PROD_SSH ist nicht eingetragen (Portainer → Stack → Umgebungsvariablen)")
    cont = os.environ.get("PROD_DB_CONTAINER", "bauplattform_db")
    innen = f"docker exec {shlex.quote(cont)} pg_dump -U bautagebuch -Fc bautagebuch"
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", ziel, innen]


def _lauf(dsn, db_tx):
    datei = "/tmp/produktion.dump"
    try:
        ZUSTAND.update(schritt="hole Sicherung von Docker 2 …", fehler="")
        with open(datei, "wb") as f:
            p = subprocess.run(befehl_dump(), stdout=f, stderr=subprocess.PIPE, timeout=3600)
        if p.returncode or os.path.getsize(datei) < 1000:
            raise RuntimeError("Sicherung fehlgeschlagen: " + p.stderr.decode("utf-8", "replace")[-400:])
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
    befehl_dump()   # prüft PROD_SSH vorab – Fehler gleich an die Oberfläche
    ZUSTAND["laeuft"] = True
    threading.Thread(target=_lauf, args=(dsn, db_tx), daemon=True).start()
    return True


if __name__ == "__main__":   # erste Kopie ohne Oberfläche: docker exec bauportal_ki_agent python -m ki.kopie
    from bpcore import db
    ZUSTAND["laeuft"] = True
    _lauf(db.DSN, db.tx)
    print(ZUSTAND["fehler"] or f"Kopie fertig ({ZUSTAND['fertig']})")
