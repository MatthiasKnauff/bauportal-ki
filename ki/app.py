"""bauportal-ki – Oberfläche und Schnittstellen des Claude-Agenten (Port 8200).

Anmeldung: Cookie der Bauportal-Kopie (bp_sso, gilt für alle Ports des Servers). Ohne Anmeldung → Anmeldeseite der Kopie.
"""
import os
import threading

from flask import Flask, g, jsonify, redirect, request, send_from_directory

from bpcore import db, sso

from . import agent, kopie, werkzeuge

HIER = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.json.sort_keys = False
PORTAL = os.environ.get("PORTAL_URL") or ""

with db.tx() as _c:
    agent.einrichten(_c)


def _token():
    for t in sso.tokens(request):
        with db.tx() as c:
            if sso.lookup(c, t):
                return t
    return None


@app.before_request
def anmelden():
    if request.path.startswith("/static/") or request.path == "/api/health":
        return None
    tok = _token()
    if not tok:
        if request.path.startswith("/api/"):
            return jsonify(error="Nicht angemeldet"), 401
        ziel = PORTAL or f"{request.scheme}://{request.host.split(':')[0]}:{os.environ.get('PORTAL_PUBLISH', '8090')}"
        return redirect(f"{ziel}/?weiter={request.url}")
    with db.tx() as c:
        u = sso.lookup(c, tok)
    g.user, g.tok = dict(u), tok
    if request.method not in ("GET", "HEAD") and (o := request.headers.get("Origin")) and o.split("://", 1)[-1] != request.host:
        return jsonify(error="Anfrage von fremder Seite abgelehnt"), 403
    return None


def _bp():
    return werkzeuge.Bauportal(g.tok)


def _fehler(e, code=400):
    return jsonify(error=str(e)), code


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.get("/")
def start():
    return send_from_directory(os.path.join(HIER, "static"), "index.html")


@app.get("/static/<path:f>")
def statisch(f):
    return send_from_directory(os.path.join(HIER, "static"), f, max_age=0)


@app.get("/api/me")
def me():
    with db.tx() as c:
        return jsonify(name=g.user.get("name"), admin=bool(g.user.get("is_admin")), modell=agent.MODELL,
                       kosten=round(agent.monat(c), 2), deckel=agent.deckel(c), kopie=kopie.ZUSTAND,
                       kopie_moeglich=True)


@app.get("/api/gespraeche")
def gespraeche():
    with db.tx() as c:
        return jsonify([dict(r) for r in c.execute("SELECT id, titel, updated FROM kia_gespraech WHERE user_id=? ORDER BY updated DESC LIMIT 100",
                                                    (g.user["id"],)).fetchall()])


@app.post("/api/gespraeche")
def gespraech_neu():
    with db.tx() as c:
        i = c.execute("INSERT INTO kia_gespraech(user_id, titel, created, updated, verlauf, zuordnung, cache, offen) "
                      "VALUES(?, '', ?, ?, '[]', '{}', '{}', '[]') RETURNING id", (g.user["id"], agent._jetzt(), agent._jetzt())).fetchone()["id"]
    return jsonify(id=i)


@app.get("/api/gespraeche/<int:gid>")
def gespraech(gid):
    from . import schutz
    with db.tx() as c:
        try:
            r = agent.laden(c, gid, g.user["id"])
        except werkzeuge.Fehler as e:
            return _fehler(e, 404)
        tausch = schutz.Gespraech(db.tx, agent._j(r["zuordnung"], {}))
        schritte = [dict(x) for x in c.execute("SELECT id, status, ergebnis, erledigt, fehler FROM kia_schritt WHERE gespraech_id=? ORDER BY id", (gid,)).fetchall()]
        return jsonify(id=gid, titel=r["titel"], eintraege=agent.anzeige(agent._j(r["verlauf"], []), tausch), vorschau=agent.vorschau(r), schritte=schritte)


@app.delete("/api/gespraeche/<int:gid>")
def gespraech_weg(gid):
    with db.tx() as c:
        c.execute("DELETE FROM kia_gespraech WHERE id=? AND user_id=?", (gid, g.user["id"]))
    return jsonify(ok=True)


# Ein Zug dauert oft länger als ein Proxy wartet: er läuft im Hintergrund, die Seite fragt den Stand ab (ein Prozess, siehe Dockerfile).
ZUEGE = {}


def _zug_lauf(gid, user, tok, text):
    Z = ZUEGE[gid]
    try:
        agent.zug(db.tx, gid, user, werkzeuge.Bauportal(tok), text, melden=lambda t: Z["fortschritt"].append(t))
    except agent.Deckel as e:
        Z["fehler"] = str(e)
    except Exception as e:   # noqa: BLE001 – Meldung für die Oberfläche, nichts gespeichert
        Z["fehler"] = str(e)
    finally:
        Z["laeuft"] = False


@app.post("/api/gespraeche/<int:gid>/nachricht")
def nachricht(gid):
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:20000]
    if not text:
        return _fehler("Leere Nachricht")
    with db.tx() as c:
        try:
            agent.laden(c, gid, g.user["id"])
        except werkzeuge.Fehler as e:
            return _fehler(e, 404)
    if ZUEGE.get(gid, {}).get("laeuft"):
        return _fehler("Claude arbeitet noch an der letzten Nachricht")
    ZUEGE[gid] = {"laeuft": True, "fortschritt": [], "fehler": "", "text": text}
    threading.Thread(target=_zug_lauf, args=(gid, g.user, g.tok, text), daemon=True).start()
    return jsonify(laeuft=True)


@app.get("/api/gespraeche/<int:gid>/stand")
def stand(gid):
    Z = ZUEGE.get(gid) or {"laeuft": False, "fortschritt": [], "fehler": ""}
    return jsonify(laeuft=Z["laeuft"], fortschritt=Z["fortschritt"][-30:], fehler=Z["fehler"], text=Z.get("text", ""))


@app.post("/api/gespraeche/<int:gid>/ausfuehren")
def ausfuehren(gid):
    try:
        with db.tx() as c:
            return jsonify(agent.ausfuehren(c, gid, g.user, _bp()))
    except werkzeuge.Fehler as e:
        return _fehler(e)


@app.post("/api/gespraeche/<int:gid>/verwerfen")
def verwerfen(gid):
    with db.tx() as c:
        agent.verwerfen(c, gid, g.user)
    return jsonify(ok=True)


@app.post("/api/schritte/<int:sid>/rueckgaengig")
def rueckgaengig(sid):
    try:
        with db.tx() as c:
            f = agent.zuruecknehmen(c, sid, g.user, _bp())
        return jsonify(ok=not f, fehler=f)
    except werkzeuge.Fehler as e:
        return _fehler(e)


@app.put("/api/deckel")
def deckel_setzen():
    if not g.user.get("is_admin"):
        return _fehler("Nur für Administratoren", 403)
    try:
        v = max(0.0, min(10000.0, float((request.get_json(silent=True) or {}).get("eur"))))
    except (TypeError, ValueError):
        return _fehler("Betrag fehlt")
    with db.tx() as c:
        c.execute("INSERT INTO bp_store(key, data, updated, updated_by) VALUES('kia:deckel', ?, ?, ?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                  (str(v), agent._jetzt(), g.user["name"]))
    return jsonify(deckel=v)


@app.post("/api/kopie")
def kopie_holen():
    if not g.user.get("is_admin"):
        return _fehler("Nur für Administratoren", 403)
    try:
        return jsonify(gestartet=kopie.starten(db.DSN, db.tx), zustand=kopie.ZUSTAND)
    except RuntimeError as e:
        return _fehler(e)
