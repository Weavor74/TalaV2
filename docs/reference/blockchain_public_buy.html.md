# File: `buy.html`

## Overview

A web interface for purchasing Tala credits or subscriptions.

## File Contents

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tala Network - Fund Your Node</title>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0a0a0c;
      --surface: rgba(255, 255, 255, 0.05);
      --surface-hover: rgba(255, 255, 255, 0.08);
      --border: rgba(255, 255, 255, 0.1);
      --primary: #6366f1;
      --primary-hover: #4f46e5;
      --text: #ffffff;
      --text-muted: #9ca3af;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: 'Outfit', sans-serif;
      background-color: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
      overflow: hidden;
    }

    .back-link {
      position: absolute;
      top: 32px;
      left: 48px;
      color: var(--text-muted);
      text-decoration: none;
      font-weight: 600;
      font-size: 1rem;
      z-index: 10;
      transition: color 0.2s;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .back-link:hover {
      color: #fff;
    }

    /* Animated background glow */
    .glow {
      position: absolute;
      width: 600px;
      height: 600px;
      background: radial-gradient(circle, rgba(99, 102, 241, 0.15) 0%, rgba(0, 0, 0, 0) 70%);
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      z-index: 0;
      animation: pulse 10s infinite alternate;
    }

    @keyframes pulse {
      0% { transform: translate(-50%, -50%) scale(1); opacity: 0.8; }
      100% { transform: translate(-50%, -50%) scale(1.2); opacity: 1; }
    }

    .container {
      position: relative;
      z-index: 1;
      background: var(--surface);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 48px;
      width: 100%;
      max-width: 480px;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
      animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1);
    }

    @keyframes slideUp {
      from { opacity: 0; transform: translateY(40px); }
      to { opacity: 1; transform: translateY(0); }
    }

    h1 {
      font-weight: 800;
      font-size: 2rem;
      margin-bottom: 8px;
      background: linear-gradient(to right, #fff, #a5b4fc);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    p.subtitle {
      color: var(--text-muted);
      margin-bottom: 32px;
      font-weight: 300;
      line-height: 1.5;
    }

    .form-group {
      margin-bottom: 24px;
    }

    label {
      display: block;
      font-size: 0.875rem;
      font-weight: 600;
      margin-bottom: 8px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    input[type="text"] {
      width: 100%;
      background: rgba(0, 0, 0, 0.2);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      color: var(--text);
      font-size: 1rem;
      font-family: 'Outfit', sans-serif;
      transition: all 0.2s ease;
    }

    input[type="text"]::placeholder {
      color: rgba(255, 255, 255, 0.2);
    }

    input[type="text"]:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.2);
    }

    .amount-selector {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
    }

    .amount-btn {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px 0;
      color: var(--text);
      font-size: 1.125rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      font-family: 'Outfit', sans-serif;
    }

    .amount-btn:hover {
      background: var(--surface-hover);
    }

    .amount-btn.active {
      background: var(--primary);
      border-color: var(--primary);
      box-shadow: 0 4px 12px rgba(99, 102, 241, 0.4);
    }

    .checkout-btn {
      width: 100%;
      background: var(--text);
      color: var(--bg);
      border: none;
      border-radius: 12px;
      padding: 18px;
      font-size: 1rem;
      font-weight: 800;
      cursor: pointer;
      transition: all 0.2s ease;
      margin-top: 16px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      font-family: 'Outfit', sans-serif;
    }

    .checkout-btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 20px rgba(255, 255, 255, 0.2);
    }

    .checkout-btn:active {
      transform: translateY(0);
    }

    .checkout-btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
      transform: none;
    }

    .stripe-icon {
      width: 44px;
      height: 18px;
    }

    .success-overlay {
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      background: var(--bg);
      border-radius: 24px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.4s ease;
      z-index: 10;
    }

    .success-overlay.show {
      opacity: 1;
      pointer-events: all;
    }

    .success-icon {
      width: 64px;
      height: 64px;
      border-radius: 50%;
      background: rgba(34, 197, 94, 0.1);
      color: #22c55e;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 32px;
      margin-bottom: 24px;
      border: 1px solid rgba(34, 197, 94, 0.2);
    }

    .error-text {
      color: #ef4444;
      font-size: 0.875rem;
      margin-top: 12px;
      text-align: center;
      display: none;
    }
  </style>
