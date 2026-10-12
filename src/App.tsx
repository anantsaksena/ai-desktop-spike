import { useState, useEffect, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";

type LlmStatus =
  | { state: "starting" }
  | { state: "unsupported_cpu"; missing: string[]; message: string }
  | { state: "exited"; message: string };

type Message = {
  role: "user" | "ai";
  content: string;
  citations?: { page: number; text: string }[];
};

function App() {
  const [backendStatus, setBackendStatus] = useState("Checking...");
  const [modelStatus, setModelStatus] = useState("Checking...");
  const [llmProblem, setLlmProblem] = useState<string | null>(null);
  const [hasDocument, setHasDocument] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [isDragging, setIsDragging] = useState(false);
  
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    fetch("http://127.0.0.1:8000/health")
      .then((res) => res.json())
      .then((data) => setBackendStatus(data.status))
      .catch(() => setBackendStatus("FAILED"));

    const checkLlama = async (retries = 30) => {
      const status = await invoke<LlmStatus>("llm_status").catch(() => null);
      if (status && status.state !== "starting") {
        setLlmProblem(status.message);
        setModelStatus("unavailable");
        return;
      }
      try {
        const res = await fetch("http://127.0.0.1:8080/health");
        if (res.ok) {
          setModelStatus("OK");
          return;
        }
        setModelStatus("loading model...");
      } catch {
        // not listening yet
      }
      if (retries > 0) setTimeout(() => checkLlama(retries - 1), 1000);
      else setModelStatus("FAILED");
    };
    checkLlama();
  }, []);

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    
    // In a real implementation, we would send this to the backend /upload endpoint
    // and wait for ingestion. For now, simulate ingestion.
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const file = e.dataTransfer.files[0];
      setHasDocument(true);
      setMessages([
        { role: "ai", content: `Successfully loaded "${file.name}". How can I help you with this document?` }
      ]);
    }
  };

  const handleSend = () => {
    if (!inputValue.trim()) return;
    
    const userMsg = inputValue;
    setMessages(prev => [...prev, { role: "user", content: userMsg }]);
    setInputValue("");
    
    // Simulate AI response with citations for UI purposes
    setTimeout(() => {
      setMessages(prev => [...prev, {
        role: "ai", 
        content: "Based on the document, this is a placeholder response since the backend Q&A endpoint is not fully wired up yet. The document discusses local-only AI benefits.",
        citations: [{ page: 1, text: "The document discusses..." }, { page: 3, text: "Another relevant point..." }]
      }]);
    }, 1000);
  };

  const getStatusDot = (status: string) => {
    if (status.includes("OK")) return "ok";
    if (status.includes("FAILED") || status.includes("unavailable")) return "error";
    return "loading";
  };

  return (
    <main className="container">
      <h1>AI Desktop</h1>
      <p className="subtitle">Private document intelligence on your device</p>
      
      {llmProblem && (
        <div className="glass-panel" style={{ borderColor: '#ef4444', backgroundColor: 'rgba(239, 68, 68, 0.1)', marginBottom: '1rem' }}>
          {llmProblem}
        </div>
      )}

      <div className="glass-panel">
        {!hasDocument ? (
          <div 
            className={`dropzone ${isDragging ? 'active' : ''}`}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
          >
            <svg className="icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 13h6m-3-3v6m5 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
            <p>Drag and drop a PDF here to begin</p>
          </div>
        ) : (
          <div className="chat-container">
            <div className="messages">
              {messages.map((msg, idx) => (
                <div key={idx} className={`message ${msg.role}`}>
                  <div>{msg.content}</div>
                  {msg.citations && msg.citations.length > 0 && (
                    <div className="citations">
                      {msg.citations.map((c, i) => (
                        <a key={i} href={`#page-${c.page}`} className="citation-badge" title={c.text}>
                          Page {c.page}
                        </a>
                      ))}
                    </div>
                  )}
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>
            
            <div className="input-area">
              <input 
                type="text" 
                className="chat-input" 
                placeholder="Ask a question about the document..." 
                value={inputValue}
                onChange={e => setInputValue(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleSend()}
              />
              <button className="send-button" onClick={handleSend} disabled={!inputValue.trim()}>
                <svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                </svg>
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="status-bar">
        <div className="status-indicator">
          <div className={`status-dot ${getStatusDot(backendStatus)}`}></div>
          <span>Python Backend: {backendStatus}</span>
        </div>
        <div className="status-indicator">
          <div className={`status-dot ${getStatusDot(modelStatus)}`}></div>
          <span>Llama Server: {modelStatus}</span>
        </div>
      </div>
    </main>
  );
}

export default App;
