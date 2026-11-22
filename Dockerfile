# bauportal-ki – Claude-Agent. Baut AUF dem Image des Bauportals auf (gleiche Version wie die Kopie im Stack, Datei BP_VERSION):
# Kern (bpcore: Anmeldung, Datenbank, Anonymisierung, KI-Verbrauch) wird benutzt, nicht kopiert.
ARG BP_VERSION=v2026.11.314
FROM ghcr.io/matthiasknauff/bauplattform/plattform:${BP_VERSION}
# pg_restore und der SMB-Zugang zum Dateiserver (für „Echte Projekte neu holen“) kommen mit dem Bauportal-Image
COPY ki /app/ki
WORKDIR /app
ENV APP=ki
EXPOSE 8200
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/api/health', timeout=4)"
# EIN Prozess (Züge laufen als Hintergrund-Threads und werden über den Speicher des Prozesses abgefragt), dafür viele Threads
CMD ["gunicorn", "-b", "0.0.0.0:8200", "-w", "1", "--threads", "16", "--timeout", "120", "ki.app:app"]
