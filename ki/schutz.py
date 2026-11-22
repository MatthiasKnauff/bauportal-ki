"""Anonymisierung für den Claude-Agenten – Ausbau des Bauportal-Bausteins bpcore.anonym.

Unterschiede zum Bauportal (dort: je Anfrage neue Kennungen):
  1. Kennungen gelten für das GANZE Gespräch. Claude muss sich über viele Schritte auf „firma#…“ beziehen können;
     die Zuordnung (Kennung → Klartext) liegt in der Datenbank des Büros (Tabelle kia_gespraech.zuordnung) und geht nie hinaus.
  2. Zusätzliche Muster: PLZ + Ort, Flur/Flurstück/Gemarkung, Internetadressen, Kfz-Kennzeichen, Geburtsdaten mit „geb.“,
     Dateinamen und Pfade (\\\\server\\…, /mnt/…), Aktenzeichen.
  3. Alles, was hinausgeht, läuft durch hin(): Nachrichten, Ergebnisse der Werkzeuge (Freitexte, Langtexte, Dateinamen …).
     Alles, was zurückkommt, durch zurueck(): Antworttext UND die Eingaben der Werkzeuge (Claude schreibt dort Kennungen).
  4. Schlusskontrolle: vor dem Senden wird geprüft, ob noch ein bekannter Klartext im Text steht – dann wird nicht gesendet.
"""
import json
import re
import secrets

try:   # im Image vorhanden; in den Tests ersetzt
    from bpcore import anonym as _bp
except ImportError:   # pragma: no cover
    _bp = None

ZUSATZ = [
    ("adresse", re.compile(r"\b\d{5}\s+(?!(?:Euro|EUR|Stück|Stk|St|Std|Stunden|Tage|Wochen|Positionen|Zeilen|Mal|Meter|Liter|Kilo)\b)[A-ZÄÖÜ][\wäöüß\-]+(?:\s(?:an der|am|im|in der|bei)\s[A-ZÄÖÜ][\wäöüß\-]+)?")),            # 45127 Essen
    ("flurstueck", re.compile(r"(?i:gemarkung)\s+[A-ZÄÖÜ][\wäöüß\-]+|(?i:flur(?:stück)?s?)\s*(?:nr\.?\s*)?\d+(?:/\d+)?")),
    ("link", re.compile(r"\bhttps?://[^\s)\"'<>]+|\bwww\.[^\s)\"'<>]+")),
    ("pfad", re.compile(r"\\\\[\w.\-]+\\[^\s\"'<>]*|(?<![\w])/(?:mnt|data|srv|home)/[^\s\"'<>]*")),
    ("kennzeichen", re.compile(r"\b[A-ZÄÖÜ]{1,3}-[A-Z]{1,2}\s?\d{1,4}[EH]?\b")),
    ("geburt", re.compile(r"(?i:geb\.|geboren(?:\s+am)?)\s*\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}")),
    ("aktenzeichen", re.compile(r"(?i:az\.?|aktenzeichen)\s*[:\-]?\s*[\w/.\-]{3,30}")),
]
ARTEN = tuple(_bp.ARTEN if _bp else ()) + tuple(a for a, _ in ZUSATZ)
_KENNUNG = re.compile(r"\b([a-z]+)#(\d{12})\b")

HINWEIS = ("Namen, Adressen, Kontaktdaten, Orte, Flurstücke, Links und Dateipfade sind durch Art#Nummer ersetzt "
           "(z. B. firma#302918475610, projekt#…, person#…, adresse#…). Benutze diese Kennungen unverändert, wo du die Sache meinst "
           "– auch in den Eingaben der Werkzeuge. Erfinde keine neuen Kennungen und rate keine Klartexte.")


def _an_kennung(m):
    """Treffer berührt eine Kennung (enthält „#“ oder eine Kennung folgt direkt) – dann nicht ersetzen."""
    return "#" in m.group(0) or m.string[m.end():m.end() + 1] == "#"


# Wörter, die in Projektnamen stehen, aber allein nichts verraten
_ALLG_PROJEKT = set("""neubau umbau anbau erweiterung sanierung modernisierung aufstockung abbruch instandsetzung energetische
energetisch haus gebäude wohnhaus wohnung wohnungen bauvorhaben projekt mfh efh dhh rh bv bauabschnitt ba teil und der die das""".split())
_TEILE = {"stand": None, "rx": None}


