"""Werkzeuge, mit denen Claude das Bauportal bedient – Etappe 1: Ausschreibung (AVA).

Grundsatz: Claude benutzt dieselben Schnittstellen wie die Oberfläche (AVA-Container der Bauportal-Kopie), mit dem Cookie der
angemeldeten Person. Rechte, Prüfungen, Verlauf und Protokoll des Bauportals gelten also unverändert – hier wird nichts nachgebaut.

Zwei Arten:
  LESEN     wird sofort ausgeführt, das Ergebnis geht (anonymisiert) an Claude.
  VORMERKEN ändert noch nichts. Claude bekommt „vorgemerkt“ zurück; am Ende des Zuges sieht die Person den ganzen
            Arbeitsschritt als Vorschau und führt ihn mit einem Klick aus (oder verwirft ihn). Neue Dinge bekommen Platzhalter
            („neu:1“), auf die spätere Aktionen desselben Schritts verweisen dürfen.
Rückgängig: vor jeder Änderung an einem LV wird dessen Inhalt gemerkt; zurück geht es in umgekehrter Reihenfolge
(LV-Inhalt zurückschreiben, neu angelegte LVs in den Papierkorb des Bauportals).
"""
import copy
import json
import os
import urllib.error
import urllib.request

AVA = os.environ.get("AVA_INTERNAL", "http://ava:8097")
COOKIE = os.environ.get("SSO_COOKIE_NAME", "bp_sso")
MAX_ERGEBNIS = 24000   # Zeichen je Werkzeug-Ergebnis an Claude
EINHEITEN = ("m", "m2", "m3", "m²", "m³", "St", "Stk", "psch", "h", "kg", "t", "lfm", "l", "Wo", "Tg", "%")


class Fehler(Exception):
    pass


