import { useState, useEffect, useRef, useCallback } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";

const API = "http://127.0.0.1:8000";

type LlmStatus =
  | { state: "starting" }
  | { state: "needs_model" }
  | { state: "unsupported_cpu"; missing: string[]; message: string }
  | { state: "exited"; message: string };

type ModelStatus = {
  state: "missing" | "downloading" | "verifying" | "ready" | "error";
  downloaded: number;
  total: number;
  error: string | null;
  model: string;
  license: string;
};

type Citation = { page: number; text: string; cited?: boolean };

type Message = {
  role: "user" | "ai";
  content: string;
  citations?: Citation[];
  error?: boolean;
};

// Outside Tauri (plain browser during `vite` dev) there is no sidecar control;
// the UI then relies on the backend's /model and /llm endpoints only.
type Phase = "checking" | "needs_model" | "downloading" | "loading" | "ready" | "problem";

const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
const mb = (n: number) => Math.round(n / 1024 / 1024).toLocaleString();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function llmStatus(): Promise<LlmStatus | null> {
  if (!isTauri) return null;
  return invoke<LlmStatus>("llm_status").catch(() => null);
}

async function getJSON<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${API}${path}`);
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}

function App() {
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  const [phase, setPhase] = useState<Phase>("checking");
  const [problem, setProblem] = useState<string | null>(null);
  const [model, setModel] = useState<ModelStatus | null>(null);

  const [doc, setDoc] = useState<{ id: number; name: string; pages: number } | null>(null);
  const [uploading, setUploading] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [isDragging, setIsDragging] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [openCite, setOpenCite] = useState<string | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isProcessing]);

  /** Wait for llama-server to answer /health (via the backend). */
  const waitForLlm = useCallback(async () => {
    setPhase("loading");
    for (let i = 0; i < 180; i++) {
      const st = await llmStatus();
      if (st && (st.state === "unsupported_cpu" || st.state === "exited")) {
        setProblem(st.message);
        setPhase("problem");
        return;
      }
      const h = await getJSON<{ ready: boolean }>("/llm/health");
      if (h?.ready) {
        setPhase("ready");
        return;
      }
      await sleep(1000);
    }
    setProblem("The AI model did not start within 3 minutes. See the app log for details.");
    setPhase("problem");
  }, []);

  const startLlm = useCallback(async () => {
    if (isTauri) {
      try {
        await invoke("start_llm");
      } catch (e) {
        setProblem(String(e));
        setPhase("problem");
        return;
      }
    }
    await waitForLlm();
  }, [waitForLlm]);

  // Startup: backend health, then decide between download / load / ready.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let ok = false;
      for (let i = 0; i < 60 && !cancelled; i++) {
        if (await getJSON("/health")) {
          ok = true;
          break;
        }
        await sleep(500);
      }
      if (cancelled) return;
      setBackendOk(ok);
      if (!ok) {
        setProblem("The local backend did not start. See the app log for details.");
        setPhase("problem");
        return;
      }
      const st = await llmStatus();
      if (st?.state === "unsupported_cpu" || st?.state === "exited") {
        setProblem(st.message);
        setPhase("problem");
        return;
      }
      const m = await getJSON<ModelStatus>("/model/status");
      setModel(m);
      if (m?.state === "downloading" || m?.state === "verifying") {
        setPhase("downloading");
      } else if (m?.state !== "ready") {
        setPhase("needs_model");
      } else if (st?.state === "needs_model") {
        await startLlm(); // downloaded earlier, not started yet
      } else {
        await waitForLlm();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [startLlm, waitForLlm]);

  // Poll download progress.
  useEffect(() => {
    if (phase !== "downloading") return;
    let stop = false;
    (async () => {
      while (!stop) {
        const m = await getJSON<ModelStatus>("/model/status");
        if (m) setModel(m);
        if (m?.state === "ready") {
          await startLlm();
          return;
        }
        if (m?.state === "error" || m?.state === "missing") {
          setPhase("needs_model");
          return;
        }
        await sleep(500);
      }
    })();
    return () => {
      stop = true;
    };
  }, [phase, startLlm]);

  const beginDownload = async () => {
    try {
      const res = await fetch(`${API}/model/download`, { method: "POST" });
      setModel(await res.json());
      setPhase("downloading");
    } catch (e) {
      setModel((m) => (m ? { ...m, state: "error", error: String(e) } : m));
    }
  };

  const uploadFile = async (file: File) => {
    if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
      setMessages([{ role: "ai", content: `"${file.name}" is not a PDF.`, error: true }]);
      return;
    }
    setUploading(file.name);
    setMessages([]);
    try {
      const res = await fetch(`${API}/upload?filename=${encodeURIComponent(file.name)}`, {
        method: "POST",
        headers: { "Content-Type": "application/pdf" },
        body: file,
      });
      const data = await res.json();
      if (res.ok && data.document_id) {
        setDoc({ id: data.document_id, name: data.filename ?? file.name, pages: data.page_count ?? 0 });
        setMessages([{ role: "ai", content: `${data.message} Ask me anything about it.` }]);
      } else {
        setMessages([{ role: "ai", content: `Could not load the document: ${data.detail || data.message || "unknown error"}`, error: true }]);
      }
    } catch (err) {
      setMessages([{ role: "ai", content: `Could not reach the local backend: ${err}`, error: true }]);
    } finally {
      setUploading(null);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) uploadFile(file);
  };

  const handleSend = async () => {
    const q = inputValue.trim();
    if (!q || !doc || isProcessing || phase !== "ready") return;
    setMessages((prev) => [...prev, { role: "user", content: q }]);
    setInputValue("");
    setIsProcessing(true);
    try {
      const res = await fetch(`${API}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_id: doc.id, question: q }),
      });
      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        res.ok
          ? { role: "ai", content: data.answer, citations: data.citations }
          : { role: "ai", content: data.detail || "Failed to get an answer.", error: true },
      ]);
    } catch (err) {
      setMessages((prev) => [...prev, { role: "ai", content: `Could not reach the local backend: ${err}`, error: true }]);
    } finally {
      setIsProcessing(false);
    }
  };

  const resetDoc = () => {
    setDoc(null);
    setMessages([]);
    setOpenCite(null);
  };

  const pct = model && model.total ? Math.min(100, (model.downloaded / model.total) * 100) : 0;
  const llmLabel: Record<Phase, string> = {
    checking: "Checking…",
    needs_model: "Model not downloaded",
    downloading: "Downloading model…",
    loading: "Loading model…",
    ready: "Ready",
    problem: "Unavailable",
  };
  const llmDot = phase === "ready" ? "ok" : phase === "problem" || phase === "needs_model" ? "error" : "loading";

  return (
    <main className="container">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">◆</span>
          <h1>AI Desktop</h1>
        </div>
        <p className="subtitle">Private document Q&amp;A. Your files never leave this computer.</p>
      </header>

      {problem && (
        <div className="glass-panel alert" role="alert">
          {problem}
        </div>
      )}

      {(phase === "needs_model" || phase === "downloading") && (
        <section className="glass-panel onboard" aria-labelledby="onboard-title">
          <h2 id="onboard-title">One-time setup: download the AI model</h2>
          <p>
            The app answers questions with <strong>{model?.model ?? "a local AI model"}</strong>
            {model?.license ? ` (${model.license})` : ""}. It runs entirely on this computer.
          </p>
          <ul className="onboard-points">
            <li>About {model ? mb(model.total) : "1,070"} MB, downloaded once from Hugging Face.</li>
            <li>This is the only time the app uses the internet. Documents and questions never leave this machine.</li>
            <li>The file is checked against a pinned SHA-256 fingerprint before it is ever loaded.</li>
          </ul>
          {phase === "needs_model" ? (
            <>
              {model?.state === "error" && <p className="onboard-error">Download failed: {model.error}</p>}
              <button id="download-model-btn" className="btn-primary" onClick={beginDownload} disabled={!backendOk}>
                {model?.state === "error" ? "Retry download" : "Download model"}
              </button>
            </>
          ) : (
            <div className="progress-wrap" aria-live="polite">
              <div className="progress" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
                <div className={`progress-fill ${model?.state === "verifying" ? "verifying" : ""}`} style={{ width: `${pct}%` }} />
              </div>
              <div className="progress-label">
                {model?.state === "verifying"
                  ? "Verifying SHA-256 fingerprint…"
                  : `${mb(model?.downloaded ?? 0)} / ${mb(model?.total ?? 0)} MB · ${pct.toFixed(0)}%`}
              </div>
            </div>
          )}
        </section>
      )}

      <section className="glass-panel workspace">
        {!doc ? (
          <div
            id="dropzone"
            className={`dropzone ${isDragging ? "active" : ""} ${uploading ? "busy" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setIsDragging(true);
            }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={handleDrop}
            onClick={() => !uploading && fileInputRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && fileInputRef.current?.click()}
          >
            {uploading ? (
              <>
                <div className="spinner" aria-hidden="true" />
                <p>Reading and encrypting “{uploading}”…</p>
              </>
            ) : (
              <>
                <svg className="icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 13h6m-3-3v6m5 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                <p>Drop a PDF here, or <span className="link">choose a file</span></p>
                <small>Text and search index are stored in an encrypted local database.</small>
              </>
            )}
            {messages[0]?.error && <p className="onboard-error">{messages[0].content}</p>}
            <input
              ref={fileInputRef}
              id="file-input"
              type="file"
              accept="application/pdf,.pdf"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) uploadFile(f);
                e.target.value = "";
              }}
            />
          </div>
        ) : (
          <div className="chat-container">
            <div className="doc-bar">
              <span className="doc-name" title={doc.name}>📄 {doc.name}</span>
              {doc.pages > 0 && <span className="doc-meta">{doc.pages} pages</span>}
              <button id="new-doc-btn" className="btn-ghost" onClick={resetDoc}>New document</button>
            </div>
            <div className="messages">
              {messages.map((msg, idx) => (
                <div key={idx} className={`message ${msg.role} ${msg.error ? "error" : ""}`}>
                  <div className="message-text">{msg.content}</div>
                  {msg.citations && msg.citations.length > 0 && (
                    <div className="citations">
                      <span className="citations-label">Sources</span>
                      {msg.citations.map((c, i) => {
                        const key = `${idx}-${i}`;
                        const best = i === 0;
                        return (
                          <button
                            key={key}
                            className={`citation-badge ${openCite === key ? "open" : ""} ${best ? "best" : ""}`}
                            onClick={() => setOpenCite(openCite === key ? null : key)}
                            aria-expanded={openCite === key}
                            title={best ? "Best match" : undefined}
                          >
                            Page {c.page}
                          </button>
                        );
                      })}
                      {msg.citations.map((c, i) =>
                        openCite === `${idx}-${i}` ? (
                          <blockquote key={`q-${i}`} className="citation-excerpt">
                            <span className="citation-excerpt-head">Page {c.page}</span>
                            {c.text}
                          </blockquote>
                        ) : null,
                      )}
                    </div>
                  )}
                </div>
              ))}
              {isProcessing && (
                <div className="message ai typing" aria-label="Thinking">
                  <span /><span /><span />
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            <div className="input-area">
              <input
                id="question-input"
                type="text"
                className="chat-input"
                placeholder={phase === "ready" ? "Ask a question about the document…" : "Waiting for the AI model…"}
                value={inputValue}
                maxLength={2000}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
                disabled={phase !== "ready"}
              />
              <button
                id="send-btn"
                className="send-button"
                onClick={handleSend}
                disabled={!inputValue.trim() || isProcessing || phase !== "ready"}
                aria-label="Send question"
              >
                <svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                </svg>
              </button>
            </div>
          </div>
        )}
      </section>

      <footer className="status-bar">
        <div className="status-indicator">
          <div className={`status-dot ${backendOk === null ? "loading" : backendOk ? "ok" : "error"}`} />
          <span>Encrypted library: {backendOk === null ? "Starting…" : backendOk ? "Ready" : "Unavailable"}</span>
        </div>
        <div className="status-indicator">
          <div className={`status-dot ${llmDot}`} />
          <span>AI model: {llmLabel[phase]}</span>
        </div>
        <div className="status-indicator offline">
          <span aria-hidden="true">🔒</span>
          <span>{phase === "downloading" ? "Downloading model" : "Offline"}</span>
        </div>
      </footer>
    </main>
  );
}

export default App;
