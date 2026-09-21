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

  addEventListener() {}
  removeEventListener() {}
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
  card._shared.currentEpisodeId = "ep_test";
  card._shared.currentEpisode = card._currentEpisode;
  card._shared.ownerId = card._instanceId;
  card._audio.src = "https://example.test/episode.mp3";
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

  card._pageHidden = true;
  assert.equal(await card._saveProgressForEpisode(card._currentEpisode, true), false);
  assert.equal(messages.length, 0);
  assert.equal(await card._saveProgressForEpisode(card._currentEpisode, true, { allowHidden: true }), true);
  assert.equal(messages.length, 1);
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

test("a real card touch restores foreground sync despite stale WebView visibility", async () => {
  const card = progressCard();
  const messages = [];
  card._pageHidden = true;
  document.visibilityState = "hidden";
  card._hass = {
    connection: {
      connected: true,
      async sendMessagePromise(message) { messages.push(message); },
    },
  };

  card._onForegroundInteraction();
  assert.equal(card._pageHidden, false);
  assert.equal(await card._saveProgressForEpisode(card._currentEpisode, false), true);
  assert.equal(messages[0].position, 124);
  document.visibilityState = "visible";
});

test("a passive card cannot overwrite progress owned by another card", async () => {
  const card = progressCard();
  const messages = [];
  card._shared.ownerId = "another-card";
  card._progressDirty = true;
  card._hass = {
    connection: {
      connected: true,
      async sendMessagePromise(message) { messages.push(message); },
    },
  };

  assert.equal(await card._saveProgress(false), false);
  card._onPageHidden();
  card.disconnectedCallback();
  assert.equal(messages.length, 0);
  assert.equal(card._shared.ownerId, "another-card");
});

test("syncing display state does not steal active playback ownership", () => {
  const card = progressCard();
  card._shared.ownerId = "actual-player-card";
  card._playerState = () => ({ output_mode: "browser" });

  card._syncToShared();

  assert.equal(card._shared.ownerId, "actual-player-card");
});

test("a passive card displays backend progress instead of stale local audio", () => {
  const card = progressCard();
  card._shared.ownerId = null;
  card._audio.currentTime = 181;
  card._currentEpisode.position = 181;
  card._playerState = () => ({
    current_episode_id: "ep_test",
    position: 220,
    duration: 300,
  });

  assert.deepEqual(card._displayPositionDuration(), {
    position: 220,
    duration: 300,
  });
  assert.equal(card._resumePositionForEpisode(card._currentEpisode), 220);
});

test("a passive card advances from the latest backend checkpoint while playing", () => {
  const card = progressCard();
  const realDateNow = Date.now;
  const now = new Date("2026-09-21T12:00:10Z").getTime();
  card._shared.ownerId = null;
  card._playerState = () => ({
    current_episode_id: "ep_test",
    position: 220,
    duration: 300,
    speed: 1.25,
    state: "playing",
    position_updated_at: "2026-09-21T12:00:00Z",
  });

  try {
    Date.now = () => now;
    assert.deepEqual(card._displayPositionDuration(), {
      position: 232.5,
      duration: 300,
    });
    assert.equal(card._shouldRunDisplayClock(), true);
    card._pageHidden = true;
    assert.equal(card._shouldRunDisplayClock(), false);
  } finally {
    Date.now = realDateNow;
  }
});