def _teilnamen(c):
    """Teilnamen von Projekten: je zwei oder mehr aufeinanderfolgende Wörter eines Projektnamens, sobald ein Wort darin
    nicht allgemein ist („Tierheim Hafenstraße“ aus „P123 Erweiterung Tierheim Hafenstraße“) – so spricht man von Projekten."""
    stand = tuple(c.execute("SELECT count(*), max(updated) FROM projects").fetchone())
    if _TEILE["stand"] == stand:
        return _TEILE["rx"]
    L = set()
    for r in c.execute("SELECT name FROM projects").fetchall():
        w = [x for x in re.findall(r"[\wäöüÄÖÜß\-]+", r["name"] or "") if not re.fullmatch(r"[A-Z]?\d+(?:\.\d+)?", x)]
        for i in range(len(w)):
            for j in range(i + 2, len(w) + 1):
                teil = w[i:j]
                if any(x.lower() not in _ALLG_PROJEKT and len(x) >= 4 for x in teil):
                    L.add(" ".join(teil))
    L = sorted(L, key=len, reverse=True)
    _TEILE.update(stand=stand, rx=re.compile(r"(?<![\w#])(?:" + "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in L) + r")(?![\w#])", re.I) if L else None)
    return _TEILE["rx"]


class Gespraech:
    """Tausch für ein ganzes Gespräch. zuordnung = {kennung: klartext} (persistiert vom Aufrufer)."""

    def __init__(self, tx, zuordnung=None):
        """tx: Transaktion der Datenbank (bpcore.db.tx) – je hin() eine kurze, damit ein langer Zug keine Transaktion offen hält."""
        self.tx = tx
        self.k2t = dict(zuordnung or {})
        self.t2k = {t.lower(): k for k, t in self.k2t.items()}

    def _kennung(self, text, art):
        key = text.lower()
        if key not in self.t2k:
            k = f"{art}#{secrets.randbelow(10 ** 12):012d}"
            while k in self.k2t:
                k = f"{art}#{secrets.randbelow(10 ** 12):012d}"
            self.t2k[key], self.k2t[k] = k, text
        return self.t2k[key]

    def hin(self, text):
        if not text:
            return text
        if _bp:   # Namen aus der Datenbank (Liste des Bauportals) und Teilnamen von Projekten
            with self.tx() as c:
                _bp._liste(c)
                rx, idx = _bp._CACHE["rx"], _bp._CACHE["idx"]
                teile = _teilnamen(c)
            if rx:
                text = rx.sub(lambda m: self._kennung(m.group(0), idx.get(m.group(0).lower(), "person")), text)
            if teile:
                text = teile.sub(lambda m: self._kennung(m.group(0), "projekt"), text)
            for art, rx in _bp._MUSTER:   # Muster des Bauportals – nie über eine Kennung hinweg („Firma firma#…“)
                text = rx.sub(lambda m: m.group(0) if _an_kennung(m) else self._kennung(m.group(0), art), text)
        for t_low, k in sorted(self.t2k.items(), key=lambda x: -len(x[0])):   # schon bekannte Klartexte (auch aus früheren Zügen)
            if len(t_low) >= 3:
                text = re.sub(r"(?<![\w#@.])" + re.escape(self.k2t[k]) + r"(?![\w@])", k, text, flags=re.I)
        for art, rx in ZUSATZ:
            text = rx.sub(lambda m: m.group(0) if _an_kennung(m) else self._kennung(m.group(0), art), text)
        return text

    def zurueck(self, text):
        if not text or not self.k2t:
            return text
        nr = {k.split("#", 1)[1]: t for k, t in self.k2t.items()}
        return _KENNUNG.sub(lambda m: nr.get(m.group(2), m.group(0)), text)

    def zurueck_tief(self, v):
        """Eingaben der Werkzeuge: alle Zeichenketten zurückübersetzen."""
        if isinstance(v, str):
            return self.zurueck(v)
        if isinstance(v, list):
            return [self.zurueck_tief(x) for x in v]
        if isinstance(v, dict):
            return {k: self.zurueck_tief(x) for k, x in v.items()}
        return v

    def hin_json(self, v):
        return self.hin(json.dumps(v, ensure_ascii=False, default=str))

    def pruefen(self, text):
        """Schlusskontrolle: bekannte Klartexte (ab 4 Zeichen) dürfen im ausgehenden Text nicht mehr stehen. Gibt Funde zurück."""
        low = (text or "").lower()
        return [self.k2t[k] for t, k in self.t2k.items() if len(t) >= 4 and re.search(r"(?<![\w#@.])" + re.escape(t) + r"(?![\w@])", low)]


def protokoll(c, zweck, gesendet, antwort, zuordnung, dauer_ms):
    """Ins Protokoll der Bauportal-Kopie (Verwaltung → KI → Datenschutz) – dieselbe Tabelle wie alle KI-Anfragen."""
    if _bp:
        _bp.protokollieren(c, "claude", zweck, gesendet, antwort, [(k, t, k.split("#")[0]) for k, t in zuordnung.items()], dauer_ms)
