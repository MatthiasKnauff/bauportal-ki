// Bauportal KI – Gespräch mit Claude, Vorschau des Arbeitsschritts, Ausführen / Verwerfen / Rückgängig.
'use strict';
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const eur = (v) => (Math.round(v * 100) / 100).toLocaleString('de-DE', { minimumFractionDigits: 2 }) + ' €';
let aktiv = null, warte = null;

async function api(methode, pfad, daten) {
  const r = await fetch(pfad, { method: methode, headers: { 'Content-Type': 'application/json' }, body: daten ? JSON.stringify(daten) : undefined });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) { location.reload(); throw new Error('Nicht angemeldet'); }
  if (!r.ok) throw new Error(j.error || 'Fehler ' + r.status);
  return j;
}
function meldung(t) { const m = $('#meldung'); m.textContent = t; m.hidden = false; clearTimeout(m._t); m._t = setTimeout(() => (m.hidden = true), 4000); }

// zweimal klicken statt Systemdialog: erster Klick fragt im Knopf selbst nach
function sicher(knopf, frage, tun) {
  if (knopf.dataset.frage) { delete knopf.dataset.frage; knopf.textContent = knopf.dataset.alt; tun(); return; }
  knopf.dataset.alt = knopf.textContent; knopf.dataset.frage = '1'; knopf.textContent = frage;
  setTimeout(() => { if (knopf.dataset.frage) { delete knopf.dataset.frage; knopf.textContent = knopf.dataset.alt; } }, 4000);
}

async function kopf() {
  const me = await api('GET', '/api/me');
  $('#wer').textContent = me.name;
  const k = $('#kosten');
  k.textContent = `Claude: ${eur(me.kosten)} / ${eur(me.deckel)}`;
  k.classList.toggle('warn', me.kosten >= me.deckel * 0.8);
  if (me.admin) k.title = 'Klick: Monatsdeckel ändern';
  k.onclick = me.admin ? deckelAendern : null;
  $('#portal').href = `${location.protocol}//${location.hostname}:8090/ava`;
  const kb = $('#kopie');
  kb.hidden = !me.admin || !me.kopie_moeglich;
  const z = me.kopie || {};
  kb.disabled = !!z.laeuft;
  kb.textContent = z.laeuft ? z.schritt || 'Kopie läuft …' : 'Echte Projekte neu holen';
  kb.title = z.fehler ? 'Letzter Versuch: ' + z.fehler : z.fertig ? 'Letzte Kopie: ' + z.fertig : '';
  if (z.laeuft) setTimeout(kopf, 3000);
}
function deckelAendern() {
  const k = $('#kosten');
  k.innerHTML = '<input type="number" min="0" step="5" style="width:70px"> € <button>OK</button>';
  const i = k.querySelector('input'); i.focus();
  k.onclick = null;
  k.querySelector('button').onclick = async (e) => { e.stopPropagation(); await api('PUT', '/api/deckel', { eur: Number(i.value) }); kopf(); };
}
$('#kopie').onclick = (e) => sicher(e.target, 'Wirklich? Ersetzt alle Daten hier', async () => {
  try { await api('POST', '/api/kopie'); meldung('Kopie gestartet – danach neu anmelden'); kopf(); } catch (x) { meldung(x.message); }
});

async function liste() {
  const L = await api('GET', '/api/gespraeche');
  $('#liste').innerHTML = L.map((g) => `<li data-id="${g.id}" class="${g.id === aktiv ? 'an' : ''}"><span>${esc(g.titel || 'Neues Gespräch')}</span><b class="weg" title="Löschen">✕</b></li>`).join('');
}
$('#liste').onclick = async (e) => {
  const li = e.target.closest('li'); if (!li) return;
  const id = Number(li.dataset.id);
  if (e.target.classList.contains('weg')) {
    return sicher(e.target, 'löschen?', async () => { await api('DELETE', `/api/gespraeche/${id}`); if (id === aktiv) { aktiv = null; leeren(); } liste(); });
  }
  oeffnen(id);
};
$('#neu').onclick = async () => { const r = await api('POST', '/api/gespraeche'); aktiv = r.id; await liste(); oeffnen(r.id); $('#text').focus(); };

function leeren() { $('#verlauf').innerHTML = '<p class="leer">Neues Gespräch oder eines links wählen.</p>'; zeigeVorschau([]); $('#schritte').innerHTML = ''; }