class Bauportal:
    """Zugang zur Bauportal-Kopie im Namen der angemeldeten Person."""

    def __init__(self, token, basis=AVA):
        self.token, self.basis = token, basis.rstrip("/")

    def __call__(self, methode, pfad, daten=None, timeout=60):
        body = json.dumps(daten).encode() if daten is not None else None
        req = urllib.request.Request(self.basis + pfad, data=body, method=methode,
                                     headers={"Cookie": f"{COOKIE}={self.token}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                t = r.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error")
            except Exception:   # noqa: BLE001 – Fehlerseite ohne JSON
                msg = None
            raise Fehler(msg or f"Bauportal antwortet mit {e.code}") from None
        except urllib.error.URLError as e:
            raise Fehler(f"Bauportal nicht erreichbar ({e.reason})") from None
        return json.loads(t)


# ------------------------------------------------------------------------------------------------ Beschreibung für Claude
def _o(props, req=()):
    return {"type": "object", "properties": props, "required": list(req)}


_POS = {"type": "object", "properties": {
    "typ": {"type": "string", "enum": ["titel", "pos"], "description": "titel = Gliederungspunkt, pos = Position"},
    "lvl": {"type": "integer", "description": "nur Titel: Ebene 1–4"},
    "katalog_ref": {"type": "string", "description": "Position aus den Baudaten übernehmen (ref aus katalog_suche) – Text kommt dann aus der Datenbank, nicht von dir"},
    "kurz": {"type": "string", "description": "Kurztext (bei katalog_ref nur, wenn er abweichen soll)"},
    "lang": {"type": "string", "description": "Langtext"},
    "menge": {"type": "number"}, "einheit": {"type": "string"},
    "kg": {"type": "string", "description": "Kostengruppe DIN 276, 3-stellig"},
    "art": {"type": "string", "enum": ["N", "E", "A", "T"], "description": "N normal, E Eventual/Bedarf, A Alternative, T Hinweistext"},
    "ep_s": {"type": "number", "description": "geschätzter Einheitspreis netto in €"}}}

WERKZEUGE = [
    {"name": "projekte", "description": "Alle Projekte, die die Person sehen darf (id, Name, archiviert).", "input_schema": _o({})},
    {"name": "lv_liste", "description": "Leistungsverzeichnisse eines Projekts mit Stand (Positionen, Schätzung, Angebote, Auftrag, Abrechnung).",
     "input_schema": _o({"projekt_id": {"type": "integer"}}, ["projekt_id"])},
    {"name": "lv_lesen", "description": "Inhalt eines LV: Titel und Positionen mit id, OZ, Kurz-/Langtext (gekürzt), Menge, Einheit, KG, Schätzpreis. "
                                        "Große LVs in Abschnitten (ab, anzahl).",
     "input_schema": _o({"lv_id": {"type": "integer"}, "ab": {"type": "integer"}, "anzahl": {"type": "integer"}, "langtext": {"type": "boolean"}}, ["lv_id"])},
    {"name": "suche", "description": "Volltextsuche über LVs, Bieter, Aufmaß (alle Wörter müssen vorkommen).",
     "input_schema": _o({"text": {"type": "string"}, "projekt_id": {"type": "integer"}}, ["text"])},
    {"name": "baudaten_kataloge", "description": "Kataloge der Positionsdatenbank (Baudaten) mit Anzahl Positionen.", "input_schema": _o({})},
    {"name": "katalog_suche", "description": "Positionen in den Baudaten suchen. Ergebnis mit ref für positionen_einfuegen (katalog_ref). "
                                             "Bevorzuge Positionen aus den Baudaten vor selbst formulierten Texten.",
     "input_schema": _o({"text": {"type": "string"}, "katalog": {"type": "string", "description": "key aus baudaten_kataloge; leer = alle"}}, ["text"])},
    {"name": "gebaeudemodell", "description": "Gebäudemodell eines Projekts (Planungsangaben, Bauteile, Räume, Mengen) – Grundlage, um LVs abzuleiten.",
     "input_schema": _o({"projekt_id": {"type": "integer"}, "teil": {"type": "string", "description": "optional: nur dieser Schlüssel des Modells"}}, ["projekt_id"])},
    {"name": "firmen", "description": "Firmen aus den Adressen (für Bieterlisten), optional nach Gewerk gefiltert.",
     "input_schema": _o({"gewerk": {"type": "string"}})},
    # ---- vormerken (ändert erst nach Bestätigung)
    {"name": "lv_anlegen", "description": "VORMERKEN: neues LV im Projekt anlegen. Gibt einen Platzhalter (neu:n) zurück, den du als lv_id weiterverwenden kannst.",
     "input_schema": _o({"projekt_id": {"type": "integer"}, "name": {"type": "string"}, "gewerk": {"type": "string"}, "nr": {"type": "string"},
                         "kopie_von": {"type": "integer", "description": "Texte und Mengen eines vorhandenen LV übernehmen (ohne Preise)"}}, ["projekt_id", "name"])},
    {"name": "positionen_einfuegen", "description": "VORMERKEN: Titel/Positionen in ein LV einfügen – am Ende oder nach einer vorhandenen Zeile (nach_id).",
     "input_schema": _o({"lv_id": {"type": ["integer", "string"]}, "nach_id": {"type": "string"}, "eintraege": {"type": "array", "items": _POS}}, ["lv_id", "eintraege"])},
    {"name": "positionen_aendern", "description": "VORMERKEN: vorhandene Positionen ändern (nur die angegebenen Felder).",
     "input_schema": _o({"lv_id": {"type": ["integer", "string"]}, "aenderungen": {"type": "array", "items": {"type": "object", "properties": {
         "id": {"type": "string"}, **{k: _POS["properties"][k] for k in ("kurz", "lang", "menge", "einheit", "kg", "art", "ep_s")}}, "required": ["id"]}}},
        ["lv_id", "aenderungen"])},
    {"name": "positionen_loeschen", "description": "VORMERKEN: Zeilen (Positionen/Titel) aus einem LV entfernen.",
     "input_schema": _o({"lv_id": {"type": ["integer", "string"]}, "ids": {"type": "array", "items": {"type": "string"}}}, ["lv_id", "ids"])},
    {"name": "lv_daten_aendern", "description": "VORMERKEN: Name, Gewerk oder Nummer eines LV ändern.",
     "input_schema": _o({"lv_id": {"type": ["integer", "string"]}, "name": {"type": "string"}, "gewerk": {"type": "string"}, "nr": {"type": "string"}}, ["lv_id"])},
]
VORMERKEN = {"lv_anlegen", "positionen_einfuegen", "positionen_aendern", "positionen_loeschen", "lv_daten_aendern"}


# ------------------------------------------------------------------------------------------------ Lesen
def _kurz(t, n):
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[:n] + " …"


def _zeile(x, lang):
    if x.get("typ") == "titel":
        return {"id": x["id"], "titel": x.get("kurz", ""), "oz": x.get("oz", ""), "lvl": x.get("lvl", 1)}
    z = {"id": x["id"], "oz": x.get("oz", ""), "kurz": x.get("kurz", ""), "menge": x.get("menge"), "einheit": x.get("einheit", "")}
    for k in ("kg", "ep_s", "nt", "nt_status"):
        if x.get(k):
            z[k] = x[k]
    if x.get("art", "N") != "N":
        z["art"] = x["art"]
    if lang and x.get("lang"):
        z["lang"] = _kurz(x["lang"], 600)
    return z


def lesen(bp, name, a, cache):
    """Lese-Werkzeug ausführen → Ergebnis (Python-Wert). cache: Speicher des Gesprächs (Katalogtreffer)."""
    if name == "projekte":
        return bp("GET", "/api/projekte")
    if name == "lv_liste":
        felder = ("id", "nr", "name", "gewerk", "status", "n_pos", "n_angebote", "schaetzung", "auftrag", "auftragnehmer", "vergeben", "abgerechnet", "kg")
        return [{k: x.get(k) for k in felder} for x in bp("GET", f"/api/lv?project_id={int(a['projekt_id'])}") if not x.get("lv_weg")]
    if name == "lv_lesen":
        p = bp("GET", f"/api/lv/{int(a['lv_id'])}")
        items = (p.get("data") or {}).get("items") or []
        ab, n = max(0, int(a.get("ab") or 0)), max(1, min(400, int(a.get("anzahl") or 150)))
        return {"id": p["id"], "nr": p.get("nr"), "name": p.get("name"), "gewerk": p.get("gewerk"), "status": p.get("status"),
                "angebote": len(p.get("angebote") or []), "vergeben": bool((p.get("vergabe") or {}).get("bieter")),
                "zeilen_gesamt": len(items), "ab": ab, "zeilen": [_zeile(x, a.get("langtext", True)) for x in items[ab:ab + n]],
                "vorbemerkung": _kurz((p.get("data") or {}).get("vorbem"), 1500)}
    if name == "suche":
        q = urllib.request.quote(str(a["text"]))
        r = bp("GET", f"/api/suche?q={q}" + (f"&pid={int(a['projekt_id'])}" if a.get("projekt_id") else ""))
        return {"treffer": (r.get("treffer") or [])[:60], "mehr": r.get("mehr")}
    if name == "baudaten_kataloge":
        return bp("GET", "/api/baudaten/kataloge")
    if name == "katalog_suche":
        keys = [a["katalog"]] if a.get("katalog") else [k["key"] for k in bp("GET", "/api/baudaten/kataloge") if k.get("n")]
        out = []
        for key in keys[:6]:
            r = bp("GET", f"/api/katalog/bd-{urllib.request.quote(key)}/suche?q={urllib.request.quote(str(a['text']))}")
            for x in (r.get("data") or {}).get("items") or []:
                if x.get("typ") != "pos":
                    continue
                ref = f"kat:{key}:{x['id']}"
                cache[ref] = x   # ganzer Eintrag bleibt im Büro; Claude verweist nur
                out.append({"ref": ref, "oz": x.get("oz"), "kurz": x.get("kurz"), "einheit": x.get("einheit"), "ep": x.get("ep_s") or x.get("ep"),
                            "lang": _kurz(x.get("lang"), 220)})
                if len(out) >= 40:
                    break
        return {"treffer": out}
    if name == "gebaeudemodell":
        m = bp("GET", f"/api/bp/modell/{int(a['projekt_id'])}")
        if a.get("teil"):
            m = {a["teil"]: m.get(a["teil"]) if isinstance(m, dict) else None}
        return m
    if name == "firmen":
        g = (a.get("gewerk") or "").lower()
        return [{"id": x["id"], "name": x["name"], "gewerk": x.get("trade")} for x in bp("GET", "/api/kontakte") if not g or g in (x.get("trade") or "").lower()][:150]
    raise Fehler(f"Unbekanntes Werkzeug {name}")


def kuerzen(text):
    return text if len(text) <= MAX_ERGEBNIS else text[:MAX_ERGEBNIS] + f"\n… (gekürzt, {len(text)} Zeichen – mit ab/anzahl oder teil genauer abfragen)"


# ------------------------------------------------------------------------------------------------ Vormerken
def vormerken(bp, name, a, schritt, cache):
    """Prüft die Aktion und hängt sie an den Arbeitsschritt (Liste). Gibt die Rückmeldung an Claude zurück."""
    neu = [x for x in schritt if x["werkzeug"] == "lv_anlegen"]
    lv = a.get("lv_id")
    if name != "lv_anlegen":
        if isinstance(lv, str) and lv.startswith("neu:"):
            n = next((x for x in neu if x["platzhalter"] == lv), None)
            if not n:
                raise Fehler(f"Platzhalter {lv} gibt es in diesem Schritt nicht")
            a = {**a, "_lv_name": f"neues LV „{n['eingabe'].get('name')}“"}
        else:
            try:
                lv = int(lv)
            except (TypeError, ValueError):
                raise Fehler("lv_id fehlt") from None
            p = bp("GET", f"/api/lv/{lv}")   # gibt es das LV, darf die Person es sehen?
            ids = {x["id"] for x in (p.get("data") or {}).get("items") or []}
            fremd = [i for i in ([c["id"] for c in a.get("aenderungen") or []] + list(a.get("ids") or []) + ([a["nach_id"]] if a.get("nach_id") else [])) if i not in ids]
            if fremd:
                raise Fehler(f"Zeilen nicht im LV {lv}: {', '.join(fremd[:5])}")
            a = {**a, "lv_id": lv, "_lv_name": f"{p.get('nr') or ''} {p.get('name') or ''}".strip()}
    for e in a.get("eintraege") or []:
        if e.get("katalog_ref") and e["katalog_ref"] not in cache:
            raise Fehler(f"{e['katalog_ref']} ist nicht aus einer katalog_suche dieses Gesprächs")
    eintrag = {"werkzeug": name, "eingabe": a}
    if name == "lv_anlegen":
        eintrag["platzhalter"] = f"neu:{len(neu) + 1}"
    schritt.append(eintrag)
    text = beschreiben(eintrag)
    return f"vorgemerkt: {text}" + (f" – Platzhalter {eintrag['platzhalter']}" if name == "lv_anlegen" else "")


def beschreiben(e):
    """Eine Zeile für die Vorschau (Klartext für die Person)."""
    t = _beschreiben(e)
    return t[:1].upper() + t[1:]


def _beschreiben(e):
    a, w = e["eingabe"], e["werkzeug"]
    ziel = a.get("_lv_name") or (f"LV {a.get('lv_id')}" if a.get("lv_id") else "")
    if w == "lv_anlegen":
        return f"Neues LV „{a.get('name')}“" + (f" ({a['gewerk']})" if a.get("gewerk") else "") + (f", Texte aus LV {a['kopie_von']}" if a.get("kopie_von") else "")
    if w == "positionen_einfuegen":
        E = a.get("eintraege") or []
        np_ = sum(1 for x in E if x.get("typ", "pos") == "pos")
        return f"{ziel}: {np_} Position(en)" + (f" und {len(E) - np_} Titel" if len(E) - np_ else "") + " einfügen"
    if w == "positionen_aendern":
        return f"{ziel}: {len(a.get('aenderungen') or [])} Position(en) ändern"
    if w == "positionen_loeschen":
        return f"{ziel}: {len(a.get('ids') or [])} Zeile(n) entfernen"
    if w == "lv_daten_aendern":
        return f"{ziel}: " + ", ".join(f"{k} → {a[k]}" for k in ("name", "gewerk", "nr") if a.get(k))
    return w


def einzelheiten(e, cache):
    """Zeilen der Vorschau unter der Überschrift (was genau eingefügt/geändert wird)."""
    a, w = e["eingabe"], e["werkzeug"]
    if w == "positionen_einfuegen":
        out = []
        for x in a.get("eintraege") or []:
            k = cache.get(x.get("katalog_ref")) or {}
            kurz = x.get("kurz") or k.get("kurz") or ""
            if x.get("typ") == "titel":
                out.append(f"▸ {kurz}")
            else:
                m = x.get("menge", k.get("menge") or 0)
                out.append(f"{kurz} – {m:g} {x.get('einheit') or k.get('einheit') or ''}".strip() + ("  [Baudaten]" if x.get("katalog_ref") else ""))
        return out
    if w == "positionen_aendern":
        return [f"{c['id']}: " + ", ".join(f"{k} → {_kurz(v, 60)}" for k, v in c.items() if k != "id") for c in a.get("aenderungen") or []]
    if w == "positionen_loeschen":
        return list(a.get("ids") or [])
    return []


# ------------------------------------------------------------------------------------------------ Ausführen und Rückgängig
def _neue_zeile(x, cache):
    import uuid
    if x.get("typ") == "titel":
        return {"id": uuid.uuid4().hex[:10], "typ": "titel", "lvl": int(x.get("lvl") or 1), "oz": "", "kurz": x.get("kurz") or "Titel"}
    k = dict(cache.get(x.get("katalog_ref")) or {})
    p = {"id": uuid.uuid4().hex[:10], "typ": "pos", "oz": "", "kurz": k.get("kurz") or "", "lang": k.get("lang") or "",
         "menge": k.get("menge") or 0, "einheit": k.get("einheit") or "", "art": k.get("art") or "N", "kg": k.get("kg") or "", "ep_s": k.get("ep_s")}
    if k.get("pdb"):
        p["pdb"] = k["pdb"]
    for f in ("kurz", "lang", "menge", "einheit", "kg", "art", "ep_s"):
        if x.get(f) not in (None, ""):
            p[f] = x[f]
    if x.get("katalog_ref"):
        p["quelle"] = "Claude: " + x["katalog_ref"]
    return p


def ausfuehren(bp, aktionen, cache):
    """Arbeitsschritt ausführen. Gibt (ergebnisse, rueck) zurück; rueck = Liste für rueckgaengig(), in Ausführungsreihenfolge.
    Bricht eine Aktion ab, wird das bis dahin Ausgeführte sofort zurückgenommen und Fehler geworfen."""
    ids, rueck, erg, gemerkt = {}, [], [], set()

    def lv_id(v):
        return ids[v] if isinstance(v, str) and v.startswith("neu:") else int(v)

    try:
        for e in aktionen:
            a, w = e["eingabe"], e["werkzeug"]
            if w == "lv_anlegen":
                p = bp("POST", "/api/lv", {k: v for k, v in {"project_id": a["projekt_id"], "name": a["name"], "gewerk": a.get("gewerk"),
                                                             "nr": a.get("nr"), "kopie_von": a.get("kopie_von")}.items() if v not in (None, "")})
                ids[e["platzhalter"]] = p["id"]
                rueck.append({"art": "lv_weg", "lv_id": p["id"]})
                gemerkt.add(p["id"])   # neues LV: Inhalt muss nicht gemerkt werden
                erg.append({"aktion": beschreiben(e), "lv_id": p["id"], "nr": p.get("nr")})
                continue
            i = lv_id(a["lv_id"])
            p = bp("GET", f"/api/lv/{i}")
            d = p.get("data") or {}
            if i not in gemerkt:
                rueck.append({"art": "lv_inhalt", "lv_id": i, "data": copy.deepcopy({"items": d.get("items") or [], "vorbem": d.get("vorbem") or ""}),
                              "name": p.get("name"), "gewerk": p.get("gewerk"), "nr": p.get("nr")})
                gemerkt.add(i)
            items = list(d.get("items") or [])
            body = {"rev": p.get("rev")}
            if w == "positionen_einfuegen":
                neu = [_neue_zeile(x, cache) for x in a.get("eintraege") or []]
                at = next((k + 1 for k, x in enumerate(items) if x["id"] == a.get("nach_id")), len(items))
                items[at:at] = neu
                # nummerieren nur, solange das LV noch nicht draußen war (keine Angebote, nicht vergeben) – sonst ändern sich OZ, die Bieter kennen
                body["nummerieren"] = not p.get("angebote") and not (p.get("vergabe") or {}).get("bieter")
            elif w == "positionen_aendern":
                by = {c["id"]: c for c in a.get("aenderungen") or []}
                for x in items:
                    for k, v in (by.get(x["id"]) or {}).items():
                        if k != "id":
                            x[k] = v
            elif w == "positionen_loeschen":
                weg = set(a.get("ids") or [])
                items = [x for x in items if x["id"] not in weg]
            if w == "lv_daten_aendern":
                body.update({k: a[k] for k in ("name", "gewerk", "nr") if a.get(k)})
            else:
                body["data"] = {"items": items, "vorbem": d.get("vorbem") or ""}
                if d.get("ozm"):
                    body["data"]["ozm"] = d["ozm"]
            r = bp("PUT", f"/api/lv/{i}", body)
            erg.append({"aktion": beschreiben(e), "lv_id": i, "zeilen": len((r.get("data") or {}).get("items") or [])})
    except Exception as ex:
        fehler = rueckgaengig(bp, rueck)
        raise Fehler(f"{ex} – bereits Ausgeführtes wurde zurückgenommen" + (f" (dabei: {'; '.join(fehler)})" if fehler else "")) from None
    return erg, rueck


def rueckgaengig(bp, rueck):
    """In umgekehrter Reihenfolge zurück. Gibt Fehlermeldungen zurück (leer = alles zurück)."""
    fehler = []
    for r in reversed(rueck):
        try:
            if r["art"] == "lv_weg":
                bp("DELETE", f"/api/lv/{r['lv_id']}")   # Papierkorb des Bauportals – dort auch wiederherstellbar
            elif r["art"] == "lv_inhalt":
                bp("PUT", f"/api/lv/{r['lv_id']}", {"data": r["data"], "name": r["name"], "gewerk": r["gewerk"], "nr": r["nr"], "force": True})
        except Exception as e:   # noqa: BLE001 – weiter mit dem Rest, Meldung sammeln
            fehler.append(f"LV {r['lv_id']}: {e}")
    return fehler
