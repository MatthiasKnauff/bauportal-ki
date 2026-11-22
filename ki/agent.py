"""Claude-Agent: ein Zug = Nachricht der Person → Claude liest und merkt vor (beliebig viele Werkzeug-Runden) → Antwort + Arbeitsschritt.

Gespeichert wird in der Datenbank der Bauportal-Kopie, in eigenen Tabellen (kia_…), damit „Echte Projekte neu holen“ sie nicht löscht.
Was an Claude geht, ist anonymisiert (schutz.py); der Verlauf wird so gespeichert, wie er gesendet wurde, und erst für die Anzeige
zurückübersetzt.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date

from . import schutz, werkzeuge

API = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"
MODELL = os.environ.get("KI_MODELL", "claude-sonnet-5-5")
RUNDEN = 25            # höchstens so viele Werkzeug-Runden je Zug
MAX_VERLAUF = 60       # Nachrichten, die mitgeschickt werden (ältere fallen aus dem Gespräch, bleiben aber gespeichert)

SYSTEM = """Du arbeitest im Bauportal eines Architekturbüros (GK3, Essen) mit – in der Ausschreibung (AVA): Leistungsverzeichnisse (LV) anlegen,
Positionen einfügen und ändern, LVs aus dem Gebäudemodell ableiten, Bestehendes prüfen.

So arbeitest du:
- Erst lesen, dann handeln: Projekt, vorhandene LVs und bei Bedarf das Gebäudemodell ansehen, bevor du etwas vormerkst.
- Positionen bevorzugt aus den Baudaten übernehmen (katalog_suche → katalog_ref). Eigene Texte nur, wenn es nichts Passendes gibt –
  dann knapp und fachlich korrekt (VOB/C, DIN 18299 ff.), keine Herstellernamen ohne „oder gleichwertig“.
- Mengen aus dem Gebäudemodell oder aus Angaben der Person; geschätzte Mengen in der Antwort als geschätzt kennzeichnen.
- Ändern geht nur über die VORMERKEN-Werkzeuge. Die Person sieht danach den ganzen Arbeitsschritt als Vorschau und führt ihn selbst aus.
  Schreibe deshalb nie „erledigt“ oder „angelegt“, sondern „vorgemerkt – bitte prüfen und ausführen“.
- Ein Arbeitsschritt = eine sinnvolle Einheit (z. B. ein LV mit seinen Positionen). Lieber mehrere kleine Schritte als einen riesigen.
- Unklarheiten, die das Ergebnis ändern (Gewerk, Umfang, Standard), kurz nachfragen statt raten.
- Antworte auf Deutsch, knapp, ohne Vorrede. Zahlen mit deutschem Komma.
"""


class Deckel(Exception):
    pass


# ------------------------------------------------------------------------------------------------ Tabellen
SCHEMA = """
CREATE TABLE IF NOT EXISTS kia_gespraech (id SERIAL PRIMARY KEY, user_id INTEGER, titel TEXT, created TEXT, updated TEXT,
    zuordnung TEXT, verlauf TEXT, cache TEXT, offen TEXT, hinweis TEXT);
CREATE TABLE IF NOT EXISTS kia_schritt (id SERIAL PRIMARY KEY, gespraech_id INTEGER, user_id INTEGER, status TEXT, aktionen TEXT,
    ergebnis TEXT, rueck TEXT, created TEXT, erledigt TEXT, fehler TEXT);
