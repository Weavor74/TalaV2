# File: `download.html`

## Overview

The static download page served by VPS nodes for new peers to download the Tala client.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tala Engine | Download & Install</title>
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
      opacity: 0.2;
      animation: float 25s infinite ease-in-out alternate;
    }

    .blob-1 { top: -10%; left: -10%; width: 50vw; height: 50vw; background: var(--accent-2); }
    .blob-2 { bottom: -20%; right: -10%; width: 60vw; height: 60vw; background: var(--accent-1); animation-delay: -5s; }

    @keyframes float {
      0% { transform: translate(0, 0) scale(1); }
      100% { transform: translate(5%, 10%) scale(1.1); }
    }

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

    .hero {
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      padding: 10rem 2rem 4rem;
    }

    h1 {
      font-size: clamp(2rem, 5vw, 3.5rem);
      font-weight: 800;
      line-height: 1.2;
      margin-bottom: 1rem;
    }

    .hero p {
      font-size: 1.1rem;
      color: var(--text-muted);
      max-width: 600px;
      margin-bottom: 3rem;
    }

    .download-container {
      width: 100%;
      max-width: 1000px;
      margin: 0 auto 6rem;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 2rem;
      padding: 0 2rem;
    }

    .os-card {
      background: var(--surface-color);
      border: 1px solid rgba(255, 255, 255, 0.05);
      padding: 3.5rem 2.5rem;
      border-radius: 16px;
      backdrop-filter: blur(10px);
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      transition: transform 0.3s, border-color 0.3s;
    }

    .os-card:hover {
      transform: translateY(-5px);
      border-color: rgba(0, 240, 255, 0.3);
    }

    .os-icon {
      font-size: 4rem;
      margin-bottom: 1.5rem;
    }

    .os-card h3 {
      font-size: 1.8rem;
      margin-bottom: 0.8rem;
    }

    .os-card p {
      color: var(--text-muted);
      font-size: 1rem;
      margin-bottom: 2.5rem;
    }

    .btn {
      display: inline-block;
      width: 100%;
      padding: 1rem 1.5rem;
      border-radius: 6px;
      text-decoration: none;
      font-size: 1.1rem;
      font-weight: 600;
      text-align: center;
      transition: all 0.3s;
    }

    .btn-primary {
      background: rgba(0, 240, 255, 0.1);
      border: 1px solid rgba(0, 240, 255, 0.3);
      color: var(--accent-1);
    }

    .btn-primary:hover {
      background: rgba(0, 240, 255, 0.2);
    }

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
      <a href="/">Home</a>
      <a href="/buy">Get Tokens</a>
    </nav>
  </header>

  <main class="hero">
    <h1>Install Tala Network</h1>
    <p>Deploy the cognitive engine locally. Since the repository is private, download the official standalone bundle below.</p>
    <p style="margin-top: 1rem; color: var(--accent-1); font-weight: 600;">Instructions: Extract the ZIP bundle on your machine, then run the installer script for your OS.</p>
  </main>

  <section class="download-container">
    
    <!-- Windows -->
    <div class="os-card">
      <div class="os-icon">⊞</div>
      <h3>Windows</h3>
      <p>Windows 10/11 (PowerShell)</p>
      <a href="/tala-windows.zip" class="btn btn-primary" download>Download Bundle</a>
    </div>

    <!-- Linux -->
    <div class="os-card">
      <div class="os-icon">🐧</div>
      <h3>Linux</h3>
      <p>Ubuntu, Debian, Fedora, Arch</p>
      <a href="/tala-linux.zip" class="btn btn-primary" download>Download Bundle</a>
    </div>

    <!-- macOS -->
    <div class="os-card">
      <div class="os-icon"></div>
      <h3>macOS</h3>
      <p>Intel & Apple Silicon (M1/M2/M3)</p>
      <a href="/tala-macos.zip" class="btn btn-primary" download>Download Bundle</a>
    </div>

  </section>

  <footer>
    <p>&copy; 2026 Tala Network. All rights reserved.</p>
  </footer>

</body>
</html>

```
