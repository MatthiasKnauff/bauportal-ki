# bauportal-ki

Eine eigene Instanz des Bauportals, in der **Claude mitarbeitet**: Claude liest Projekte, LVs, Baudaten und Gebäudemodell
und merkt Änderungen vor. Die Person sieht den ganzen Arbeitsschritt als Vorschau und führt ihn mit einem Klick aus.
Jeder Schritt lässt sich wieder zurücknehmen.

Das Bauportal selbst (Repo `bauplattform`) wird dafür **nicht verändert**. Diese Instanz läuft auf einem eigenen LXC.

| Teil | Was | Port |
|---|---|---|
| Bauportal-Kopie | dasselbe Image wie die Produktion (Version in `BP_VERSION`), Schutz wie der Testserver (`TESTSERVER=1`) | 8090 ff. |
| KI-Container | Claude-Agent mit eigener Oberfläche (`ki/`) | 8200 |

```
Browser ──► :8200 Bauportal KI ──► Claude (anonymisiert, mit Kostendeckel)
                 │
                 └──► AVA der Kopie (:8097, intern) – mit dem Cookie der angemeldeten Person
                         │
                         └──► Datenbank der Kopie ◄── „Echte Projekte neu holen“ (neueste Sicherung vom Dateiserver)
```

## Wie Claude arbeitet

- **Lesen** sofort: Projekte, LVs, Suche, Baudaten-Kataloge, Gebäudemodell, Firmen.
- **Ändern** nur über **Vormerken**: LV anlegen, Positionen einfügen, ändern, löschen, LV-Daten ändern. Erst „Ausführen“ ändert etwas.
- Ausgeführt wird über die **normalen Schnittstellen der Kopie** mit den **Rechten der angemeldeten Person**. Prüfungen, Verlauf,
  Papierkorb und Protokoll des Bauportals gelten unverändert.
- **Rückgängig** je Arbeitsschritt: LV-Inhalte werden auf den Stand vor dem Schritt zurückgeschrieben, neu angelegte LVs gehen in
  den Papierkorb. Scheitert eine Aktion mitten im Schritt, wird das bis dahin Ausgeführte sofort zurückgenommen.
- Positionen aus den Baudaten übernimmt Claude **per Verweis**: der Text kommt aus der Datenbank, nicht von Claude.
- OZ werden nur neu nummeriert, solange das LV noch nicht draußen war (keine Angebote, nicht vergeben).

## Datenschutz

Alles, was an Claude geht, wird anonymisiert (`ki/schutz.py`, aufbauend auf dem Bauportal-Baustein `bpcore/anonym.py`):

- Projekte, Bauherr:innen, Firmen, Personen, Mitarbeiter:innen, E-Mail, Telefon, IBAN, Anschriften (aus der Datenbank und über Muster)
- **neu:** Teilnamen von Projekten („Tierheim Hafenstraße“), PLZ + Ort, Gemarkung/Flur/Flurstück, Internetadressen, Dateipfade,
  Kfz-Kennzeichen, Geburtsdaten, Aktenzeichen
- **neu:** Kennungen gelten für das ganze Gespräch (Claude kann sich über viele Schritte auf `firma#…` beziehen); die Zuordnung bleibt in der Datenbank
- **neu:** auch die Ergebnisse der Werkzeuge (Langtexte, Freitexte, Dateinamen) gehen nur anonymisiert hinaus; Claudes Eingaben werden zurückübersetzt
- **neu:** Schlusskontrolle – steht ein bekannter Klartext noch im Text, wird nicht gesendet
- jede Anfrage im Protokoll der Kopie (Verwaltung → KI → Datenschutz), Kosten zusätzlich in der Verbrauchsübersicht

## Einrichten (einmal)

1. **LXC** mit Docker und Portainer-Agent anlegen (wie Docker 1/2). Etwa 8 GB RAM ohne Ollama.
2. **Portainer → Stacks → Add stack → Repository**: `https://github.com/MatthiasKnauff/bauportal-ki`, Datei `server/stack.yml`,
   „Automatic updates“ (Polling) an. Das Repo und die Images sind öffentlich – kein Token nötig.
3. **Umgebungsvariablen** im Stack:

   | Variable | Wert |
   |---|---|
   | `DB_PASSWORD` | beliebig, nur für diese Instanz |
   | `ANTHROPIC_API_KEY` | eigener Schlüssel für diese Instanz (Pflicht) |
   | `KI_DECKEL_EUR` | Monatsdeckel, Standard 50 (später in der Oberfläche änderbar, Klick auf die Kosten) |
   | `KI_MODELL` | Standard `claude-sonnet-5-5` |
   | `KOPIE_PFAD` | Ordner der Bauportal-Sicherungen auf dem Dateiserver, wie im Bauportal unter Verwaltung → Sicherung eingetragen |
   | `KOPIE_BENUTZER`, `KOPIE_KENNWORT`, `KOPIE_DOMAIN` | Zugang zum Dateiserver (wie für die Ablage) – Lesen genügt |
   | `SECRET_KEY` | beliebig |

4. **Woher die Kopie kommt:** Das Bauportal legt seine Datenbanksicherungen ohnehin auf den Dateiserver (Ordner `Datenbank`).
   Diese Instanz liest von dort die neueste – nur lesend, ohne Zugang zu Docker 2. Stand = letzte Sicherung; für einen frischeren
   Stand im Bauportal unter Verwaltung → Sicherung „jetzt sichern“ und „auf den Dateiserver kopieren“.
5. **Erste Kopie** auf dem KI-LXC: `docker exec bauportal_ki_agent python -m ki.kopie` (danach gibt es die gewohnten Konten).
   Dann `http://<KI-LXC>:8200` öffnen und mit dem gewohnten Konto anmelden. Später holt ein Administrator neue Stände über den
   Knopf **„Echte Projekte neu holen“** oben; danach einmal neu anmelden (die Sitzungen kommen mit der Kopie).

## Was die Kopie beim Holen trennt

Damit die Kopie nicht mit echten Zugängen arbeitet, werden nach dem Einspielen gelöscht: Microsoft-365-Zugang und Mail-Kennwort
(Posteingang würde sonst Mails abholen), persönliche Postfächer (IMAP), Zugang zum Bauherrenportal (würde sonst echte Freigaben
hinausschieben). Mails, Kalendertermine und Schreiben in die Ablage sperrt zusätzlich `TESTSERVER=1`.

## Neue Versionen

- **Claude-Agent:** Push auf `main` → Tests → Image → Kurzkennung in `server/stack.yml` → Portainer spielt ein.
- **Bauportal-Kopie:** Version in `BP_VERSION` eintragen und `python3 werkzeug/stack_erzeugen.py ../bauplattform/server/test.yml <Version>`
  ausführen (übernimmt neue Container und Schutzschalter des Bauportals), committen.

## Tests

```bash
BPCORE=../bauplattform/shared python3 -m unittest discover -s tests -v
```
Claude, Bauportal und PostgreSQL sind in den Tests Attrappen; der Kern des Bauportals (Anonymisierung, Verbrauch) läuft echt.