</head>
<body>
  <a href="/" class="back-link">← Return to Tala Engine</a>
  <div class="glow"></div>
  
  <div class="container">
    <h1>Top Up Wallet</h1>
    <p class="subtitle">Purchase TalaCoins to query the decentralized neural network.</p>

    <div class="form-group">
      <label>Tala Username</label>
      <input type="text" id="username" placeholder="@steve" autocomplete="off">
    </div>

    <div class="form-group">
      <label>Amount (USD)</label>
      <div class="amount-selector" id="amount-selector">
        <button class="amount-btn active" data-amount="1">$1</button>
        <button class="amount-btn" data-amount="5">$5</button>
        <button class="amount-btn" data-amount="10">$10</button>
      </div>
      <input type="number" id="custom-amount" placeholder="Or enter custom amount..." min="1" step="any" style="margin-top: 12px; width: 100%; background: rgba(0, 0, 0, 0.2); border: 1px solid var(--border); border-radius: 12px; padding: 16px; color: var(--text); font-size: 1rem; font-family: 'Outfit', sans-serif;">
    </div>

    <button class="checkout-btn" id="checkout-btn">
      Checkout with
      <svg class="stripe-icon" viewBox="0 0 60 25" xmlns="http://www.w3.org/2000/svg">
        <path fill="currentColor" d="M59.64 14.28h-8.06c.19 1.93 1.6 2.55 3.2 2.55 1.64 0 2.96-.37 4.05-.95v3.32a8.33 8.33 0 0 1-4.56 1.1c-4.01 0-6.83-2.5-6.83-7.48 0-4.19 2.39-7.52 6.3-7.52 3.92 0 5.96 3.28 5.96 7.5v1.48zm-5.92-5.62c-1.03 0-2.17.73-2.17 2.58h4.25c0-1.62-.64-2.58-2.08-2.58zM43.4 4.04v11.83c0 3.32 1.6 4.41 4.5 4.41 1.25 0 2.22-.16 2.87-.41v-3.23a4.7 4.7 0 0 1-1.66.27c-1.12 0-1.58-.55-1.58-1.92V9.3h3.2v-3.8h-3.2V1.65l-4.13 2.39zm-6.24 6.74c-.06-1.78-1.28-2.27-2.6-2.27-1.4 0-2.73.57-2.73 2.53 0 1.67 1.18 2.08 2.66 2.27l1.32.17c1.7.21 2.45.68 2.45 1.8 0 1.61-1.46 2.67-3.41 2.67-1.58 0-3.32-.51-4.52-1.35v3.66a8.04 8.04 0 0 0 4.76 1.35c2.31 0 4.48-1.14 4.48-4.08 0-2.45-1.85-2.9-3.28-3.11l-1.32-.2c-1.38-.2-2.04-.54-2.04-1.51 0-1.22 1.23-1.63 2.33-1.63 1.14 0 2.55.37 3.51.98V8.16a7.6 7.6 0 0 0-4.03-1.1c-2.31 0-4.22 1.17-4.22 3.96 0 2.32 1.5 2.8 3.1 3.02l1.32.2c1.38.2 2.15.65 2.15 1.67 0 1.15-1.18 1.65-2.4 1.65-1.36 0-3.11-.53-4.26-1.26v3.7c1.23.63 3 1.1 4.8 1.1 2.5 0 4.67-1.15 4.67-4.15 0-2.78-1.91-3.22-3.3-3.43l-1.3-.22c-.67-.11-2.05-.33-2.05-1.52 0-1.07.98-1.52 2.05-1.52 1.34 0 2.8.44 3.79 1.05v-3.66a7.7 7.7 0 0 0-3.87-.97zM20.25 5.5h-4.21v14.54h4.21V5.5zm-2.1-4.9a2.38 2.38 0 1 0 0 4.76 2.38 2.38 0 0 0 0-4.75zM12.44 9.16V5.5H8.38v3.66H5.43v3.83h2.95v7.05h4.06v-7.05h3.69V9.16h-3.69zM31.25 5.5h-4.06v14.54h4.06v-1.19a5.1 5.1 0 0 0 3.73 1.43c2.72 0 5.05-2.34 5.05-7.41 0-4.95-2.26-7.39-4.98-7.39a5.05 5.05 0 0 0-3.8 1.51V5.5zm.06 7.42c0-2.61.9-4.2 2.35-4.2 1.54 0 2.45 1.53 2.45 4.2 0 2.76-.89 4.34-2.45 4.34-1.48 0-2.35-1.63-2.35-4.34z"/>
      </svg>
    </button>
    
    <div class="error-text" id="error-message"></div>

    <div class="success-overlay" id="success-overlay">
      <div class="success-icon">✓</div>
      <h2>Payment Successful</h2>
      <p style="color: var(--text-muted); margin-top: 8px;">Tokens added to your wallet</p>
    </div>
  </div>

  <script>
    const amountBtns = document.querySelectorAll('.amount-btn');
    const customAmountInput = document.getElementById('custom-amount');
    const checkoutBtn = document.getElementById('checkout-btn');
    const usernameInput = document.getElementById('username');
    const errorMsg = document.getElementById('error-message');
    const successOverlay = document.getElementById('success-overlay');
    let selectedAmount = 1;

    amountBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        amountBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        selectedAmount = parseInt(btn.dataset.amount);
        customAmountInput.value = ''; // clear custom amount
      });
    });

    customAmountInput.addEventListener('input', (e) => {
      if (e.target.value) {
        amountBtns.forEach(b => b.classList.remove('active'));
        selectedAmount = parseFloat(e.target.value);
      } else {
        // default back to the first button if cleared
        amountBtns[0].classList.add('active');
        selectedAmount = parseInt(amountBtns[0].dataset.amount);
      }
    });

    checkoutBtn.addEventListener('click', async () => {
      const username = usernameInput.value.trim();
      
      if (!username) {
        showError("Please enter your Tala username");
        return;
      }
      
      if (!username.startsWith('@')) {
        showError("Username must start with @");
        return;
      }

      // Hide error, show loading
      errorMsg.style.display = 'none';
      checkoutBtn.disabled = true;
      checkoutBtn.innerHTML = 'Processing...';

      try {
        const response = await fetch('/checkout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, amount: selectedAmount })
        });

        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.error || 'Payment failed');
        }

        // Show success overlay
        successOverlay.classList.add('show');
        
        // Reset form after 3 seconds
        setTimeout(() => {
          successOverlay.classList.remove('show');
          checkoutBtn.disabled = false;
          checkoutBtn.innerHTML = `
            Checkout with
            <svg class="stripe-icon" viewBox="0 0 60 25" xmlns="http://www.w3.org/2000/svg">
              <path fill="currentColor" d="M59.64 14.28h-8.06c.19 1.93 1.6 2.55 3.2 2.55 1.64 0 2.96-.37 4.05-.95v3.32a8.33 8.33 0 0 1-4.56 1.1c-4.01 0-6.83-2.5-6.83-7.48 0-4.19 2.39-7.52 6.3-7.52 3.92 0 5.96 3.28 5.96 7.5v1.48zm-5.92-5.62c-1.03 0-2.17.73-2.17 2.58h4.25c0-1.62-.64-2.58-2.08-2.58zM43.4 4.04v11.83c0 3.32 1.6 4.41 4.5 4.41 1.25 0 2.22-.16 2.87-.41v-3.23a4.7 4.7 0 0 1-1.66.27c-1.12 0-1.58-.55-1.58-1.92V9.3h3.2v-3.8h-3.2V1.65l-4.13 2.39zm-6.24 6.74c-.06-1.78-1.28-2.27-2.6-2.27-1.4 0-2.73.57-2.73 2.53 0 1.67 1.18 2.08 2.66 2.27l1.32.17c1.7.21 2.45.68 2.45 1.8 0 1.61-1.46 2.67-3.41 2.67-1.58 0-3.32-.51-4.52-1.35v3.66a8.04 8.04 0 0 0 4.76 1.35c2.31 0 4.48-1.14 4.48-4.08 0-2.45-1.85-2.9-3.28-3.11l-1.32-.2c-1.38-.2-2.04-.54-2.04-1.51 0-1.22 1.23-1.63 2.33-1.63 1.14 0 2.55.37 3.51.98V8.16a7.6 7.6 0 0 0-4.03-1.1c-2.31 0-4.22 1.17-4.22 3.96 0 2.32 1.5 2.8 3.1 3.02l1.32.2c1.38.2 2.15.65 2.15 1.67 0 1.15-1.18 1.65-2.4 1.65-1.36 0-3.11-.53-4.26-1.26v3.7c1.23.63 3 1.1 4.8 1.1 2.5 0 4.67-1.15 4.67-4.15 0-2.78-1.91-3.22-3.3-3.43l-1.3-.22c-.67-.11-2.05-.33-2.05-1.52 0-1.07.98-1.52 2.05-1.52 1.34 0 2.8.44 3.79 1.05v-3.66a7.7 7.7 0 0 0-3.87-.97zM20.25 5.5h-4.21v14.54h4.21V5.5zm-2.1-4.9a2.38 2.38 0 1 0 0 4.76 2.38 2.38 0 0 0 0-4.75zM12.44 9.16V5.5H8.38v3.66H5.43v3.83h2.95v7.05h4.06v-7.05h3.69V9.16h-3.69zM31.25 5.5h-4.06v14.54h4.06v-1.19a5.1 5.1 0 0 0 3.73 1.43c2.72 0 5.05-2.34 5.05-7.41 0-4.95-2.26-7.39-4.98-7.39a5.05 5.05 0 0 0-3.8 1.51V5.5zm.06 7.42c0-2.61.9-4.2 2.35-4.2 1.54 0 2.45 1.53 2.45 4.2 0 2.76-.89 4.34-2.45 4.34-1.48 0-2.35-1.63-2.35-4.34z"/>
            </svg>
          `;
        }
      }
    });

    function showError(msg) {
      errorMsg.textContent = msg;
      errorMsg.style.display = 'block';
    }
  </script>
</body>
</html>

```
