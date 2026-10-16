"use strict";
(() => {
  const form = document.getElementById("browser-launch");
  if (!form) return;
  const choice = document.getElementById("browser-choice");
  const mode = document.getElementById("browser-mode");
  const proxies = document.getElementById("browser-proxies");
  const open = document.getElementById("browser-open");
  const feedback = document.getElementById("browser-feedback");
  let opening = false;
  let refreshing = false;
  let loaded = false;
  function message(text, error = false) {
    feedback.textContent = text;
    feedback.className = error ? "error" : "helper";
  }
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Demande invalide. Vérifiez votre sélection.");
    return data;
  }
  function updateMode() {
    document.getElementById("browser-proxy-field").hidden = mode.value !== "fixed";
    document.getElementById("browser-mode-help").textContent = mode.value === "fixed"
      ? "Chaque proxy choisi ouvre sa propre session, sans modifier la rotation des autres fenêtres."
      : "Utilise les réglages de rotation et de sélection du pool. Les connexions déjà ouvertes gardent leur proxy.";
    open.disabled = opening || !choice.value || (mode.value === "fixed" && !proxies.selectedOptions.length);
  }
  function sizeLabel(bytes) {
    if (bytes == null) return "Taille indisponible";
    const unit = bytes >= 1024 ** 3 ? "Go" : "Mo";
    const value = bytes / (unit === "Go" ? 1024 ** 3 : 1024 ** 2);
    return value.toLocaleString("fr-FR", {maximumFractionDigits: 1}) + " " + unit;
  }
  function render(data) {
    const previous = choice.value;
    choice.replaceChildren(new Option("Choisir un navigateur…", ""));
    const installed = document.createElement("optgroup"); installed.label = "Installés sur cet ordinateur";
    for (const browser of data.installed) {
      const option = new Option(browser.name + (browser.supported ? "" : " — proxy isolé non pris en charge"), "installed:" + browser.id);
      option.disabled = !browser.supported; installed.append(option);
    }
    choice.append(installed);
    const dedicated = document.createElement("optgroup"); dedicated.label = "Dédiés à l’app";
    const engines = document.getElementById("browser-engines"); engines.replaceChildren();
    for (const engine of data.dedicated) {
      const option = new Option(engine.name + (engine.ready ? "" : " — à télécharger"), "dedicated:" + engine.id);
      option.disabled = !engine.ready; dedicated.append(option);
      const row = document.createElement("div"); row.className = "browser-engine-row";
      const label = document.createElement("span");
      label.textContent = engine.name + " · " + ({installing:"Téléchargement en cours…",error:engine.error || "Téléchargement échoué — réessayez"}[engine.status] || (engine.ready ? "Téléchargé" : "Non téléchargé"));
      if (engine.removing) label.textContent = engine.name + " · Suppression en cours…";
      const info = document.createElement("div"); info.className = "browser-engine-info";
      info.append(label);
      if (engine.ready || engine.size_bytes) {
        const size = document.createElement("span"); size.className = "helper browser-engine-size";
        size.textContent = (engine.size_bytes == null ? "Taille indisponible" : sizeLabel(engine.size_bytes) + " installés") + (engine.in_use ? " · En cours d’utilisation" : "");
        info.append(size);
      }
      row.append(info);
      if ((engine.ready || engine.size_bytes) && !engine.removing) {
        const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary danger";
        remove.textContent = "Supprimer";
        remove.setAttribute("aria-label", "Supprimer le moteur " + engine.name);
        remove.disabled = !engine.can_remove;
        if (engine.in_use) remove.title = "Fermez les sessions de ce moteur avant de le supprimer.";
        remove.addEventListener("click", async () => {
          if (!window.confirm("Supprimer " + engine.name + " (" + sizeLabel(engine.size_bytes) + ") ? Vous pourrez le télécharger à nouveau. Ce moteur peut être partagé avec d’autres applications Playwright.")) return;
          remove.disabled = true; remove.textContent = "Suppression…";
          try {
            await request("/api/browsers/engines/" + engine.id, {method:"DELETE"});
            message(engine.name + " supprimé. Vous pouvez le télécharger à nouveau.");
          } catch (error) { message(error.message, true); }
          finally { await refresh(); }
        });
        row.append(remove);
      }
      if ((!engine.ready || engine.status === "installing") && !engine.removing) {
        const button = document.createElement("button"); button.type = "button"; button.className = "secondary";
        button.textContent = engine.status === "installing" ? "Téléchargement…" : "Télécharger";
        button.disabled = engine.status === "installing";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await request("/api/browsers/install/" + engine.id, {method:"POST"}); message("Téléchargement de " + engine.name + " lancé."); await refresh(); }
          catch (error) { message(error.message, true); button.disabled = false; }
        });
        row.append(button);
      }
      engines.append(row);
    }
    choice.append(dedicated);
    if ([...choice.options].some(option => option.value === previous && !option.disabled)) choice.value = previous;
    choice.disabled = opening;
    document.getElementById("browser-detection").textContent = data.installed.length ? "Navigateurs détectés automatiquement. Chaque ouverture utilise un profil séparé." : "Aucun navigateur installé reconnu. Téléchargez un moteur dédié.";
    const selected = new Set([...proxies.selectedOptions].map(option => option.value));
    proxies.replaceChildren();
    for (const proxy of data.proxies) {
      const label = "#" + proxy.id + " · " + (proxy.exit_ip || proxy.host + ":" + proxy.port) + " · " + (proxy.country || "pays inconnu");
      const option = new Option(label, String(proxy.id)); option.selected = selected.has(option.value); proxies.append(option);
    }
    const sessions = document.getElementById("browser-sessions"); sessions.replaceChildren();
    if (!data.sessions.length) { const empty = document.createElement("p"); empty.textContent = "Aucune session ouverte depuis l’app."; sessions.append(empty); }
    for (const session of data.sessions) {
      const row = document.createElement("div"); row.className = "browser-session-row";
      const label = document.createElement("span");
      label.textContent = session.name + " · " + (session.mode === "fixed" ? "Proxy #" + session.proxy_id + (session.exit_ip ? " · IP vérifiée : " + session.exit_ip : "") : "Rotation du pool") + (session.available ? "" : " · proxy indisponible") + (session.close_error ? " · " + session.close_error : "");
      const close = document.createElement("button"); close.type = "button"; close.className = "secondary"; close.textContent = session.close_error ? "Réessayer la fermeture" : "Fermer";
      close.addEventListener("click", async () => {
        close.disabled = true;
        try { await request("/api/browsers/sessions/" + session.id, {method:"DELETE"}); await refresh(); }
        catch (error) { message(error.message, true); close.disabled = false; }
      });
      row.append(label, close); sessions.append(row);
    }
    updateMode();
  }
  async function refresh() {
    if (refreshing || opening) return;
    refreshing = true;
    try {
      render(await request("/api/browsers"));
      if (!loaded) { message("Choisissez votre navigateur et votre connexion."); loaded = true; }
      document.getElementById("connection-status").textContent = "Connecté";
    } catch (error) { message(error.message || "Serveur inaccessible.", true); }
    finally { refreshing = false; }
  }
  mode.addEventListener("change", updateMode);
  choice.addEventListener("change", updateMode);
  proxies.addEventListener("change", updateMode);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (opening || !choice.value) return;
    const ids = [...proxies.selectedOptions].map(option => Number(option.value));
    if (mode.value === "fixed" && (!ids.length || ids.length > 8)) { message("Choisissez entre 1 et 8 proxies.", true); return; }
    const [source, browser_id] = choice.value.split(":");
    opening = true; choice.disabled = true; updateMode(); message("Ouverture du navigateur…");
    try {
      const data = await request("/api/browsers/sessions", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({source,browser_id,mode:mode.value,proxy_ids:ids,url:document.getElementById("browser-url").value})});
      message(data.opened + " session(s) ouverte(s). Les fenêtres apparaissent sur cet ordinateur.");
    } catch (error) { message(error.message, true); }
    finally { opening = false; choice.disabled = false; await refresh(); updateMode(); }
  });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 3000);
})();
