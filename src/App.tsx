import { useState, useEffect } from "react";
import "./App.css";

function App() {
  const [backendStatus, setBackendStatus] = useState("Checking...");
  const [modelStatus, setModelStatus] = useState("Checking...");

  useEffect(() => {
    // Check python backend
    fetch("http://127.0.0.1:8000/health")
      .then((res) => res.json())
      .then((data) => setBackendStatus(data.status))
      .catch(() => setBackendStatus("backend FAILED"));

    // Check llama server (defaults to 8080)
    // Because it might be starting up, we retry a few times
    const checkLlama = async (retries = 5) => {
      try {
        const res = await fetch("http://127.0.0.1:8080/health");
        if (res.ok) {
          setModelStatus("model OK");
        } else {
          setModelStatus("model OK (No model loaded, but server running)");
        }
      } catch (err) {
        if (retries > 0) {
          setTimeout(() => checkLlama(retries - 1), 1000);
        } else {
          setModelStatus("model FAILED");
        }
      }
    };
    
    checkLlama();
  }, []);

  return (
    <main className="container">
      <h1>AI Desktop Spike</h1>
      <div style={{ marginTop: "2rem", fontSize: "1.5rem" }}>
        <p>Python Backend Status: <strong>{backendStatus}</strong></p>
        <p>Llama Server Status: <strong>{modelStatus}</strong></p>
      </div>
    </main>
  );
}

export default App;
