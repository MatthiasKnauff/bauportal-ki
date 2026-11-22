"""Erzeugt server/stack.yml aus dem Teststack des Bauportals (server/test.yml im Repo bauplattform).

Warum aus dem Teststack: der hat schon alle Schutzschalter (TESTSERVER=1 – Ablage nur lesen, keine Mails, keine Termine)
und eigene Volumes. Hier kommen nur dazu:
  · eigener Name, eigene Container- und Volume-Namen (bauportal_ki_…)
  · Bauportal-Version über BP_VERSION (Standard: aktuelle Freigabe) statt des Test-Stands von main
  · der Container „ki“ (Claude-Agent, Port 8200)

Aufruf (aus dem Ordner dieses Repos, das Bauportal-Repo daneben oder per Pfad):
  python3 werkzeug/stack_erzeugen.py ../bauplattform/server/test.yml v2026.11.314
"""
import re
import sys
from pathlib import Path

KI = """
  # ---------------------------------------------------------------------------------------------- Claude-Agent (bauportal-ki)
  # Eigene Oberfläche auf Port 8200. Anmeldung = Anmeldung der Bauportal-Kopie (Cookie gilt für alle Ports dieses Servers).
  # Claude bedient die Kopie über deren eigene Schnittstellen – mit den Rechten der angemeldeten Person.
  ki:
    image: ghcr.io/matthiasknauff/bauportal-ki/ki:${KI_VERSION:-latest}
    container_name: bauportal_ki_agent
    restart: unless-stopped
    depends_on:
      dienste: { condition: service_healthy }
    ports:
      - "${KI_PUBLISH:-8200}:8200"
    environment:
      <<: *env
      APP: ki
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY fehlt – in Portainer eintragen}
      KI_MODELL: ${KI_MODELL:-claude-sonnet-5-5}
      KI_DECKEL_EUR: ${KI_DECKEL_EUR:-50}
      # „Echte Projekte neu holen“: Ordner der Bauportal-Sicherungen auf dem Dateiserver (nur lesen) – wie im Bauportal unter
      # Verwaltung → Sicherung eingetragen. Nach der ersten Kopie kennt die Kopie Pfad und Zugang selbst.
      KOPIE_PFAD: ${KOPIE_PFAD:-}
      KOPIE_BENUTZER: ${KOPIE_BENUTZER:-}
      KOPIE_KENNWORT: ${KOPIE_KENNWORT:-}
      KOPIE_DOMAIN: ${KOPIE_DOMAIN:-}
"""


def erzeugen(vorlage: str, version: str) -> str:
    t = vorlage
    kopf = ("# =============================================================================\n"
            "# bauportal-ki – Bauportal-Kopie mit Claude-Agent (eigener LXC)\n"
            "# ERZEUGT von werkzeug/stack_erzeugen.py aus server/test.yml des Bauportals – nicht von Hand ändern.\n"
            "# Schutz wie der Testserver: TESTSERVER=1 (Ablage nur lesen, keine Mails, keine Termine), eigene Volumes.\n"
            "# Daten: Kopie der echten Projekte, geholt über den Knopf im KI-Container (Außenverbindungen werden dabei getrennt).\n"
            "# =============================================================================\n")
    t = re.sub(r"\A(#[^\n]*\n)+", kopf, t)
    t = re.sub(r"^name: .*$", "name: bauportal-ki", t, count=1, flags=re.M)
    t = re.sub(r"ghcr\.io/matthiasknauff/bauplattform/plattform:[\w.\-]+",
               "ghcr.io/matthiasknauff/bauplattform/plattform:${BP_VERSION:-" + version + "}", t)
    t = re.sub(r"container_name: bauplattform_(test_)?", "container_name: bauportal_ki_", t)
    t = re.sub(r"bauplattform_test_", "bauportal_ki_", t)
    # KI-Container vor den Volumes einfügen
    i = t.index("\nvolumes:")
    t = t[:i].rstrip() + "\n" + KI + t[i:]
    assert "TESTSERVER: \"1\"" in t, "Schutzschalter TESTSERVER fehlt in der Vorlage"
    return t


if __name__ == "__main__":
    src, ver = Path(sys.argv[1]), sys.argv[2]
    ziel = Path(__file__).resolve().parent.parent / "server" / "stack.yml"
    ziel.write_text(erzeugen(src.read_text(encoding="utf-8"), ver), encoding="utf-8")
    print(f"{ziel} geschrieben (Bauportal {ver})")
