/* YT Relay frontend.
 *
 * Single-page app: passphrase gate, URL submission, status polling,
 * result link, recent-jobs list — all in localStorage.
 */

(() => {
    "use strict";

    // ----- Config: backend URL is the named Cloudflare Tunnel host. -----
    const BACKEND = "https://yt.rentskill.ai";

    const LS_PASS = "yt-relay.pass";
    const LS_HISTORY = "yt-relay.history";
    const LS_ACTIVE_JOB = "yt-relay.activeJob";
    const POLL_MS = 1500;
    const MAX_HISTORY = 10;

    // ----- DOM -----
    const $ = (id) => document.getElementById(id);

    const authGate = $("auth-gate");
    const authForm = $("auth-form");
    const authPass = $("auth-password");
    const authError = $("auth-error");

    const app = $("app");
    const logoutBtn = $("logout-btn");

    const downloadForm = $("download-form");
    const urlInput = $("url-input");
    const submitBtn = $("submit-btn");
    const formError = $("form-error");

    const jobPanel = $("job-panel");
    const jobStatus = $("job-status");
    const jobFileRow = $("job-file-row");
    const jobFile = $("job-file");
    const jobSizeRow = $("job-size-row");
    const jobSize = $("job-size");
    const jobIdEl = $("job-id");
    const jobProgress = $("job-progress");
    const jobResult = $("job-result");
    const jobError = $("job-error");
    const resultUrl = $("result-url");
    const copyBtn = $("copy-btn");
    const openBtn = $("open-btn");

    const dismissBtn = $("dismiss-btn");

    const queuePanel = $("queue-panel");
    const queueList = $("queue-list");
    const queueCount = $("queue-count");

    const historyList = $("history");
    const historyEmpty = $("history-empty");

    // ----- State -----
    let pollTimer = null;
    let queueTimer = null;
    const QUEUE_POLL_MS = 3000;

    // ----- Helpers -----
    const fmtBytes = (n) => {
        if (!Number.isFinite(n) || n <= 0) return "";
        const units = ["B", "KB", "MB", "GB"];
        let i = 0;
        let v = n;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
    };

    const show = (el) => el.classList.remove("hidden");
    const hide = (el) => el.classList.add("hidden");

    const setError = (el, msg) => {
        if (!msg) { hide(el); el.textContent = ""; return; }
        el.textContent = msg;
        show(el);
    };

    // ----- Auth gating (client side only — the real check is server-side) -----
    const unlock = (pass) => {
        localStorage.setItem(LS_PASS, pass);
        hide(authGate);
        show(app);
        renderHistory();
        startQueuePolling();
        const active = localStorage.getItem(LS_ACTIVE_JOB);
        if (active) attachJob(active);
    };

    const lock = () => {
        localStorage.removeItem(LS_PASS);
        localStorage.removeItem(LS_ACTIVE_JOB);
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        if (queueTimer) { clearTimeout(queueTimer); queueTimer = null; }
        hide(app);
        show(authGate);
        authPass.value = "";
        setError(authError, "");
    };

    authForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const pass = authPass.value.trim();
        if (!pass) return;
        // The server is the source of truth — we just stash and let
        // the first real request validate. Bad passphrases bounce there.
        unlock(pass);
    });

    logoutBtn.addEventListener("click", lock);

    // ----- Backend calls -----
    const startDownload = async (url) => {
        const pass = localStorage.getItem(LS_PASS) || "";
        const res = await fetch(`${BACKEND}/api/download`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, passphrase: pass }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            const err = new Error(body.error || `HTTP ${res.status}`);
            err.status = res.status;
            throw err;
        }
        return body;
    };

    const fetchStatus = async (jobId) => {
        const res = await fetch(`${BACKEND}/api/status/${encodeURIComponent(jobId)}`);
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${res.status}`);
        }
        return res.json();
    };

    // ----- Job lifecycle -----
    const STATUS_LABEL = {
        queued: "Queued",
        downloading: "Downie is downloading…",
        uploading: "Uploading to litterbox…",
        done: "Done",
        error: "Error",
    };

    const renderJob = (j) => {
        show(jobPanel);
        jobStatus.textContent = STATUS_LABEL[j.status] || j.status;
        jobStatus.dataset.state = j.status;
        jobIdEl.textContent = j.id;

        if (j.filename) {
            jobFile.textContent = j.filename;
            jobFileRow.hidden = false;
        } else {
            jobFileRow.hidden = true;
        }

        if (j.bytes) {
            jobSize.textContent = fmtBytes(j.bytes);
            jobSizeRow.hidden = false;
        } else {
            jobSizeRow.hidden = true;
        }

        jobProgress.classList.toggle("is-done", j.status === "done");
        jobProgress.classList.toggle("is-error", j.status === "error");

        if (j.status === "done" && j.upload_url) {
            show(jobResult);
            resultUrl.value = j.upload_url;
            openBtn.href = j.upload_url;
            setError(jobError, "");
        } else {
            hide(jobResult);
        }

        if (j.status === "error") {
            setError(jobError, j.error || "Something went wrong.");
        } else {
            setError(jobError, "");
        }
    };

    const attachJob = (jobId) => {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        localStorage.setItem(LS_ACTIVE_JOB, jobId);
        renderJob({ id: jobId, status: "queued" });

        const poll = async () => {
            try {
                const j = await fetchStatus(jobId);
                renderJob(j);
                if (j.status === "done") {
                    addToHistory({ id: j.id, url: j.upload_url, filename: j.filename, ts: Date.now() });
                    localStorage.removeItem(LS_ACTIVE_JOB);
                    return;
                }
                if (j.status === "error") {
                    localStorage.removeItem(LS_ACTIVE_JOB);
                    return;
                }
            } catch (e) {
                // Transient network errors: keep polling, but show the message.
                setError(jobError, `Status fetch failed: ${e.message}. Retrying…`);
            }
            pollTimer = setTimeout(poll, POLL_MS);
        };
        pollTimer = setTimeout(poll, POLL_MS);
    };

    downloadForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const url = urlInput.value.trim();
        if (!url) return;
        submitBtn.disabled = true;
        setError(formError, "");
        try {
            const { id } = await startDownload(url);
            urlInput.value = "";
            attachJob(id);
        } catch (e) {
            if (e.status === 401) {
                setError(formError, "Access code rejected. Lock the page and re-enter it.");
            } else if (e.status === 429) {
                setError(formError, e.message);
            } else {
                setError(formError, e.message || "Request failed.");
            }
        } finally {
            submitBtn.disabled = false;
        }
    });

    // ----- Active queue (public bulletin) -----
    const ytIdFromUrl = (u) => {
        try {
            const url = new URL(u);
            if (url.hostname === "youtu.be") return url.pathname.slice(1);
            return url.searchParams.get("v") || url.pathname.split("/").pop() || u;
        } catch { return u; }
    };

    const ageLabel = (createdAt) => {
        const sec = Math.max(0, Math.floor(Date.now() / 1000 - createdAt));
        if (sec < 60) return `${sec}s`;
        if (sec < 3600) return `${Math.floor(sec / 60)}m`;
        return `${Math.floor(sec / 3600)}h`;
    };

    const renderQueue = (items) => {
        if (!items || items.length === 0) {
            hide(queuePanel);
            queueList.innerHTML = "";
            queueCount.textContent = "";
            return;
        }
        show(queuePanel);
        queueCount.textContent = `${items.length}`;
        const mineId = localStorage.getItem(LS_ACTIVE_JOB);
        queueList.innerHTML = "";
        for (const it of items) {
            const li = document.createElement("li");
            li.className = "queue-item" + (it.id === mineId ? " is-mine" : "");

            const status = document.createElement("span");
            status.className = "q-status";
            status.dataset.state = it.status;
            status.textContent = it.status;
            li.appendChild(status);

            const title = document.createElement("span");
            title.className = "q-title";
            title.textContent = it.filename || ytIdFromUrl(it.yt_url);
            li.appendChild(title);

            const age = document.createElement("span");
            age.className = "q-age";
            age.textContent = ageLabel(it.created_at);
            li.appendChild(age);

            queueList.appendChild(li);
        }
    };

    const pollQueue = async () => {
        try {
            const res = await fetch(`${BACKEND}/api/active`, { cache: "no-store" });
            const body = await res.json();
            renderQueue(body.active || []);
        } catch {
            // Network blip — leave the previous render in place.
        }
        queueTimer = setTimeout(pollQueue, QUEUE_POLL_MS);
    };

    const startQueuePolling = () => {
        if (queueTimer) return;
        pollQueue();
    };

    // ----- Dismiss / reset stuck or completed job -----
    const dismissActiveJob = () => {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        localStorage.removeItem(LS_ACTIVE_JOB);
        hide(jobPanel);
        hide(jobResult);
        setError(jobError, "");
        setError(formError, "");
    };

    dismissBtn.addEventListener("click", dismissActiveJob);

    // ----- Clipboard -----
    copyBtn.addEventListener("click", async () => {
        const url = resultUrl.value;
        if (!url) return;
        try {
            await navigator.clipboard.writeText(url);
            const orig = copyBtn.textContent;
            copyBtn.textContent = "Copied";
            setTimeout(() => { copyBtn.textContent = orig; }, 1200);
        } catch {
            resultUrl.select();
            document.execCommand("copy");
        }
    });

    // ----- History -----
    const loadHistory = () => {
        try {
            const raw = localStorage.getItem(LS_HISTORY);
            return raw ? JSON.parse(raw) : [];
        } catch {
            return [];
        }
    };

    const saveHistory = (items) => {
        localStorage.setItem(LS_HISTORY, JSON.stringify(items.slice(0, MAX_HISTORY)));
    };

    const addToHistory = (item) => {
        const items = loadHistory().filter((x) => x.id !== item.id);
        items.unshift(item);
        saveHistory(items);
        renderHistory();
    };

    const renderHistory = () => {
        const items = loadHistory();
        historyList.innerHTML = "";
        if (items.length === 0) {
            show(historyEmpty);
            return;
        }
        hide(historyEmpty);
        for (const it of items) {
            const li = document.createElement("li");
            li.className = "history-item";

            const title = document.createElement("span");
            title.className = "h-title";
            title.textContent = it.filename || it.id;
            li.appendChild(title);

            if (it.url) {
                const link = document.createElement("a");
                link.href = it.url;
                link.target = "_blank";
                link.rel = "noopener";
                link.textContent = "open";
                li.appendChild(link);
            }
            historyList.appendChild(li);
        }
    };

    // ----- Boot -----
    if (localStorage.getItem(LS_PASS)) {
        unlock(localStorage.getItem(LS_PASS));
    } else {
        show(authGate);
        hide(app);
    }
})();