CREATE TABLE IF NOT EXISTS kia_kosten (tag TEXT, modell TEXT, ein BIGINT, aus BIGINT, eur DOUBLE PRECISION, PRIMARY KEY (tag, modell));
"""


def einrichten(c):
    for s in SCHEMA.strip().split(";"):
        if s.strip():
            c.execute(s)


def _j(v, d):
    try:
        return json.loads(v) if v else d
    except (TypeError, ValueError):
        return d


def _jetzt():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------------------------------------ Kosten
def deckel(c):
    r = c.execute("SELECT data FROM bp_store WHERE key='kia:deckel'").fetchone()
    try:
        return float(r["data"]) if r and r["data"] else float(os.environ.get("KI_DECKEL_EUR") or 50)
    except ValueError:
        return 50.0


def monat(c):
    r = c.execute("SELECT coalesce(sum(eur), 0) AS s FROM kia_kosten WHERE tag >= ?", (date.today().strftime("%Y-%m-01"),)).fetchone()
    return float(r["s"] or 0)


def _kosten_merken(c, modell, d):
    from bpcore import ki_verbrauch
    ein, aus = ki_verbrauch.tokens("claude", d)
    eur = ki_verbrauch.kosten("claude", modell, ein, aus)
    c.execute("INSERT INTO kia_kosten(tag, modell, ein, aus, eur) VALUES(?,?,?,?,?) ON CONFLICT (tag, modell) DO UPDATE SET "
              "ein=kia_kosten.ein+EXCLUDED.ein, aus=kia_kosten.aus+EXCLUDED.aus, eur=kia_kosten.eur+EXCLUDED.eur",
              (date.today().isoformat(), modell, ein, aus, eur))
    ki_verbrauch.merke("claude", modell, d)   # auch in der Verbrauchsübersicht der Verwaltung


# ------------------------------------------------------------------------------------------------ Claude
def _claude(nachrichten, schluessel=None):
    werk = [dict(w) for w in werkzeuge.WERKZEUGE]
    werk[-1]["cache_control"] = {"type": "ephemeral"}
    body = {"model": MODELL, "max_tokens": 8000, "tools": werk, "messages": nachrichten,
            "system": [{"type": "text", "text": SYSTEM + "\n" + schutz.HINWEIS, "cache_control": {"type": "ephemeral"}}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": schluessel or os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for versuch in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 529) and versuch < 2:
                time.sleep(4 * (versuch + 1))
                continue
            raise werkzeuge.Fehler(f"Claude antwortet mit {e.code}: {text}") from None


# ------------------------------------------------------------------------------------------------ ein Zug
def laden(c, gid, uid):
    r = c.execute("SELECT * FROM kia_gespraech WHERE id=? AND user_id=?", (gid, uid)).fetchone()
    if not r:
        raise werkzeuge.Fehler("Gespräch nicht gefunden")
    return r


def zug(tx, gid, user, bp, text, claude=_claude, melden=lambda t: None):
    """Nachricht verarbeiten. Gibt die neuen Anzeige-Einträge zurück.
    tx: Transaktion der Datenbank – je Schritt eine kurze (ein Zug dauert Minuten). claude ist austauschbar (Tests);
    melden(text) zeigt den Fortschritt."""
    with tx() as c:
        g = dict(laden(c, gid, user["id"]))
        if monat(c) >= deckel(c):
            raise Deckel(f"Monatsdeckel von {deckel(c):.0f} € erreicht")
    tausch = schutz.Gespraech(tx, _j(g["zuordnung"], {}))
    verlauf, cache, offen = _j(g["verlauf"], []), _j(g["cache"], {}), _j(g["offen"], [])
    hinweis = g["hinweis"] or ""
    raus = tausch.hin((hinweis + "\n\n" if hinweis else "") + text)
    if tausch.pruefen(raus):   # Schlusskontrolle: lieber nicht senden
        raise werkzeuge.Fehler("Anonymisierung unvollständig – nicht gesendet")
    verlauf.append({"role": "user", "content": raus})
    neu_ab = len(verlauf) - 1
    for _ in range(RUNDEN):
        t0 = time.time()
        d = claude(_fenster(verlauf))
        inhalt = d.get("content") or []
        verlauf.append({"role": "assistant", "content": inhalt})
        with tx() as c:   # Kosten und Protokoll sofort festhalten – auch wenn der Zug später abbricht
            _kosten_merken(c, MODELL, d)
            schutz.protokoll(c, "Agent", json.dumps(verlauf[-2]["content"], ensure_ascii=False)[:200000],
                             json.dumps(inhalt, ensure_ascii=False)[:100000], tausch.k2t, (time.time() - t0) * 1000)
            ueber = monat(c) >= deckel(c)
        if d.get("stop_reason") != "tool_use":
            break
        antworten = []
        for b in inhalt:
            if b.get("type") != "tool_use":
                continue
            name, a = b["name"], tausch.zurueck_tief(b.get("input") or {})
            melden(anzeige([{"role": "assistant", "content": [b]}], tausch)[0]["text"])
            try:
                if name in werkzeuge.VORMERKEN:
                    erg = werkzeuge.vormerken(bp, name, a, offen, cache)
                else:
                    erg = werkzeuge.kuerzen(json.dumps(werkzeuge.lesen(bp, name, a, cache), ensure_ascii=False, default=str))
                antworten.append({"type": "tool_result", "tool_use_id": b["id"], "content": tausch.hin(erg)})
            except Exception as e:   # noqa: BLE001 – Fehler gehen an Claude zurück, der Zug läuft weiter
                antworten.append({"type": "tool_result", "tool_use_id": b["id"], "content": tausch.hin(str(e)), "is_error": True})
        verlauf.append({"role": "user", "content": antworten})
        if ueber:
            verlauf.append({"role": "assistant", "content": [{"type": "text", "text": "Monatsdeckel erreicht – ich höre hier auf."}]})
            break
    else:   # Runden aufgebraucht: Verlauf muss mit einer Antwort enden
        verlauf.append({"role": "assistant", "content": [{"type": "text", "text": "Ich habe hier angehalten (zu viele Arbeitsschritte in einem Zug). Schreib „weiter“, dann mache ich weiter."}]})
    with tx() as c:
        c.execute("UPDATE kia_gespraech SET verlauf=?, zuordnung=?, cache=?, offen=?, hinweis='', titel=?, updated=? WHERE id=?",
                  (json.dumps(verlauf, ensure_ascii=False), json.dumps(tausch.k2t, ensure_ascii=False), json.dumps(_cache_kappen(cache), ensure_ascii=False),
                   json.dumps(offen, ensure_ascii=False), g["titel"] or text[:60], _jetzt(), gid))
    return anzeige(verlauf[neu_ab:], tausch)


def _fenster(verlauf):
    """Die letzten MAX_VERLAUF Nachrichten – Anfang immer bei einer Nachricht der Person mit Text (nicht mitten in Werkzeug-Runden)."""
    if len(verlauf) <= MAX_VERLAUF:
        return verlauf
    v = verlauf[-MAX_VERLAUF:]
    while v and not (v[0]["role"] == "user" and isinstance(v[0]["content"], str)):
        v = v[1:]
    return v or verlauf[-1:]


def _cache_kappen(cache, n=600):
    return dict(list(cache.items())[-n:])


def anzeige(nachrichten, tausch):
    """Verlauf → Einträge für die Oberfläche (Klartext). Werkzeug-Runden als kurze Zeilen."""
    NAMEN = {"projekte": "Projekte angesehen", "lv_liste": "LVs angesehen", "lv_lesen": "LV gelesen", "suche": "gesucht",
             "baudaten_kataloge": "Kataloge angesehen", "katalog_suche": "in den Baudaten gesucht", "gebaeudemodell": "Gebäudemodell gelesen",
             "firmen": "Firmen angesehen", "lv_anlegen": "LV anlegen", "positionen_einfuegen": "Positionen einfügen",
             "positionen_aendern": "Positionen ändern", "positionen_loeschen": "Zeilen entfernen", "lv_daten_aendern": "LV-Daten ändern"}
    out = []
    for m in nachrichten:
        if m["role"] == "user" and isinstance(m["content"], str):
            out.append({"wer": "ich", "text": tausch.zurueck(m["content"])})
        elif m["role"] == "assistant":
            for b in m["content"]:
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append({"wer": "claude", "text": tausch.zurueck(b["text"])})
                elif b.get("type") == "tool_use":
                    n = b["name"]
                    out.append({"wer": "werkzeug", "text": ("📝 vorgemerkt: " + NAMEN.get(n, n)) if n in werkzeuge.VORMERKEN else "🔍 " + NAMEN.get(n, n)})
    return out


def vorschau(g):
    """Offener Arbeitsschritt für die Oberfläche."""
    offen, cache = _j(g["offen"], []), _j(g["cache"], {})
    return [{"titel": werkzeuge.beschreiben(e), "zeilen": werkzeuge.einzelheiten(e, cache)} for e in offen]


def ausfuehren(c, gid, user, bp):
    g = laden(c, gid, user["id"])
    offen, cache = _j(g["offen"], []), _j(g["cache"], {})
    if not offen:
        raise werkzeuge.Fehler("Kein offener Arbeitsschritt")
    sid = c.execute("INSERT INTO kia_schritt(gespraech_id, user_id, status, aktionen, created) VALUES(?,?,?,?,?) RETURNING id",
                    (gid, user["id"], "laeuft", json.dumps(offen, ensure_ascii=False), _jetzt())).fetchone()["id"]
    try:
        erg, rueck = werkzeuge.ausfuehren(bp, offen, cache)
    except werkzeuge.Fehler as e:
        c.execute("UPDATE kia_schritt SET status='fehler', fehler=?, erledigt=? WHERE id=?", (str(e), _jetzt(), sid))
        c.execute("UPDATE kia_gespraech SET hinweis=? WHERE id=?", (f"[Ausführen fehlgeschlagen: {e}. Nichts wurde geändert.]", gid))
        raise
    c.execute("UPDATE kia_schritt SET status='ausgefuehrt', ergebnis=?, rueck=?, erledigt=? WHERE id=?",
              (json.dumps(erg, ensure_ascii=False), json.dumps(rueck, ensure_ascii=False), _jetzt(), sid))
    c.execute("UPDATE kia_gespraech SET offen='[]', hinweis=? WHERE id=?",
              ("[Die Person hat den Arbeitsschritt ausgeführt: " + json.dumps(erg, ensure_ascii=False) + "]", gid))
    return {"schritt": sid, "ergebnis": erg}


def verwerfen(c, gid, user):
    laden(c, gid, user["id"])
    c.execute("UPDATE kia_gespraech SET offen='[]', hinweis='[Die Person hat den vorgemerkten Arbeitsschritt verworfen.]' WHERE id=?", (gid,))


def zuruecknehmen(c, sid, user, bp):
    r = c.execute("SELECT * FROM kia_schritt WHERE id=? AND user_id=?", (sid, user["id"])).fetchone()
    if not r or r["status"] != "ausgefuehrt":
        raise werkzeuge.Fehler("Dieser Schritt kann nicht zurückgenommen werden")
    fehler = werkzeuge.rueckgaengig(bp, _j(r["rueck"], []))
    c.execute("UPDATE kia_schritt SET status=?, fehler=? WHERE id=?", ("zurueck" if not fehler else "teilweise", "; ".join(fehler), sid))
    c.execute("UPDATE kia_gespraech SET hinweis=? WHERE id=?", (f"[Die Person hat Arbeitsschritt {sid} rückgängig gemacht.]", r["gespraech_id"]))
    return fehler
