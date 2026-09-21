const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class FakeElement {
  attachShadow() {
    this.shadowRoot = {
      activeElement: null,
      appendChild() {},
      innerHTML: "",
      querySelector() { return null; },
      querySelectorAll() { return []; },
    };
  }
}

class FakeAudio {
  constructor() {
    this.currentTime = 0;
    this.duration = Number.NaN;
    this.ended = false;
    this.paused = true;
    this.playbackRate = 1;
    this.src = "";
  }

  addEventListener() {}
  removeEventListener() {}
}

global.HTMLElement = FakeElement;
global.Audio = FakeAudio;
global.CustomEvent = class {};
global.navigator = { userAgent: "" };
global.document = {
  visibilityState: "visible",
  addEventListener() {},
  removeEventListener() {},
  createElement() { return new FakeElement(); },
};
global.window = global;
global.window.localStorage = { getItem() { return null; }, setItem() {} };
global.window.addEventListener = () => {};
global.window.removeEventListener = () => {};
global.window.customCards = [];
global.customElements = { get() { return undefined; }, define() {} };

const cardPath = path.join(__dirname, "..", "www", "podcast-player-card", "podcast-player-card.js");
const source = `${fs.readFileSync(cardPath, "utf8")}\n;globalThis.__PodcastPlayerCard = PodcastPlayerCard;`;
vm.runInThisContext(source, { filename: cardPath });
const PodcastPlayerCard = global.__PodcastPlayerCard;

function progressCard() {
  const card = new PodcastPlayerCard();
  card._connected = true;
  card._currentEpisode = { episode_id: "ep_test", position: 60, duration_seconds: 300 };
  card._audio.currentTime = 124;
  card._audio.duration = 300;
  card._syncToShared = () => {};
  card._isSpeakerOutput = () => false;
  card._isLimitedSpeakerOutput = () => false;
  card._isActuallyPlaying = () => false;
  card._currentBrowserSpeed = () => 1.25;
  return card;
}

test("progress bookkeeping uses the silent websocket command", async () => {
  const card = progressCard();
  const messages = [];
  let serviceCalls = 0;
  card._hass = {
    connection: {
      connected: true,
      async sendMessagePromise(message) {
        messages.push(message);
        return { saved: true };
      },
    },
    callService() { serviceCalls += 1; },
  };

  assert.equal(await card._saveProgress(false), undefined);
  assert.equal(serviceCalls, 0);
  assert.deepEqual(messages, [{
    type: "podcast_player/save_progress",
    episode_id: "ep_test",
    position: 124,
    playing: false,
    speed: 1.25,
    duration: 300,
  }]);
});

test("hidden playback does not repeatedly send progress", async () => {
  const card = progressCard();
  const messages = [];
  card._hass = {
    connection: {
      connected: true,
      async sendMessagePromise(message) { messages.push(message); },
    },
  };

  document.visibilityState = "hidden";
  assert.equal(await card._saveProgressForEpisode(card._currentEpisode, true), false);
  assert.equal(messages.length, 0);
  assert.equal(await card._saveProgressForEpisode(card._currentEpisode, true, { allowHidden: true }), true);
  assert.equal(messages.length, 1);
  document.visibilityState = "visible";
});

test("wake abandons a stale lock request and reconnect flushes latest time", () => {
  const card = progressCard();
  let saves = 0;
  card._progressDirty = true;
  card._progressSaveInFlight = { id: 1 };
  card._saveProgress = () => { saves += 1; };

  card._onConnectionReady();

  assert.equal(card._progressSaveInFlight, null);
  assert.equal(saves, 1);
});
