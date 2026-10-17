"use strict";
(() => {
  const form = document.getElementById("import-form");
  if (!form) return;
  const button = form.querySelector('button[type="submit"]');
  const state = document.getElementById("import-state");
  const counts = document.getElementById("import-counts");
  const bar = document.getElementById("import-progress");
  const elapsed = document.getElementById("import-elapsed");
  const feedback = document.getElementById("import-feedback");
  let current = null;
  let timer;
  let clock;
  const format = value => Number(value).toLocaleString("fr-FR");
  const outcome = (number, word) => `${format(number)} ${word}${number === 1 ? "" : "s"}`;
  async function poll(job) {
    if (current !== job) return;
    try {
      const response = await fetch("/api/proxies/imports/" + job.id, {cache: "no-store", signal: AbortSignal.timeout(5000)});
      if (current !== job) return;
      if (response.ok) {
        const data = await response.json();
        if (current !== job) return;
        job.latest = data;
        if (data.stage === "testing" || data.stage === "saving" || data.stage === "done") {
          bar.max = Math.max(1, data.total);
          bar.value = data.completed;
          counts.textContent = `${format(data.completed)} / ${format(data.total)} testés · ${outcome(data.completed - data.failed, "réussi")} · ${outcome(data.failed, "échec")}`;
          state.textContent = data.stage === "testing" ? "Vérification des nouveaux proxies…" : "Enregistrement des proxies disponibles…";
        }
      }
    } catch {
      if (current === job) state.textContent = "Suivi momentanément indisponible — import toujours en attente…";
    }
    if (current === job) timer = setTimeout(() => poll(job), 700);
  }
  form.addEventListener("htmx:configRequest", event => {
    if (current) { event.preventDefault(); return; }
    const job = {id: crypto.randomUUID(), started: Date.now(), latest: null};
    current = job;
    event.detail.headers["X-Import-ID"] = job.id;
  });
  form.addEventListener("htmx:beforeRequest", () => {
    const job = current;
    if (!job) return;
    form.setAttribute("aria-busy", "true");
    button.textContent = "Import en cours…";
    feedback.replaceChildren();
    state.textContent = "Analyse de la liste…";
    counts.textContent = "Patientez pendant la vérification. Gardez cette page ouverte.";
    bar.classList.remove("is-idle");
    bar.removeAttribute("value");
    elapsed.textContent = "0 s";
    clock = setInterval(() => {
      elapsed.textContent = format(Math.floor((Date.now() - job.started) / 1000)) + " s";
    }, 1000);
    timer = setTimeout(() => poll(job), 150);
  });
  form.addEventListener("htmx:afterRequest", event => {
    clearTimeout(timer);
    clearInterval(clock);
    const success = event.detail.successful;
    const last = current?.latest;
    current = null;
    form.removeAttribute("aria-busy");
    button.textContent = "Tester et importer les disponibles";
    state.textContent = success ? "Vérification terminée" : "Import non confirmé";
    bar.max = 1;
    bar.value = success ? 1 : 0;
    counts.textContent = success ? "Consultez le résultat ci-dessous." : "Votre liste est conservée. Vérifiez le résultat avant de relancer.";
    if (!success) {
      feedback.textContent = event.detail.xhr.status === 422 ? "Saisie invalide : vérifiez la liste et le protocole choisi." : "La réponse de l’import n’a pas été reçue. Vérifiez le pool avant de réessayer.";
    } else if (last?.completed === last?.total && last?.total > 0) {
      counts.textContent = `${format(last.completed)} testés · ${outcome(last.completed - last.failed, "réussi")} · ${outcome(last.failed, "échec")}`;
    }
  });
  window.addEventListener("pagehide", () => { current = null; clearTimeout(timer); clearInterval(clock); });
})();
