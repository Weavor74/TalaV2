# File: `index.html`

## Overview

The landing page for the public webserver served by VPS nodes.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tala Engine | The Cognitive Desktop Interface</title>
  <style>
    :root {
      --bg-color: #050505;
      --surface-color: rgba(20, 20, 25, 0.6);
      --border-color: rgba(0, 240, 255, 0.15);
      --text-main: #f0f0f0;
      --text-muted: #a0a0a0;
      --accent-1: #00f0ff;
      --accent-2: #7000ff;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg-color);
      color: var(--text-main);
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
      line-height: 1.6;
    }

    /* Ambient Background Effects */
    .bg-blobs {
      position: fixed;
      top: 0;
      left: 0;
      width: 100vw;
      height: 100vh;
      overflow: hidden;
      z-index: -1;
      pointer-events: none;
      background: radial-gradient(circle at 50% 50%, rgba(10,10,15,1) 0%, rgba(5,5,5,1) 100%);
    }

    .blob {
      position: absolute;
      filter: blur(150px);
      border-radius: 50%;
      opacity: 0.3;
      animation: float 25s infinite ease-in-out alternate;
    }

    .blob-1 { top: -10%; left: -10%; width: 50vw; height: 50vw; background: var(--accent-2); }
    .blob-2 { bottom: -20%; right: -10%; width: 60vw; height: 60vw; background: var(--accent-1); animation-delay: -5s; }

    @keyframes float {
      0% { transform: translate(0, 0) scale(1); }
      100% { transform: translate(5%, 10%) scale(1.1); }
    }

    /* Navbar */
    header {
      width: 100%;
      padding: 1.5rem 4rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: fixed;
      top: 0;
      z-index: 100;
      background: rgba(5, 5, 5, 0.5);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border-bottom: 1px solid var(--border-color);
    }

    .logo {
      font-size: 1.5rem;
      font-weight: 800;
      letter-spacing: 2px;
      text-transform: uppercase;
      background: linear-gradient(90deg, #fff, var(--accent-1));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      text-decoration: none;
    }

    .nav-links a {
      color: var(--text-muted);
      text-decoration: none;
      margin-left: 2rem;
      font-size: 0.95rem;
      font-weight: 500;
      transition: color 0.3s;
    }

    .nav-links a:hover {
      color: #fff;
    }

    .nav-links .btn-nav {
      background: rgba(0, 240, 255, 0.1);
      border: 1px solid rgba(0, 240, 255, 0.3);
      padding: 0.5rem 1rem;
      border-radius: 6px;
      color: var(--accent-1);
    }

    .nav-links .btn-nav:hover {
      background: rgba(0, 240, 255, 0.2);
    }

    /* Hero Section */
    .hero {
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      padding: 10rem 2rem 4rem;
    }

    h1 {
      font-size: clamp(2.5rem, 6vw, 4.5rem);
      font-weight: 800;
      line-height: 1.1;
      margin-bottom: 1.5rem;
      max-width: 900px;
    }

    .hero p {
      font-size: clamp(1.1rem, 2vw, 1.25rem);
      color: var(--text-muted);
      max-width: 700px;
      margin-bottom: 3rem;
    }

    /* Image Mockup */
    .mockup-container {
      width: 100%;
      max-width: 1100px;
      margin: 0 auto 6rem;
      border-radius: 12px;
      border: 1px solid var(--border-color);
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.8), 0 0 30px rgba(0, 240, 255, 0.1);
      overflow: hidden;
      position: relative;
    }

    .mockup-container::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0; height: 30px;
      background: rgba(20, 20, 25, 0.9);
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      padding-left: 1rem;
    }

    .mockup-container::after {
      content: '•••';
      position: absolute;
      top: -3px; left: 1rem;
      color: rgba(255,255,255,0.2);
      font-size: 24px;
      letter-spacing: 2px;
    }

    .mockup-container img {
      width: 100%;
      height: auto;
      display: block;
      padding-top: 30px; /* space for the fake titlebar */
      background: #000;
    }

    /* Features Section */
    .features {
      padding: 2rem 2rem 6rem;
      max-width: 1200px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 2rem;
    }

    .feature-card {
      background: var(--surface-color);
      border: 1px solid rgba(255, 255, 255, 0.05);
      padding: 2.5rem;
      border-radius: 16px;
      backdrop-filter: blur(10px);
    }

    .feature-icon {
      font-size: 2rem;
      margin-bottom: 1rem;
      color: var(--accent-1);
    }

    .feature-card h3 {
      font-size: 1.3rem;
      margin-bottom: 0.8rem;
    }

    .feature-card p {
      color: var(--text-muted);
      font-size: 0.95rem;
    }

    /* Footer */
    footer {
      padding: 3rem 2rem;
      text-align: center;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      margin-top: auto;
    }

    footer p { color: var(--text-muted); font-size: 0.9rem; }
  </style>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;800&display=swap" rel="stylesheet">
