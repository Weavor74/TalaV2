/**
 * AudioWorklet processor for the microphone: hands the main thread each 128-sample block of mono float audio.
 * The AudioContext is created at 16 kHz, so these samples are already at the rate the harness expects.
 * Nothing is recorded or stored here; the blocks are posted to voice_ui.js, which batches and sends them to the local harness.
 */
class TalaPcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      this.port.postMessage(new Float32Array(channel));
    }
    return true;
  }
}

registerProcessor('tala-pcm-capture', TalaPcmCapture);
