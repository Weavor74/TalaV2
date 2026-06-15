import sys

def patch_renderer():
    with open('renderer/renderer.js', 'r', encoding='utf-8') as f:
        content = f.read()

    # Add captureScreenContext and sendInferenceRequest
    
    target = '''  appendMessage(sender, name, content, isError = false) {'''
    
    replacement = '''  async captureScreenContext() {
    IPCLog.append('info', 'Capturing screen context for vision analysis...');
    this.appendMessage('user', 'User', '[Requesting screen capture...]');
    
    try {
      const result = await invoke('python:rpc', { method: 'multimodal.capture_screen', params: {} });
      if (result && result.image_base64) {
        const currentText = this._textarea.value.trim();
        const prompt = currentText ? currentText : "What do you see in this screenshot?";
        this._textarea.value = '';
        
        if (this._messages.lastElementChild && this._messages.lastElementChild.classList.contains('user')) {
          this._messages.removeChild(this._messages.lastElementChild);
        }

        const msgContent = `<img src="data:image/png;base64,${result.image_base64}" style="max-width: 100%; max-height: 300px; border-radius: 4px; margin-bottom: 8px; display: block;" />\\n${prompt}`;
        this.appendMessage('user', 'User', msgContent);
        
        this.sendInferenceRequest(prompt, [result.image_base64]);
      } else {
        this.appendMessage('agent', 'Tala', `Failed to capture screen: ${result ? result.error : 'Unknown error'}`);
      }
    } catch (err) {
      this.appendMessage('agent', 'Tala', `Screen capture error: ${err.message || err}`);
    }
  },

  async sendInferenceRequest(promptText, imagesBase64 = []) {
    const loadingId = 'chat-loading-' + Date.now();
    const loadingBubble = document.createElement('div');
    loadingBubble.className = 'chat-message agent';
    loadingBubble.id = loadingId;
    loadingBubble.innerHTML = `
      <div class="message-meta">Tala • ${fmtTime()}</div>
      <div class="message-content">Tala is thinking...</div>
    `;
    this._messages.appendChild(loadingBubble);
    this._messages.scrollTop = this._messages.scrollHeight;

    const historyLimit = 10;
    if (!this._history) {
      this._history = [];
    }
    const historyToSend = this._history.slice(-historyLimit);

    try {
      const isVision = imagesBase64 && imagesBase64.length > 0;
      const effectiveProviderId = isVision && this._visionProviderId ? this._visionProviderId : (this._providerId || undefined);

      const response = await invoke('python:rpc', {
        method: 'inference.generate',
        params: {
          prompt: promptText,
          history: historyToSend,
          session_id: this._sessionId,
          provider_id: effectiveProviderId,
          max_tokens: 1024,
          temperature: 0.7,
          images: imagesBase64
        }
      });

      const currentLoading = document.getElementById(loadingId);
      if (currentLoading) {
        this._messages.removeChild(currentLoading);
      }

      if (response && response.response) {
        this.appendMessage('agent', 'Tala', response.response);
        this._history.push({ role: 'user', content: promptText });
        this._history.push({ role: 'assistant', content: response.response });
      } else if (response && response.text) {
        this.appendMessage('agent', 'Tala', response.text);
        this._history.push({ role: 'user', content: promptText });
        this._history.push({ role: 'assistant', content: response.text });
      } else {
        const errMsg = response && response.error ? response.error : 'Empty response received';
        this.appendMessage('agent', 'Tala', `Error: ${errMsg}`, true);
      }
    } catch (err) {
      const currentLoading = document.getElementById(loadingId);
      if (currentLoading) {
        this._messages.removeChild(currentLoading);
      }
      this.appendMessage('agent', 'Tala', `Inference failed: ${err.message || err}`, true);
    }
  },

  appendMessage(sender, name, content, isError = false) {'''

    if 'captureScreenContext' not in content:
        content = content.replace(target, replacement)
        
    # we need to replace the old inference call inside sendMessage
    # find where sendMessage does inference.
    start_str = "    const loadingId = 'chat-loading-' + Date.now();"
    end_str = "      this.appendMessage('agent', 'Tala', `Error: ${err.message ?? err}`, true);\n    }\n  },"
    
    start_idx = content.find(start_str, content.find("if (isResearchRefinement) {"))
    if start_idx == -1:
        start_idx = content.find(start_str, content.find("AgentChat"))

    end_idx = content.find(end_str, start_idx)
    if start_idx != -1 and end_idx != -1:
        content = content[:start_idx] + "    this.sendInferenceRequest(text);\n  }," + content[end_idx + len(end_str) - 3:]
    
    with open('renderer/renderer.js', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    patch_renderer()
