/* Transport only. Jinja2 owns markup; HTMX performs out-of-band swaps. */
"use strict";
(() => {
  htmx.defineExtension("json-form", {
    onEvent(name, event) {
      if (name === "htmx:configRequest") event.detail.headers["Content-Type"] = "application/json";
    },
    encodeParameters(xhr, parameters, element) {
      const data = Object.fromEntries(new FormData(element));
      if (element.id === "strategy-form") {
        data.strategy = element.querySelector('[name="strategy"]').value;
        data.interval_minutes = Number(data.interval_minutes);
        data.pinned_only = element.querySelector('[name="pinned_only"]').checked;
      }
      if (element.id === "selection-form") {
        data.country = data.country || null;
        data.city = data.city.trim() || null;
        data.region = data.region.trim() || null;
        data.min_score = Number(data.min_score);
      }
      return JSON.stringify(data);
    }
  });

  const gatewayTests = new Map();
  function syncGatewayTests() {
    document.querySelectorAll("[data-gateway-test]").forEach(button => {
      const state = gatewayTests.get(button.dataset.gatewayTest);
      button.disabled = Boolean(state?.busy);
      button.textContent = state?.busy ? "Test en cours…" : "Tester la connexion";
      const output = document.querySelector(`[data-gateway-result="${button.dataset.gatewayTest}"]`);
      if (output) output.textContent = state?.message || "";
    });
  }
  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-gateway-test]");
    if (!button) return;
    const kind = button.dataset.gatewayTest;
    if (gatewayTests.get(kind)?.busy) return;
    gatewayTests.set(kind, {busy: true, message: "Vérification de bout en bout…"});
    syncGatewayTests();
    try {
      const response = await fetch(`/api/gateway/${kind}/test`, {method: "POST"});
      const result = await response.json();
      const message = response.ok ? result.message + (result.ok ? ` IP de sortie : ${result.exit_ip} · ${result.latency_ms} ms` : "") : "Test indisponible. Réessayez.";
      gatewayTests.set(kind, {busy: false, message});
    } catch {
      gatewayTests.set(kind, {busy: false, message: "Serveur inaccessible. Réessayez."});
    }
    syncGatewayTests();
  });
  document.addEventListener("htmx:afterSettle", syncGatewayTests);

  const status = document.getElementById("connection-status");
  const feedback = document.getElementById("feedback");
  const busyRows = new Map();
  const strategySelect = document.getElementById("strategy");
  let strategyDirty = false;
  const priorityCheckbox = document.getElementById("rotation-pinned-only");
  const strategyHelp = document.getElementById("strategy-help");
  function updateStrategyHelp() {
    if (strategyHelp && strategySelect) {
      strategyHelp.textContent = strategySelect.selectedOptions[0]?.dataset.description || "";
    }
  }
  strategySelect?.addEventListener("change", () => {
    strategyDirty = true;
    updateStrategyHelp();
  });
  function syncPriorityMode() {
    if (!strategySelect || !priorityCheckbox) return;
    strategySelect.disabled = priorityCheckbox.checked;
    if (priorityCheckbox.checked) strategySelect.value = "round_robin";
    updateStrategyHelp();
  }
  priorityCheckbox?.addEventListener("change", () => {
    strategyDirty = true;
    syncPriorityMode();
  });
  syncPriorityMode();
  updateStrategyHelp();

  function syncBusyRows() {
    document.querySelectorAll("[data-live-disabled]").forEach(button => {
      button.disabled = false;
      button.removeAttribute("data-live-disabled");
    });
    busyRows.forEach((count, id) => document.getElementById(id)?.querySelectorAll("button").forEach(button => {
      button.disabled = true;
      button.setAttribute("data-live-disabled", "true");
    }));
  }
  let needsSnapshot = false, updating = false, scheduled = null;
  let socket = null, reconnectTimer = null, stopped = false, lastSnapshot = 0;

  function showError(message) {
    feedback.textContent = message;
    feedback.setAttribute("role", "alert");
  }

  async function refresh() {
    scheduled = null;
    if (updating || stopped || busyRows.size) return;
    if (!document.getElementById("proxy-sort")) return;
    updating = true;
    const focusedElement = document.activeElement;
    const focusId = focusedElement?.closest("#proxy-rows") ? focusedElement.id : null;
    try {
      needsSnapshot = false;
      const params = new URLSearchParams(location.search);
      await htmx.ajax("GET", `/ui/snapshot?${params}`, {target: "#proxies-table", swap: "none"});
      lastSnapshot = Date.now();
      const countrySelect = document.getElementById("selection-country");
      if (countrySelect) {
        document.querySelectorAll("#activity-panel [data-country-code]").forEach(item => {
          const code = item.dataset.countryCode;
          if (![...countrySelect.options].some(option => option.value === code)) {
            countrySelect.add(new Option(item.dataset.countryName + " (" + code + ")", code));
          }
        });
      }
      if (feedback?.getAttribute("role") === "alert" && feedback.textContent.startsWith("Mise à jour")) {
        feedback.textContent = "";
        feedback.setAttribute("role", "status");
      }
      restoreChips();
    } catch {
      showError("Mise à jour interrompue. Une nouvelle tentative suivra la reconnexion.");
    } finally {
      updating = false;
      syncBusyRows();
      if (focusId && (document.activeElement === focusedElement ||
          (document.activeElement === document.body && !focusedElement.isConnected))) {
        document.getElementById(focusId)?.focus({preventScroll: true});
      }
      if (needsSnapshot) schedule();
    }
  }

  function schedule() {
    needsSnapshot = true;
    if (!scheduled && !updating) scheduled = setTimeout(refresh, 250);
  }

  const tableFilters = document.getElementById("table-filters");
  function applyTableFilters() {
    if (!tableFilters) return;
    const url = new URL(location.href);
    for (const field of ["city", "country", "status", "sort"]) {
      const value = tableFilters.elements.namedItem(field).value.trim();
      if (value) url.searchParams.set(field, value);
      else url.searchParams.delete(field);
    }
    history.replaceState(null, "", url);
    schedule();
  }
  tableFilters?.addEventListener("submit", event => {
    event.preventDefault();
    applyTableFilters();
  });
  tableFilters?.addEventListener("change", event => {
    if (event.target.tagName === "SELECT") applyTableFilters();
  });
  document.addEventListener("click", event => {
    if (!event.target.closest("[data-reset-table-filters]") || !tableFilters) return;
    event.preventDefault();
    for (const field of ["city", "country", "status"]) {
      tableFilters.elements.namedItem(field).value = "";
    }
    applyTableFilters();
  });
  window.addEventListener("popstate", () => {
    if (!tableFilters) return;
    const params = new URLSearchParams(location.search);
    for (const field of ["city", "country", "status", "sort"]) {
      tableFilters.elements.namedItem(field).value = params.get(field) || (field === "sort" ? "best" : "");
    }
    schedule();
  });

  function connect() {
    if (stopped) return;
    socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/events`);
    socket.onopen = () => {
      status.textContent = "En direct";
      status.classList.add("connected");
      schedule(true);
    };
    socket.onmessage = event => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (["proxy_updated", "proxy_removed", "check_result"].includes(message.type)) {
        schedule(false, message.payload.proxy_id);
      } else if (message.type === "pool_stats") {
        const select = document.getElementById("strategy");
        if (select && !strategyDirty && document.activeElement !== select) {
          select.value = message.payload.strategy;
          updateStrategyHelp();
        }
        // Periodic authoritative HTML snapshots repair missed/dropped events.
        if (Date.now() - lastSnapshot >= 1800) schedule(true);
      }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      status.textContent = "Reconnexion en cours…";
      status.classList.remove("connected");
      if (!stopped) reconnectTimer = setTimeout(connect, 2000);
    };
  }

  document.body.addEventListener("htmx:beforeRequest", event => {
    const element = event.detail.elt;
    const row = element.closest("#proxy-rows tr");
    if (row) {
      busyRows.set(row.id, (busyRows.get(row.id) || 0) + 1);
      syncBusyRows();
      event.detail.xhr.addEventListener("loadend", () => {
        const remaining = busyRows.get(row.id) - 1;
        if (remaining > 0) busyRows.set(row.id, remaining);
        else busyRows.delete(row.id);
        syncBusyRows();
        schedule(true);
      }, {once: true});
    }
    if (element.id === "strategy-form") {
      const submitted = strategySelect.value;
      event.detail.xhr.addEventListener("loadend", () => {
        if (event.detail.xhr.status >= 200 && event.detail.xhr.status < 300 && strategySelect.value === submitted) strategyDirty = false;
      }, {once: true});
    }
  });
  document.body.addEventListener("pool-refresh", () => schedule(true));

  // Keep per-row Tester chips visible for ~3s even if the tbody is
  // re-rendered by a snapshot swap in the meantime.
  const chipState = new Map(); // id -> {html, cls, ts}
  const chipTimers = new Map();
  const CHIP_TTL_MS = 3000;
  function freezeChip(id) {
    const el = document.getElementById(id);
    if (!el) return;
    chipState.set(id, {html: el.innerHTML, cls: el.className, ts: Date.now()});
    clearTimeout(chipTimers.get(id));
    chipTimers.set(id, setTimeout(() => fadeChip(id), CHIP_TTL_MS));
  }
  function fadeChip(id) {
    const el = document.getElementById(id);
    if (el) {
      el.classList.add("fading");
      setTimeout(() => {
        const still = document.getElementById(id);
        if (still && still.classList.contains("fading")) {
          still.className = "test-result";
          still.innerHTML = "";
        }
      }, 400);
    }
    chipState.delete(id);
    chipTimers.delete(id);
  }
  function restoreChips() {
    const now = Date.now();
    chipState.forEach((state, id) => {
      if (now - state.ts >= CHIP_TTL_MS) { chipState.delete(id); return; }
      const el = document.getElementById(id);
      if (!el) return;
      el.className = state.cls;
      el.innerHTML = state.html;
    });
  }
  document.body.addEventListener("click", async event => {
    const btn = event.target.closest("[data-test-proxy]");
    if (!btn) return;
    event.preventDefault();
    if (btn.disabled) return;
    const proxyId = btn.dataset.testProxy;
    const chipId = "test-result-" + proxyId;
    btn.disabled = true;
    try {
      const resp = await fetch(`/api/proxies/${proxyId}/test`, {
        method: "POST", headers: {"HX-Request": "true"},
      });
      const html = await resp.text();
      const temp = document.createElement("template");
      temp.innerHTML = html.trim();
      const newChip = temp.content.querySelector("#" + CSS.escape(chipId));
      if (newChip) {
        const current = document.getElementById(chipId);
        if (current) {
          current.className = newChip.className;
          current.textContent = newChip.textContent;
          current.dataset.fresh = "1";
          freezeChip(chipId);
        }
      }
      if (resp.headers.get("HX-Trigger") === "pool-refresh") {
        document.body.dispatchEvent(new Event("pool-refresh", {bubbles: true}));
      }
    } finally {
      btn.disabled = false;
    }
  });
  document.body.addEventListener("htmx:afterRequest", event => {
    if (event.detail.xhr.getResponseHeader("X-Clear-Import") === "true") {
      document.getElementById("import-form").reset();
      const drop = document.getElementById('proxy-drop');
      const hint = document.getElementById('proxy-file-hint');
      if (drop) drop.classList.remove('is-loaded');
      if (hint) { hint.innerHTML = ''; hint.dataset.state = ''; }
      document.querySelectorAll('.pulse-cta').forEach(el => el.classList.remove('pulse-cta'));
    }
  });
  document.body.addEventListener("htmx:responseError", event => {
    const code = event.detail.xhr.status;
    showError(code === 422 ? "Saisie invalide. Vérifiez les champs puis réessayez." : "Opération impossible. Vérifiez la connexion puis réessayez.");
  });
  document.body.addEventListener("htmx:sendError", () => showError("Serveur indisponible. Vérifiez que l’application est démarrée."));
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(scheduled);
    clearTimeout(reconnectTimer);
    if (socket) socket.close();
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) { stopped = false; connect(); }
  });


  // ── File drop / picker : concat text content into #proxy-text ──────────
  const drop = document.getElementById('proxy-drop');
  const fileInput = document.getElementById('proxy-file');
  const proxyText = document.getElementById('proxy-text');
  const dropHint = document.getElementById('proxy-file-hint');

  function countProxyLines(text) {
    return text.split(/\r?\n/).filter(line => line.trim() && !line.trim().startsWith('#')).length;
  }

  function setDropState(mode, html) {
    if (!dropHint) return;
    dropHint.innerHTML = html;
    dropHint.dataset.state = mode;
    const drop = document.getElementById('proxy-drop');
    if (drop) drop.classList.toggle('is-loaded', mode === 'loaded');
  }

  function highlightImportButton() {
    const btn = document.querySelector('#import-form button[type="submit"]');
    if (!btn) return;
    btn.classList.add('pulse-cta');
    btn.focus({preventScroll: true});
  }

  function appendLines(lines) {
    const current = proxyText.value.trim();
    const sep = current ? '\n' : '';
    proxyText.value = current + sep + lines.trim();
  }

  async function readFiles(files) {
    const names = [];
    const parts = [];
    let total = 0;
    for (const file of files) {
      if (file.size > 1_000_000) {
        setDropState('error', '<strong>Fichier trop volumineux</strong><span>' + file.name + ' dépasse 1 MB</span>');
        return;
      }
      total += file.size;
      names.push(file.name);
      parts.push(await file.text());
    }
    if (!parts.length) return;
    const combined = parts.join('\n');
    appendLines(combined);
    const kb = (total / 1024).toFixed(1);
    const proxies = countProxyLines(proxyText.value);
    const label = names.length === 1 ? names[0] : names.length + ' fichiers';
    setDropState('loaded',
      '<strong>✓ ' + label + ' chargé</strong>' +
      '<span>' + proxies + ' ligne(s) · ' + kb + ' KB — prêt à importer</span>' +
      '<span class="drop-cta-tip">Cliquez sur <em>Importer et vérifier</em> ci-dessous</span>'
    );
    highlightImportButton();
  }

  if (drop && fileInput && proxyText) {
    fileInput.addEventListener('change', () => readFiles(fileInput.files || []));
    drop.addEventListener('click', event => {
      if (event.target.closest('input, textarea, button, a, label')) return;
      event.preventDefault();
      fileInput.click();
    });
    drop.addEventListener('dragover', event => { event.preventDefault(); drop.classList.add('is-drag'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('is-drag'));
    drop.addEventListener('drop', event => {
      event.preventDefault();
      drop.classList.remove('is-drag');
      readFiles([...(event.dataTransfer?.files || [])]);
    });
    drop.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        fileInput.click();
      }
    });
  }

  if (document.getElementById("proxy-sort")) connect();
  else if (status) { status.textContent = ""; status.hidden = true; }
})();

  // ── Dichvusocks provider widget ───────────────────────────────────────
  (async function initDichvusocks() {
    const section = document.getElementById('dichvusocks-section');
    if (!section) return;
    const tokenInput  = document.getElementById('dichvusocks-token');
    const tokenHint   = document.getElementById('dichvusocks-token-hint');
    const saveTokenBtn= document.getElementById('dichvusocks-save-token');
    const clearTokenBtn=document.getElementById('dichvusocks-clear-token');
    const balanceCard = document.getElementById('dichvusocks-balance');
    const importForm  = document.getElementById('dichvusocks-form');

    let accountVersion = 0;
    function reveal(enabled) {
      accountVersion += 1;
      balanceCard.hidden = !enabled;
      importForm.hidden  = !enabled;
      clearTokenBtn.hidden = !enabled;
      section.dataset.enabled = enabled ? 'true' : 'false';
    }
    reveal(section.dataset.enabled === 'true');
    if (section.dataset.enabled !== 'true') {
      tokenHint.textContent = 'Aucun token enregistré. Cette connexion est facultative si vous importez vos proxies manuellement.';
    }

    saveTokenBtn && saveTokenBtn.addEventListener('click', async () => {
      const token = (tokenInput.value || '').trim();
      if (!token) { tokenHint.textContent = 'Collez un token non vide.'; tokenHint.className = 'helper error'; return; }
      saveTokenBtn.disabled = true;
      tokenHint.textContent = 'Enregistrement…';
      tokenHint.className = 'helper';
      try {
        const r = await fetch('/api/providers/dichvusocks/token', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ token }),
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        tokenInput.value = '';
        tokenHint.textContent = 'Token enregistré (chiffré).';
        tokenHint.className = 'helper good';
        reveal(true);
        await Promise.all([loadBalance(), loadCountries()]);
      } catch (err) {
        tokenHint.textContent = 'Enregistrement KO : ' + err.message;
        tokenHint.className = 'helper error';
      } finally {
        saveTokenBtn.disabled = false;
      }
    });

    clearTokenBtn && clearTokenBtn.addEventListener('click', async () => {
      if (!window.confirm('Oublier le token Dichvusocks ?')) return;
      try {
        const r = await fetch('/api/providers/dichvusocks/token', { method: 'DELETE' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        reveal(false);
        tokenHint.textContent = 'Token effacé.';
        tokenHint.className = 'helper';
      } catch (err) {
        tokenHint.textContent = 'Erreur : ' + err.message;
        tokenHint.className = 'helper error';
      }
    });
    const countrySel = document.getElementById('dichvusocks-country');
    const limitInput = document.getElementById('dichvusocks-limit');
    const previewBtn = document.getElementById('dichvusocks-preview');
    const importBtn  = document.getElementById('dichvusocks-import');
    const hint       = document.getElementById('dichvusocks-hint');
    const balanceEl  = document.querySelector('#dichvusocks-balance [data-field="balance"]');

    function setHint(msg, kind) {
      hint.textContent = msg || '';
      hint.className = 'helper' + (kind ? ' ' + kind : '');
    }

    async function readProvider(path) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 12000);
      try {
        const response = await fetch('/api/providers/dichvusocks/' + path, {signal: controller.signal});
        const body = await response.json();
        if (!response.ok) {
          const detail = typeof body.detail === 'string' ? body.detail : '';
          if (response.status === 401 || detail.includes('(401)')) {
            throw new Error('Token refusé. Vérifiez votre token Dichvusocks et enregistrez-le à nouveau.');
          }
          if (response.status === 403 || detail.includes('(403)')) {
            throw new Error('Accès refusé. Vérifiez l’état et le crédit de votre compte Dichvusocks.');
          }
          if (response.status === 429 || detail.includes('(429)')) {
            throw new Error('Trop de demandes. Patientez puis actualisez le compte.');
          }
          throw new Error('Service indisponible. Actualisez le compte pour réessayer.');
        }
        return body;
      } catch (error) {
        if (controller.signal.aborted) throw new Error('Délai dépassé. Actualisez le compte pour réessayer.');
        if (error instanceof SyntaxError || error instanceof TypeError) {
          throw new Error('Réponse indisponible. Vérifiez votre connexion puis actualisez le compte.');
        }
        throw error;
      } finally { clearTimeout(timer); }
    }

    async function loadBalance() {
      const version = accountVersion;
      balanceEl.textContent = 'Chargement du compte…';
      try {
        const body = await readProvider('user-info');
        const u = body && (body.user || body.data || body);
        if (!u || typeof u !== 'object' || Array.isArray(u)) {
          throw new Error('Réponse du compte invalide. Actualisez pour réessayer.');
        }
        const pts = u.point ?? '—';
        const views = u.totalView ?? '—';
        const plan = u.plan ?? '—';
        if (version !== accountVersion) return;
        balanceEl.textContent = `${u.username || 'compte'} · ${pts} pts · plan ${plan} · ${views} vues`;
      } catch (err) {
        if (version === accountVersion) balanceEl.textContent = err.message;
      }
    }

    async function loadCountries() {
      const version = accountVersion;
      try {
        const list = await readProvider('countries');
        if (!Array.isArray(list) || list.some(c => typeof c !== 'string')) {
          throw new Error('Liste des pays invalide. Actualisez pour réessayer.');
        }
        if (version !== accountVersion) return;
        const selected = countrySel.value;
        const all = document.createElement('option');
        all.value = '';
        all.textContent = 'Tous';
        countrySel.replaceChildren(all);
        for (const c of new Set(list)) {
          const opt = document.createElement('option');
          opt.value = c;
          opt.textContent = c;
          countrySel.appendChild(opt);
        }
        countrySel.value = list.includes(selected) ? selected : '';
      } catch (err) {
        if (version === accountVersion) setHint('Pays indisponibles : ' + err.message, 'error');
      }
    }

    const refreshBtn = document.getElementById('dichvusocks-refresh');
    refreshBtn?.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      setHint('');
      try { await Promise.all([loadBalance(), loadCountries()]); }
      finally { refreshBtn.disabled = false; }
    });

    previewBtn.addEventListener('click', async () => {
      setHint('Aperçu en cours…');
      previewBtn.disabled = true;
      const params = new URLSearchParams({
        limit: limitInput.value || '5',
      });
      if (countrySel.value) params.set('country', countrySel.value);
      for (const field of ['region', 'city']) {
        const value = document.getElementById('dichvusocks-' + field).value.trim();
        if (value) params.set(field, value);
      }
      try {
        const r = await fetch('/api/providers/dichvusocks/preview?' + params);
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || 'HTTP ' + r.status);
        setHint(`${body.length} sock(s) disponible(s) pour ${[params.get('city'), params.get('region'), countrySel.value].filter(Boolean).join(', ') || 'tous les pays'}. Aucun crédit débité.`, 'good');
      } catch (err) {
        setHint('Aperçu KO : ' + err.message, 'error');
      } finally {
        previewBtn.disabled = false;
      }
    });

    importBtn.addEventListener('click', async () => {
      const n = Number(limitInput.value);
      if (!Number.isInteger(n) || n < 1 || n > 50) { setHint('Choisissez un nombre entre 1 et 50.', 'error'); return; }
      if (!window.confirm(`Importer ${n} proxy(s) ? Cette opération peut consommer des crédits sur votre compte Dichvusocks.`)) return;
      setHint('Import en cours…');
      importBtn.disabled = true;
      const params = new URLSearchParams({ limit: String(n) });
      if (countrySel.value) params.set('country', countrySel.value);
      for (const field of ['region', 'city']) {
        const value = document.getElementById('dichvusocks-' + field).value.trim();
        if (value) params.set(field, value);
      }
      try {
        const r = await fetch('/api/providers/dichvusocks/import?' + params, { method: 'POST' });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || 'HTTP ' + r.status);
        setHint(`Importés : ${body.imported} · dédupliqués : ${body.deduped ?? 0} · ignorés : ${(body.skipped || []).length}`, 'good');
        document.body.dispatchEvent(new Event("pool-refresh"));
        await loadBalance();
      } catch (err) {
        setHint('Import KO : ' + err.message, 'error');
      } finally {
        importBtn.disabled = false;
      }
    });

    if (section.dataset.enabled === 'true') {
      await Promise.all([loadBalance(), loadCountries()]);
    }
  })();

