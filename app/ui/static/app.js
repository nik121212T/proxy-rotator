/* Transport only. Jinja2 owns markup; HTMX performs out-of-band swaps. */
"use strict";
(() => {
  htmx.defineExtension("json-form", {
    onEvent(name, event) {
      if (name === "htmx:configRequest") event.detail.headers["Content-Type"] = "application/json";
    },
    encodeParameters(xhr, parameters, element) {
      return JSON.stringify(Object.fromEntries(new FormData(element)));
    }
  });

  const status = document.getElementById("connection-status");
  const feedback = document.getElementById("feedback");
  const pendingRows = new Set();
  const busyRows = new Map();
  const strategySelect = document.getElementById("strategy");
  let strategyDirty = false;
  strategySelect.addEventListener("change", () => { strategyDirty = true; });

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
    updating = true;
    const focusedElement = document.activeElement;
    const focusId = focusedElement?.closest("#proxy-rows") ? focusedElement.id : null;
    try {
      if (needsSnapshot) {
        needsSnapshot = false;
        pendingRows.clear();
        await htmx.ajax("GET", "/ui/snapshot", {target: "#proxies-table", swap: "none"});
        lastSnapshot = Date.now();
      } else {
        const ids = [...pendingRows];
        pendingRows.clear();
        for (const id of ids) {
          const append = !document.getElementById(`proxy-${id}`);
          const empty = Boolean(document.getElementById("empty-row"));
          await htmx.ajax("GET", `/ui/proxies/${id}?append=${append}&empty=${empty}`, {target: "#proxies-table", swap: "none"});
        }
      }
    } catch {
      showError("Mise à jour interrompue. Une nouvelle tentative suivra la reconnexion.");
    } finally {
      updating = false;
      syncBusyRows();
      if (focusId && (document.activeElement === focusedElement ||
          (document.activeElement === document.body && !focusedElement.isConnected))) {
        document.getElementById(focusId)?.focus({preventScroll: true});
      }
      if (needsSnapshot || pendingRows.size) schedule();
    }
  }

  function schedule(full = false, id = null) {
    if (full) needsSnapshot = true;
    if (Number.isSafeInteger(id) && id > 0) pendingRows.add(id);
    if (!scheduled && !updating) scheduled = setTimeout(refresh, 80);
  }

  function connect() {
    if (stopped) return;
    socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/events`);
    socket.onopen = () => {
      status.textContent = "● En direct";
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
        if (!strategyDirty && document.activeElement !== select) select.value = message.payload.strategy;
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
  document.body.addEventListener("htmx:afterRequest", event => {
    if (event.detail.xhr.getResponseHeader("X-Clear-Import") === "true") {
      document.getElementById("import-form").reset();
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
  connect();
})();
