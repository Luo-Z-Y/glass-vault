// Push-to-talk recorder (an AudioWorklet): hands each block of microphone samples, mono
// Float32 at the page's sample rate, to the page, which resamples them for TRTC speech
// recognition. It outputs silence.
class PcmRecorder extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}

registerProcessor("pcm-recorder", PcmRecorder);
