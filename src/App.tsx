import { useState, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";

type LlmStatus =
  | { state: "starting" }
  | { state: "unsupported_cpu"; missing: string[]; message: string }
  | { state: "exited"; message: string };

function App() {
  const [backendStatus, setBackendStatus] = useState("Checking...");
  const [modelStatus, setModelStatus] = useState("Checking...");
  const [llmProblem, setLlmProblem] = useState<string | null>(null);

  useEffect(() => {
    fetch("http://127.0.0.1:8000/health")
      .then((res) => res.json())
      .then((data) => setBackendStatus(data.status))
      .catch(() => setBackendStatus("backend FAILED"));

    // Ask the app shell first: if the CPU check failed, llama-server was never
    // started and polling it would only produce a misleading "FAILED".
    const checkLlama = async (retries = 30) => {
      const status = await invoke<LlmStatus>("llm_status").catch(() => null);
      if (status && status.state !== "starting") {
        setLlmProblem(status.message);
        setModelStatus("unavailable");
        return;
      }
      try {
        // llama-server answers 503 while the model is loading, 200 when ready.
        const res = await fetch("http://127.0.0.1:8080/health");
        if (res.ok) {
          setModelStatus("model OK");
          return;
        }
        setModelStatus("loading model...");
      } catch {
        // not listening yet
      }
      if (retries > 0) setTimeout(() => checkLlama(retries - 1), 1000);
      else setModelStatus("model FAILED");
    };
    checkLlama();
  }, []);

  return (
    <main className="container">
      <h1>AI Desktop Spike</h1>
      {llmProblem && (
        <div id="llm-unavailable" role="alert" className="llm-alert">
          {llmProblem}
        </div>
      )}
      <div style={{ marginTop: "2rem", fontSize: "1.5rem" }}>
        <p>Python Backend Status: <strong id="backend-status">{backendStatus}</strong></p>
        <p>Llama Server Status: <strong id="model-status">{modelStatus}</strong></p>
      </div>
    </main>
  );
}

export default App;
