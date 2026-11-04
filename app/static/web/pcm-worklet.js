/* Average each input interval into 16kHz bins, retaining fractional bins between calls. */
class PippaPCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.weight = 0;
    this.sum = 0;
    this.offset = 0;
    this.buffer = new ArrayBuffer(3200);
    this.view = new DataView(this.buffer);
    this.port.onmessage = event => {
      if (!event.data?.reset) return;
      this.weight = 0; this.sum = 0; this.offset = 0;
      this.buffer = new ArrayBuffer(3200); this.view = new DataView(this.buffer);
    };
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels?.length || !channels[0]?.length) return true;
    const duration = 16000 / sampleRate;
    for (let i = 0; i < channels[0].length; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] || 0;
      value = Math.max(-1, Math.min(1, value / channels.length));
      let remaining = duration;
      while (remaining > 1e-10) {
        const portion = Math.min(remaining, 1 - this.weight);
        this.sum += value * portion;
        this.weight += portion;
        remaining -= portion;
        if (this.weight >= 1 - 1e-10) {
          const sample = Math.max(-1, Math.min(1, this.sum / this.weight));
          this.view.setInt16(this.offset * 2, sample < 0 ? sample * 32768 : sample * 32767, true);
          this.offset += 1;
          this.weight = 0; this.sum = 0;
          if (this.offset === 1600) {
            this.port.postMessage(this.buffer, [this.buffer]);
            this.buffer = new ArrayBuffer(3200); this.view = new DataView(this.buffer); this.offset = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('pippa-pcm', PippaPCM);
