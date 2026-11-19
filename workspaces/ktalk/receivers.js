/* Each receiver feeds its own PCM buffer. The KTalk mixer is left out. */
(() => {
  const Native = window.RTCPeerConnection;
  if (!Native) return;
  const connections = new Set();
  window.RTCPeerConnection = new Proxy(Native, {
    construct(target, args, newTarget) {
      const connection = Reflect.construct(target, args, newTarget);
      connections.add(connection);
      connection.addEventListener("connectionstatechange", () => {
        if (["closed", "failed"].includes(connection.connectionState)) connections.delete(connection);
      });
      return connection;
    },
  });

  window.startGitsKTalkCapture = async (selfName = "") => {
    const audio = new AudioContext({ sampleRate: 16000 });
    if (audio.sampleRate !== 16000) throw new Error("Expected a 16 kHz audio context");
    await audio.resume();
    const active = new Map();
    let next = 0;
    const send = (id, message) => window.gitsKTalkAudio(id, message).catch(() => {});
    // Names: the tile of a speaking participant has the class `active-speaker`. A track gets the name of the only
    // speaking remote tile while it carries sound; with one remote tile and one track the match is direct.
    const scores = new Map();
    const named = new Map();
    const remoteTiles = () => [...document.querySelectorAll("conference-participant")]
      .map((tile) => ({ tile, name: (tile.querySelector(".wrapper")?.getAttribute("aria-label") || "").trim() }))
      .filter((item) => item.name && item.name !== selfName && item.name !== selfName + selfName);
    const level = (track) => {
      for (const pc of connections) {
        for (const receiver of pc.getReceivers()) {
          if (receiver.track === track) return receiver.getSynchronizationSources()[0]?.audioLevel || 0;
        }
      }
      return 0;
    };
    const report = (item, name) => {
      if (named.get(item.id) === name) return;
      named.set(item.id, name);
      window.gitsKTalkName(item.id, name).catch(() => {});
    };
    const resolveNames = () => {
      const tiles = remoteTiles();
      if (!tiles.length) return;
      if (tiles.length === 1 && active.size === 1) {
        for (const item of active.values()) report(item, tiles[0].name);
        return;
      }
      const speaking = tiles.filter((entry) => entry.tile.classList.contains("active-speaker"));
      if (speaking.length !== 1) return;
      const loud = [...active].filter(([track]) => level(track) > 0.05);
      if (loud.length !== 1) return;
      const [, item] = loud[0];
      const row = scores.get(item.id) || new Map();
      scores.set(item.id, row);
      row.set(speaking[0].name, (row.get(speaking[0].name) || 0) + 1);
      const ranked = [...row].sort((a, b) => b[1] - a[1]);
      if (ranked[0][1] >= 3 && ranked[0][1] >= 2 * (ranked[1]?.[1] || 0)) report(item, ranked[0][0]);
    };
    const refresh = () => {
      const tracks = new Set();
      for (const pc of connections) {
        for (const receiver of pc.getReceivers()) {
          const track = receiver.track;
          if (track?.kind === "audio" && track.readyState === "live" && /^remote-audio-\d+$/.test(track.id)) {
            tracks.add(track);
          }
        }
      }
      for (const [track, item] of active) {
        if (!tracks.has(track)) {
          item.node.onaudioprocess = null;
          item.source.disconnect();
          item.node.disconnect();
          item.silent.disconnect();
          active.delete(track);
          scores.delete(item.id);
          named.delete(item.id);
          send(item.id, null);
        }
      }
      for (const track of tracks) {
        if (active.has(track) || active.size >= 32) continue;
        const id = String(++next);
        const source = audio.createMediaStreamSource(new MediaStream([track]));
        const node = audio.createScriptProcessor(1024, 1, 1);
        const silent = audio.createGain();
        silent.gain.value = 0;
        let busy = false;
        node.onaudioprocess = (event) => {
          if (busy) return;
          const input = event.inputBuffer.getChannelData(0);
          const view = new DataView(new ArrayBuffer(input.length * 2));
          for (let i = 0; i < input.length; i++) {
            view.setInt16(i * 2, Math.max(-32768, Math.min(32767, input[i] * 32768)), true);
          }
          const bytes = new Uint8Array(view.buffer);
          busy = true;
          send(id, btoa(String.fromCharCode(...bytes))).finally(() => { busy = false; });
        };
        source.connect(node);
        node.connect(silent);
        silent.connect(audio.destination);
        active.set(track, { id, source, node, silent });
      }
    };
    refresh();
    setInterval(refresh, 400);
    setInterval(resolveNames, 200);
  };
})();
