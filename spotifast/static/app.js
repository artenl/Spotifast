// Main page: lookup -> preview -> job -> live progress -> download.
// Everything server-provided is written with textContent, never innerHTML.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const FINAL = new Set(["done", "partial", "failed", "cancelled", "expired"]);
  const STATUS = {
    queued: "En attente",
    running: "En cours",
    done: "Terminé",
    partial: "Incomplet",
    failed: "Échec",
    cancelled: "Annulé",
    expired: "Effacé",
  };
  const KIND = { track: "Titre", album: "Album", playlist: "Playlist" };

  const cards = new Map(); // job id -> { el, job, source }
  let preview = null;

  async function api(path, { method = "GET", body } = {}) {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("Session expirée.");
    }
    let data = {};
    try { data = await response.json(); } catch { /* empty body */ }
    if (!response.ok) throw new Error(data.error || `Erreur ${response.status}`);
    return data;
  }

  function setMsg(el, text, bad = false) {
    el.textContent = text || "";
    el.classList.toggle("bad", Boolean(bad));
  }

  function duration(ms) {
    if (!ms) return "";
    const s = Math.round(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  function safeImage(img, url) {
    if (url && url.startsWith("https://")) img.src = url;
    else img.removeAttribute("src");
  }

  function updateQuota(limits) {
    if (!limits) return;
    $("quota").textContent = `${limits.usedToday} / ${limits.dailyQuota} titres sur 24 h`;
  }

  // lookup & preview ---------------------------------------------------------

  $("lookup").addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = $("url").value.trim();
    if (!url) {
      setMsg($("lookup-msg"), "Colle d'abord un lien Spotify.", true);
      return;
    }
    $("lookup-btn").disabled = true;
    setMsg($("lookup-msg"), "Lecture de la pochette…");
    try {
      const data = await api("/api/preview", { method: "POST", body: { url } });
      renderPreview(data.preview, data.limits);
      setMsg($("lookup-msg"), "");
    } catch (err) {
      $("preview").hidden = true;
      setMsg($("lookup-msg"), err.message, true);
    } finally {
      $("lookup-btn").disabled = false;
    }
  });

  function renderPreview(p, limits) {
    preview = p;
    const total = p.totalCount;
    const plural = total > 1 ? "s" : "";
    $("pv-kind").textContent = KIND[p.kind] || p.kind;
    $("pv-spine").textContent = `${KIND[p.kind] || ""} · ${total} titre${plural}`;
    $("pv-name").textContent = p.name;
    $("pv-meta").textContent = [p.owner, `${total}${p.truncated && total <= limits.maxTracks ? "+" : ""} titre${plural}`]
      .filter(Boolean).join(" · ");
    safeImage($("pv-cover"), p.cover || (p.tracks[0] && p.tracks[0].cover));

    const list = $("pv-tracks");
    list.replaceChildren();
    for (const t of p.tracks) {
      const li = document.createElement("li");
      const title = document.createElement("span");
      title.className = "t";
      title.textContent = t.title;
      if (t.artists && p.kind !== "track") {
        const a = document.createElement("span");
        a.className = "a";
        a.textContent = ` — ${t.artists}`;
        title.append(a);
      }
      const d = document.createElement("span");
      d.className = "d";
      d.textContent = duration(t.durationMs);
      li.append(title, d);
      list.append(li);
    }
    $("pv-count").textContent = `Voir les ${p.tracks.length} titres`;
    $("pv-details").hidden = p.kind === "track" || p.tracks.length === 0;
    $("pv-details").open = false;

    const start = $("start");
    if (p.truncated) {
      start.disabled = true;
      const size = total > limits.maxTracks ? `Cette playlist contient ${total} titres` : "Trop long";
      setMsg($("start-msg"), `${size} : la limite est de ${limits.maxTracks} titres par copie. `
        + "Découpe-la en plusieurs playlists plus courtes.", true);
    } else {
      start.disabled = false;
      setMsg($("start-msg"), `${limits.usedToday} / ${limits.dailyQuota} titres utilisés sur 24 h.`);
    }
    $("preview").hidden = false;
    updateQuota(limits);
  }

  $("start").addEventListener("click", async () => {
    if (!preview) return;
    const start = $("start");
    start.disabled = true;
    setMsg($("start-msg"), "Mise en file…");
    try {
      const data = await api("/api/jobs", {
        method: "POST",
        body: {
          url: preview.url,
          format: document.querySelector('input[name="format"]:checked').value,
          quality: document.querySelector('input[name="quality"]:checked').value,
        },
      });
      upsert(data.job, true);
      updateQuota(data.limits);
      setMsg($("start-msg"), "");
      $("preview").hidden = true;
      $("url").value = "";
      preview = null;
    } catch (err) {
      setMsg($("start-msg"), err.message, true);
    } finally {
      start.disabled = false;
    }
  });

  // jobs ------------------------------------------------------------------------

  function upsert(job, prepend = false) {
    let card = cards.get(job.id);
    if (!card) {
      const el = $("job-tpl").content.firstElementChild.cloneNode(true);
      card = { el, job, source: null };
      cards.set(job.id, card);
      const list = $("job-list");
      if (prepend) list.prepend(el);
      else list.append(el);
      el.querySelector('[data-f="cancel"]').addEventListener("click", () => cancel(job.id));
      el.querySelector('[data-f="download"]').addEventListener("click", () => {
        // The server shortens the file's lifetime once fetched; refresh soon after.
        setTimeout(() => refresh(job.id), 4000);
      });
    }
    if (job.version < card.job.version) return; // stale
    card.job = job;
    render(card);
    $("jobs-empty").hidden = cards.size > 0;
    if (!FINAL.has(job.status)) listen(card);
  }

  function render(card) {
    const { el, job } = card;
    const f = (name) => el.querySelector(`[data-f="${name}"]`);
    el.className = `job ${job.status}`;
    f("title").textContent = job.title;
    f("label").textContent = `${job.title} · ${job.format.toUpperCase()}`;
    const badge = f("status");
    badge.className = `badge ${job.status}`;
    badge.textContent = STATUS[job.status] || job.status;

    const done = job.landed;
    const counts = [`${done} / ${job.trackTotal} titre${job.trackTotal > 1 ? "s" : ""}`];
    if (job.failed) counts.push(`${job.failed} introuvable${job.failed > 1 ? "s" : ""}`);
    counts.push(`${job.format.toUpperCase()} ${job.quality} kbps`);
    f("meta").textContent = `${KIND[job.kind] || ""} · ${counts.join(" · ")}`;

    const pct = job.trackTotal ? Math.min(100, Math.round(((done + job.failed) / job.trackTotal) * 100)) : 0;
    f("bar").style.width = `${FINAL.has(job.status) && job.status !== "failed" && job.status !== "cancelled" ? 100 : pct}%`;
    f("progress").setAttribute("aria-valuenow", String(pct));

    let message = job.message;
    if (!message && job.status === "queued") message = "En file d'attente : une autre copie occupe le serveur.";
    if (!message && job.status === "partial" && job.failedTitles.length) {
      message = `Introuvables : ${job.failedTitles.slice(0, 5).join(", ")}${job.failedTitles.length > 5 ? "…" : ""}`;
    }
    if (job.status === "expired") message = "Fichiers supprimés du serveur. Relance la copie si besoin.";
    f("message").textContent = message || "";

    const feed = f("feed");
    feed.replaceChildren();
    if (!FINAL.has(job.status)) {
      for (const item of job.recent.slice(-5).reverse()) {
        const li = document.createElement("li");
        li.textContent = item.artists ? `${item.title} — ${item.artists}` : item.title;
        if (!item.ok) li.className = "ko";
        feed.append(li);
      }
    }

    const dl = f("download");
    dl.hidden = !job.downloadable;
    if (job.downloadable) {
      dl.href = `/api/jobs/${encodeURIComponent(job.id)}/download`;
      dl.textContent = job.kind === "track" ? "Télécharger le titre" : "Télécharger le .zip";
    } else {
      dl.removeAttribute("href");
    }
    f("cancel").hidden = FINAL.has(job.status);
    renderExpiry(card);
  }

  function renderExpiry(card) {
    const el = card.el.querySelector('[data-f="expiry"]');
    const { job } = card;
    if (!job.downloadable || !job.expiresAt) {
      el.textContent = "";
      return;
    }
    const left = Math.max(0, job.expiresAt - Math.floor(Date.now() / 1000));
    el.textContent = left > 0
      ? `Effacé du serveur dans ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`
      : "Effacement en cours…";
  }

  function listen(card) {
    if (card.source) return;
    const source = new EventSource(`/api/jobs/${encodeURIComponent(card.job.id)}/events`);
    card.source = source;
    source.onmessage = (event) => {
      let job;
      try { job = JSON.parse(event.data); } catch { return; }
      upsert(job);
      if (FINAL.has(job.status)) {
        source.close();
        card.source = null;
        refreshAll();
      }
    };
    source.onerror = () => {
      // The browser retries on its own; if the job vanished, stop trying.
      if (source.readyState === EventSource.CLOSED) card.source = null;
    };
  }

  async function cancel(id) {
    if (!window.confirm("Annuler cette copie ?")) return;
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} });
      upsert(data.job);
    } catch (err) {
      window.alert(err.message);
    }
  }

  async function refresh(id) {
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}`);
      upsert(data.job);
    } catch { /* gone: next full refresh drops it */ }
  }

  async function refreshAll() {
    try {
      const data = await api("/api/jobs");
      const seen = new Set();
      for (const job of data.jobs.slice().reverse()) {
        seen.add(job.id);
        upsert(job, true);
      }
      for (const [id, card] of cards) {
        if (!seen.has(id)) {
          if (card.source) card.source.close();
          card.el.remove();
          cards.delete(id);
        }
      }
      $("jobs-empty").hidden = cards.size > 0;
      updateQuota(data.limits);
    } catch { /* offline for a moment; the timer tries again */ }
  }

  setInterval(() => cards.forEach(renderExpiry), 1000);
  setInterval(refreshAll, 30000);
  refreshAll();
})();