function eintrag(e) {
  return `<div class="msg ${e.wer}">${esc(e.text)}</div>`;
}
async function oeffnen(id) {
  aktiv = id;
  document.querySelectorAll('#liste li').forEach((li) => li.classList.toggle('an', Number(li.dataset.id) === id));
  const g = await api('GET', `/api/gespraeche/${id}`);
  $('#verlauf').innerHTML = g.eintraege.length ? g.eintraege.map(eintrag).join('') : '<p class="leer">Was soll Claude in der Ausschreibung tun?</p>';
  $('#verlauf').scrollTop = 1e9;
  zeigeVorschau(g.vorschau);
  zeigeSchritte(g.schritte);
  const s = await api('GET', `/api/gespraeche/${id}/stand`);
  if (s.laeuft) beobachten(id);
}

function zeigeVorschau(V) {
  $('#vorschau').innerHTML = V.length
    ? V.map((a) => `<div class="akt"><b>${esc(a.titel)}</b>${a.zeilen.length ? `<ol>${a.zeilen.map((z) => `<li>${esc(z)}</li>`).join('')}</ol>` : ''}</div>`).join('')
    : '<p class="leer">Noch nichts vorgemerkt. Claude ändert nichts, bevor du hier „Ausführen“ drückst.</p>';
  $('#knoepfe').hidden = !V.length;
}
const tag = (t) => (t ? `${t.slice(8, 10)}.${t.slice(5, 7)}. ${t.slice(11, 16)}` : '');
function zeigeSchritte(S) {
  const T = { ausgefuehrt: '', zurueck: 'zurückgenommen', teilweise: 'teilweise zurück', fehler: 'Fehler', laeuft: 'läuft' };
  $('#schritte').innerHTML = S.slice().reverse().map((s) => {
    let erg = []; try { erg = JSON.parse(s.ergebnis || '[]'); } catch { /* leer */ }
    const text = erg.map((x) => x.aktion).join(' · ') || s.fehler || '';
    return `<li><span class="${s.status === 'zurueck' ? 'zurueck' : ''}">${esc(tag(s.erledigt))} ${esc(text)}${T[s.status] ? ` <i>(${T[s.status]})</i>` : ''}</span>`
      + (s.status === 'ausgefuehrt' ? `<button class="leise" data-rueck="${s.id}">Rückgängig</button>` : '') + '</li>';
  }).join('');
}
$('#schritte').onclick = (e) => {
  const b = e.target.closest('[data-rueck]'); if (!b) return;
  sicher(b, 'Wirklich?', async () => {
    try { const r = await api('POST', `/api/schritte/${b.dataset.rueck}/rueckgaengig`); meldung(r.ok ? 'Zurückgenommen' : 'Teilweise: ' + r.fehler.join('; ')); } catch (x) { meldung(x.message); }
    oeffnen(aktiv);
  });
};

$('#ausfuehren').onclick = async (e) => {
  e.target.disabled = true;
  try { await api('POST', `/api/gespraeche/${aktiv}/ausfuehren`); meldung('Ausgeführt – im Bauportal sichtbar'); } catch (x) { meldung(x.message); }
  e.target.disabled = false;
  oeffnen(aktiv);
};
$('#verwerfen').onclick = (e) => sicher(e.target, 'Wirklich verwerfen?', async () => { await api('POST', `/api/gespraeche/${aktiv}/verwerfen`); oeffnen(aktiv); });

$('#eingabe').onsubmit = async (e) => {
  e.preventDefault();
  const text = $('#text').value.trim();
  if (!text) return;
  if (!aktiv) { const r = await api('POST', '/api/gespraeche'); aktiv = r.id; }
  try { await api('POST', `/api/gespraeche/${aktiv}/nachricht`, { text }); } catch (x) { return meldung(x.message); }
  $('#text').value = '';
  $('#verlauf').querySelector('.leer')?.remove();
  $('#verlauf').insertAdjacentHTML('beforeend', eintrag({ wer: 'ich', text }));
  $('#verlauf').scrollTop = 1e9;
  beobachten(aktiv);
};
$('#text').onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#eingabe').requestSubmit(); } };

function beobachten(id) {
  clearTimeout(warte);
  $('#arbeit').hidden = false;
  $('#eingabe button').disabled = true;
  const runde = async () => {
    if (id !== aktiv) { $('#arbeit').hidden = true; $('#eingabe button').disabled = false; return; }
    const s = await api('GET', `/api/gespraeche/${id}/stand`).catch(() => ({ laeuft: true, fortschritt: [] }));
    $('#arbeit-text').textContent = s.fortschritt.length ? s.fortschritt[s.fortschritt.length - 1] : 'Claude denkt nach …';
    if (s.laeuft) { warte = setTimeout(runde, 1200); return; }
    $('#arbeit').hidden = true; $('#eingabe button').disabled = false;
    await oeffnen(id); liste(); kopf();
    if (s.fehler) { $('#verlauf').insertAdjacentHTML('beforeend', eintrag({ wer: 'fehler', text: s.fehler })); $('#verlauf').scrollTop = 1e9; }
  };
  runde();
}

kopf(); liste(); leeren();