</head>
<body>

  <div class="bg-blobs">
    <div class="blob blob-1"></div>
    <div class="blob blob-2"></div>
  </div>

  <header>
    <a href="/" class="logo">Tala Engine</a>
    <nav class="nav-links">
      <a href="#features">Features</a>
      <a href="https://github.com" target="_blank">Documentation</a>
      <a href="/buy">Get Tokens</a>
      <a href="/download" class="btn-nav">Download Engine</a>
    </nav>
  </header>

  <main class="hero">
    <div style="font-size: 0.9rem; letter-spacing: 3px; color: #00f0ff; margin-bottom: 1.5rem; text-transform: uppercase; font-weight: 600;">
      Tactical • Autonomous • Learning • Architecture
    </div>
    <h1>The Desktop-Native<br>Cognitive Engine</h1>
    <p>Tala is an offline-first AI orchestration shell built around a revolutionary HTML Neural Node memory system. Run models locally, orchestrate workflows, and seamlessly connect to a P2P network of permanent agent peers.</p>
    <div style="margin-top: 2rem; display: flex; gap: 1rem; justify-content: center;">
      <a href="/download" style="display:inline-block; padding:0.8rem 2rem; background:rgba(0, 240, 255, 0.1); border:1px solid rgba(0,240,255,0.3); color:#00f0ff; border-radius:6px; text-decoration:none; font-weight:600;">Download Tala</a>
      <a href="/buy" style="display:inline-block; padding:0.8rem 2rem; background:var(--surface-color); border:1px solid rgba(255,255,255,0.1); color:#fff; border-radius:6px; text-decoration:none; font-weight:600;">Purchase Tokens</a>
    </div>
  </main>

  <div class="mockup-container">
    <img src="/mockup.png" alt="Tala Interface Mockup" />
  </div>

  <section class="features" id="features">
    <div class="feature-card">
      <div class="feature-icon">🧠</div>
      <h3>HTML Neural Nodes (HTML-NN)</h3>
      <p>Tala introduces a revolutionary memory system: HTML Neural Nodes. These are human-readable documents that function as first-class, active memory files. The engine can parse, retrieve, and activate these nodes as internal context, propagating relationships and auto-evaluating workflows.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">⚡</div>
      <h3>Offline-First Embedded Inference</h3>
      <p>Unlike cloud-dependent agents, Tala is a desktop-native orchestration shell. It features an embedded inference runtime that manages provider lifecycles, model caching, and local downloads, allowing your agents to operate securely and autonomously completely offline.</p>
    </div>
    <div class="feature-card">
      <div class="feature-icon">🌐</div>
      <h3>P2P Swarm & Peer Discovery</h3>
      <p>Connect your local desktop engine to a permanent VPS peer for seamless P2P handshakes, decentralized chat, and peer discovery. You can directly reference and retrieve HTML-NN memory files from trusted peers across the network using simple handles like @user323/financial.</p>
    </div>
  </section>

  <footer>
    <p>&copy; 2026 Tala Network. All rights reserved.</p>
  </footer>

</body>
</html>

```
