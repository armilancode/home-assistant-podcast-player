class PodcastPlayerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._rememberState = true;
    this._hass = null;
    this._library = null;
    this._selectedFeed = "all";
    this._filter = "all";
    this._preferredOutputTarget = "browser";
    this._fixedOutputTarget = false;
    this._lastSpeakerTarget = null;
    this._selectedEpisodeId = null;
    this._currentEpisode = null;
    this._loading = false;
    this._error = null;
    this._info = null;
    this._shared = PodcastPlayerCard._sharedPlayer();
    this._audio = this._shared.audio;
    this._useProxyForCurrent = Boolean(this._shared.useProxy);
    this._progressTimer = null;
    this._renderTimer = null;
    this._lastSave = 0;
    this._lastDynamicUpdate = 0;
    this._deferRenderUntil = 0;
    this._lastRenderKey = "";
    this._episodeScrollTop = 0;
    this._feedStripScrollLeft = 0;
    this._pendingFeedScrollIntoView = false;
    this._resetEpisodeScrollOnNextRender = false;
    this._currentSpeed = this._currentBrowserSpeed();
    this._audioListenersAttached = false;
    this._boundSharedSpeedHandler = (ev) => this._onSharedSpeedChanged(ev);
    this._boundSharedOutputHandler = (ev) => this._onSharedOutputChanged(ev);
    this._boundStorageHandler = (ev) => this._onStorageChanged(ev);
    this._boundAudioHandlers = {
      timeupdate: () => this._onTimeUpdate(),
      loadedmetadata: () => this._onLoadedMetadata(),
      ended: () => this._onEnded(),
      error: () => this._onAudioError(),
      play: () => this._saveProgress(true),
      pause: () => this._saveProgress(false),
    };
  }

  static _sharedPlayer() {
    const key = "__haPodcastPlayerSharedAudio";
    if (!window[key]) {
      const audio = new Audio();
      audio.preload = "metadata";
      const storedSpeed = PodcastPlayerCard._readBrowserSpeedPreference();
      if (storedSpeed) audio.playbackRate = storedSpeed;
      window[key] = {
        audio,
        currentEpisodeId: null,
        currentEpisode: null,
        speed: storedSpeed,
        useProxy: false,
        outputMode: "browser",
        preferredOutputTarget: PodcastPlayerCard._readOutputTargetPreference("browser"),
        targetMediaPlayer: null,
        targetMediaPlayerName: null,
      };
    }
    return window[key];
  }

  static _speedOptions() {
    return [0.75, 1, 1.25, 1.5, 1.75, 2];
  }

  static _normalizeSpeed(value) {
    const speed = Number(value);
    if (!Number.isFinite(speed)) return null;
    return PodcastPlayerCard._speedOptions().includes(speed) ? speed : null;
  }

  static _browserSpeedStorageKey() {
    return "haPodcastPlayer:browserPlaybackSpeed";
  }

  static _readBrowserSpeedPreference() {
    try {
      return PodcastPlayerCard._normalizeSpeed(window.localStorage.getItem(PodcastPlayerCard._browserSpeedStorageKey()));
    } catch (_) {
      return null;
    }
  }

  static _writeBrowserSpeedPreference(speed) {
    const normalized = PodcastPlayerCard._normalizeSpeed(speed);
    if (!normalized) return null;
    try {
      window.localStorage.setItem(PodcastPlayerCard._browserSpeedStorageKey(), String(normalized));
    } catch (_) {}
    return normalized;
  }

  static _outputTargetStorageKey() {
    return "haPodcastPlayer:preferredOutputTarget";
  }

  static _normalizeOutputTarget(value) {
    const target = String(value || "browser").trim();
    return target || "browser";
  }

  static _readOutputTargetPreference(fallback = "browser") {
    try {
      const value = window.localStorage.getItem(PodcastPlayerCard._outputTargetStorageKey());
      return value === null || value === undefined || value === "" ? PodcastPlayerCard._normalizeOutputTarget(fallback) : PodcastPlayerCard._normalizeOutputTarget(value);
    } catch (_) {
      return PodcastPlayerCard._normalizeOutputTarget(fallback);
    }
  }

  static _writeOutputTargetPreference(target) {
    const normalized = PodcastPlayerCard._normalizeOutputTarget(target);
    try {
      window.localStorage.setItem(PodcastPlayerCard._outputTargetStorageKey(), normalized);
    } catch (_) {}
    return normalized;
  }

  static _stubConfig(mode = "full") {
    return {
      type: "custom:podcast-player-card",
      entity: "media_player.podcast_player",
      mode,
      limit: mode === "latest" ? 12 : 200,
    };
  }

  static getStubConfig() {
    return PodcastPlayerCard._stubConfig("full");
  }

  static getConfigElement() {
    return document.createElement("podcast-player-card-editor");
  }

  _backendSpeedFallback(episode = null) {
    return PodcastPlayerCard._normalizeSpeed(
      (this._library && this._library.player && this._library.player.speed) ||
      (this._library && this._library.settings && this._library.settings.default_playback_speed) ||
      (episode && episode.playback_speed)
    );
  }

  _currentBrowserSpeed(episode = null) {
    const shared = PodcastPlayerCard._normalizeSpeed(this._shared && this._shared.speed);
    if (shared) return shared;
    const stored = PodcastPlayerCard._readBrowserSpeedPreference();
    if (stored) return stored;

    const audio = PodcastPlayerCard._normalizeSpeed(this._audio && this._audio.playbackRate);
    const backend = this._backendSpeedFallback(episode);
    const hasLiveBrowserSession = Boolean((this._audio && this._audio.src) || (this._shared && this._shared.currentEpisodeId));

    if (hasLiveBrowserSession && audio) return audio;
    return backend || audio || 1;
  }

  _setSharedBrowserSpeed(speed, persist = true, notify = true) {
    const normalized = PodcastPlayerCard._normalizeSpeed(speed) || 1;
    this._currentSpeed = normalized;
    if (this._shared) this._shared.speed = normalized;
    if (this._audio) this._audio.playbackRate = normalized;
    if (persist) PodcastPlayerCard._writeBrowserSpeedPreference(normalized);
    if (this._library && this._library.settings) this._library.settings.default_playback_speed = normalized;
    if (this._library && this._library.player) this._library.player.speed = normalized;
    if (notify) {
      window.dispatchEvent(new CustomEvent("podcast-player-speed-changed", { detail: { speed: normalized } }));
    }
    return normalized;
  }

  _onSharedSpeedChanged(ev) {
    const speed = PodcastPlayerCard._normalizeSpeed(ev && ev.detail && ev.detail.speed);
    if (!speed) return;
    this._setSharedBrowserSpeed(speed, false, false);
    if (this._currentEpisode) this._currentEpisode.playback_speed = speed;
    this._updateDynamicUi(true);
    this._scheduleRender();
  }

  _setSharedOutputTarget(target, persist = true, notify = true) {
    const normalized = PodcastPlayerCard._normalizeOutputTarget(target);
    this._preferredOutputTarget = normalized;
    if (this._shared) this._shared.preferredOutputTarget = normalized;
    if (persist) {
      PodcastPlayerCard._writeOutputTargetPreference(normalized);
      this._writePreference("output_target", normalized);
    }
    if (notify) {
      window.dispatchEvent(new CustomEvent("podcast-player-output-target-changed", { detail: { target: normalized } }));
    }
    return normalized;
  }

  _onSharedOutputChanged(ev) {
    if (this._fixedOutputTarget) return;
    const target = PodcastPlayerCard._normalizeOutputTarget(ev && ev.detail && ev.detail.target);
    if (!target || target === this._preferredOutputTarget) return;
    this._setSharedOutputTarget(target, false, false);
    this._updateDynamicUi(true);
    this._scheduleRender();
  }

  _onStorageChanged(ev) {
    if (!ev) return;
    if (ev.key === PodcastPlayerCard._browserSpeedStorageKey()) {
      const speed = PodcastPlayerCard._normalizeSpeed(ev.newValue);
      if (speed) this._onSharedSpeedChanged({ detail: { speed } });
      return;
    }
    if (ev.key === PodcastPlayerCard._outputTargetStorageKey()) {
      this._onSharedOutputChanged({ detail: { target: ev.newValue || "browser" } });
    }
  }

  _attachAudioListeners() {
    if (this._audioListenersAttached) return;
    Object.entries(this._boundAudioHandlers).forEach(([event, handler]) => {
      this._audio.addEventListener(event, handler);
    });
    this._audioListenersAttached = true;
  }

  _detachAudioListeners() {
    if (!this._audioListenersAttached) return;
    Object.entries(this._boundAudioHandlers).forEach(([event, handler]) => {
      this._audio.removeEventListener(event, handler);
    });
    this._audioListenersAttached = false;
  }

  _syncFromShared() {
    // In speaker mode the backend is authoritative. Do not let an old
    // browser tab's shared audio session override the TV/speaker episode.
    if (!this._isSpeakerOutput() && this._shared.currentEpisodeId) this._selectedEpisodeId = this._shared.currentEpisodeId;
    this._setSharedBrowserSpeed(this._currentBrowserSpeed(this._currentEpisode), false, false);
    if (!this._fixedOutputTarget) {
      const sharedOutput = (this._shared && this._shared.preferredOutputTarget) || PodcastPlayerCard._readOutputTargetPreference(this._preferredOutputTarget || "browser");
      this._setSharedOutputTarget(sharedOutput, false, false);
    }
    this._useProxyForCurrent = Boolean(this._shared.useProxy);
  }

  _syncToShared() {
    if (this._currentEpisode) {
      this._shared.currentEpisodeId = this._currentEpisode.episode_id;
      this._shared.currentEpisode = this._currentEpisode;
    }
    // Browser playback speed is global for all podcast cards/pages. Never let
    // a stale card-local value overwrite the active shared audio speed.
    this._setSharedBrowserSpeed(this._currentBrowserSpeed(this._currentEpisode), false, false);
    this._shared.useProxy = Boolean(this._useProxyForCurrent);
    const player = this._playerState();
    this._shared.outputMode = player.output_mode || "browser";
    this._shared.targetMediaPlayer = player.target_media_player || null;
    this._shared.targetMediaPlayerName = player.target_media_player_name || null;
    if (!this._fixedOutputTarget) this._shared.preferredOutputTarget = this._preferredOutputTarget || "browser";
  }

  _preferenceKey(name) {
    const entity = (this._config && this._config.entity) || "podcast_player";
    const mode = this._mode();
    const storageId = this._config && this._config.storage_id ? `:${this._config.storage_id}` : "";
    return `haPodcastPlayer:${entity}:${mode}${storageId}:${name}`;
  }

  _readPreference(name, fallback) {
    if (!this._rememberState) return fallback;
    try {
      const value = window.localStorage.getItem(this._preferenceKey(name));
      return value === null || value === undefined || value === "" ? fallback : value;
    } catch (_) {
      return fallback;
    }
  }

  _writePreference(name, value) {
    if (!this._rememberState) return;
    try {
      window.localStorage.setItem(this._preferenceKey(name), String(value));
    } catch (_) {}
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("Podcast Player Card requires an entity, e.g. media_player.podcast_player");
    }
    this._config = config;
    this._rememberState = config.remember_state !== false;
    const defaultFeed = config.default_feed || "all";
    const defaultFilter = config.default_filter || "all";
    this._selectedFeed = this._readPreference("selected_feed", defaultFeed);
    this._filter = this._readPreference("filter", defaultFilter);
    this._fixedOutputTarget = config.output_target !== undefined;
    if (this._fixedOutputTarget) {
      this._preferredOutputTarget = PodcastPlayerCard._normalizeOutputTarget(config.output_target);
    } else {
      this._preferredOutputTarget = PodcastPlayerCard._readOutputTargetPreference(config.default_output || "browser");
      if (this._shared) this._shared.preferredOutputTarget = this._preferredOutputTarget;
    }
    this._lastSpeakerTarget = this._readPreference("last_speaker_target", "") || null;
  }

  set hass(hass) {
    this._hass = hass;
    this._syncOutputState();
    if (!this._library && !this._loading) {
      this._loadLibrary();
    } else {
      this._scheduleRender();
    }
  }

  connectedCallback() {
    this._attachAudioListeners();
    window.addEventListener("podcast-player-speed-changed", this._boundSharedSpeedHandler);
    window.addEventListener("podcast-player-output-target-changed", this._boundSharedOutputHandler);
    window.addEventListener("storage", this._boundStorageHandler);
    this._syncOutputState();
    this._syncFromShared();
    if ((!this._audio.paused && !this._audio.ended) || (this._isSpeakerOutput() && !this._isLimitedSpeakerOutput())) this._startProgressTimer();
    this._render();
  }

  disconnectedCallback() {
    this._saveProgress(!this._audio.paused);
    window.removeEventListener("podcast-player-speed-changed", this._boundSharedSpeedHandler);
    window.removeEventListener("podcast-player-output-target-changed", this._boundSharedOutputHandler);
    window.removeEventListener("storage", this._boundStorageHandler);
    this._detachAudioListeners();
    if (this._progressTimer) {
      window.clearInterval(this._progressTimer);
      this._progressTimer = null;
    }
  }

  getCardSize() {
    const mode = this._mode();
    if (mode === "compact") return 3;
    if (mode === "latest") return 3;
    return 8;
  }

  getGridOptions() {
    const mode = this._mode();
    if (mode === "latest" || mode === "compact") {
      return {
        rows: 3,
        columns: 6,
        min_rows: 2,
        min_columns: 4,
      };
    }
    return {
      rows: 8,
      columns: 8,
      min_rows: 5,
      min_columns: 6,
    };
  }

  _mode() {
    return String(this._config.mode || "full").toLowerCase();
  }

  async _loadLibrary() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      this._library = await this._hass.connection.sendMessagePromise({
        type: "podcast_player/get_library",
        feed_id: this._selectedFeed,
        filter: this._filter,
        limit: this._config.limit || 200,
      });
      const activeFeedIds = new Set((this._library.feeds || []).map((feed) => feed.feed_id));
      if (this._selectedFeed !== "all" && !activeFeedIds.has(this._selectedFeed)) {
        this._selectedFeed = "all";
        this._writePreference("selected_feed", this._selectedFeed);
        this._library = await this._hass.connection.sendMessagePromise({
          type: "podcast_player/get_library",
          feed_id: "all",
          filter: this._filter,
          limit: this._config.limit || 200,
        });
      }
      this._syncOutputState();
      this._syncFromShared();
      this._setSharedBrowserSpeed(this._currentBrowserSpeed(), false, false);
      const player = this._playerState();
      if (player.current_episode_id) {
        // The backend/current output is authoritative. This matters when
        // the full card is filtered to Candace while the Control Center started
        // a JRE episode on the TV: the current episode may not be in the
        // filtered episode list.
        this._selectedEpisodeId = player.current_episode_id;
      } else if (!this._selectedEpisodeId) {
        this._selectedEpisodeId = this._shared.currentEpisodeId || (this._library.episodes[0] && this._library.episodes[0].episode_id) || null;
      }
      let selectedEpisode = this._findEpisode(this._selectedEpisodeId);
      if (!selectedEpisode && player.current_episode_id) {
        selectedEpisode = await this._loadEpisodeById(player.current_episode_id);
      }
      this._currentEpisode = selectedEpisode || (!this._isSpeakerOutput() ? this._shared.currentEpisode : null) || this._library.episodes[0] || null;
      this._syncToShared();
      if ((!this._audio.paused && !this._audio.ended) || (this._isSpeakerOutput() && !this._isLimitedSpeakerOutput())) this._startProgressTimer();
      this._lastRenderKey = "";
    } catch (err) {
      this._error = this._errorText(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _findEpisode(id) {
    if (!id || !this._library) return null;
    return this._library.episodes.find((ep) => ep.episode_id === id) || null;
  }

  async _loadEpisodeById(id) {
    if (!this._hass || !id) return null;
    try {
      return await this._hass.connection.sendMessagePromise({
        type: "podcast_player/get_episode",
        episode_id: id,
      });
    } catch (err) {
      console.warn("Podcast Player could not load current episode", id, err);
      return null;
    }
  }

  _feedFor(feedId) {
    if (!feedId || !this._library) return null;
    return (this._library.feeds || []).find((feed) => feed.feed_id === feedId) || null;
  }

  _feedTitleFor(item) {
    return item.feed_title || (this._feedFor(item.feed_id) || {}).title || "Podcast";
  }

  _artFor(item) {
    return item.artwork_url || item.feed_artwork_url || (this._feedFor(item.feed_id) || {}).artwork_url || "";
  }

  _statusFor(item) {
    const pos = Number(item.position || 0);
    if (item.played) return "played";
    if (pos > 0) return "in progress";
    return "new";
  }

  _haPlayerAttributes() {
    if (!this._hass || !this._config || !this._config.entity) return {};
    const state = this._hass.states && this._hass.states[this._config.entity];
    return (state && state.attributes) || {};
  }

  _playerState() {
    const base = Object.assign({}, (this._library && this._library.player) || {});
    const attrs = this._haPlayerAttributes();
    const map = {
      current_episode_id: "current_episode_id",
      current_feed_id: "current_feed_id",
      position: "position",
      duration: "duration",
      playback_speed: "speed",
      browser_player_state: "state",
      output_mode: "output_mode",
      target_media_player: "target_media_player",
      target_media_player_name: "target_media_player_name",
      speaker_url_mode: "speaker_url_mode",
      speaker_media_content_type: "speaker_media_content_type",
      speaker_last_error: "speaker_last_error",
    };
    Object.entries(map).forEach(([from, to]) => {
      if (attrs[from] !== undefined && attrs[from] !== null) base[to] = attrs[from];
    });
    return base;
  }

  _isSpeakerOutput() {
    const player = this._playerState();
    return player.output_mode === "speaker" && Boolean(player.target_media_player);
  }

  _speakerTargetEntity() {
    return this._playerState().target_media_player || null;
  }

  _speakerTargetName() {
    const player = this._playerState();
    return player.target_media_player_name || player.target_media_player || "speaker";
  }
  _availableOutputTargets() {
    const backendTargets = (this._library && Array.isArray(this._library.output_targets)) ? this._library.output_targets : [];
    if (backendTargets.length) return backendTargets.slice().sort((a, b) => String(a.name || a.entity_id).localeCompare(String(b.name || b.entity_id)));

    if (!this._hass || !this._hass.states) return [];
    const currentEntity = (this._config && this._config.entity) || "media_player.podcast_player";
    return Object.values(this._hass.states)
      .filter((state) => state && state.entity_id && state.entity_id.startsWith("media_player."))
      .filter((state) => state.entity_id !== currentEntity)
      .filter((state) => !String(state.entity_id).includes("spotify"))
      .filter((state) => !String((state.attributes && state.attributes.friendly_name) || "").toLowerCase().includes("spotify"))
      .map((state) => ({
        entity_id: state.entity_id,
        name: (state.attributes && state.attributes.friendly_name) || state.entity_id.replace(/^media_player\./, ""),
        capabilities: {
          live_state: !["unknown", "unavailable"].includes(String(state.state || "unknown")),
          progress: Boolean(state.attributes && (state.attributes.media_position !== undefined || state.attributes.media_duration !== undefined || state.attributes.media_position_updated_at !== undefined)),
          seek: "best_effort",
          stop: "best_effort",
          artwork: "metadata",
        },
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  _outputTargetInfo(entityId) {
    if (!entityId || entityId === "browser") return null;
    return this._availableOutputTargets().find((target) => target.entity_id === entityId) || null;
  }

  _targetCapabilities(entityId) {
    const info = this._outputTargetInfo(entityId);
    return (info && info.capabilities) || {};
  }

  _targetLimitedByCapabilities(entityId) {
    const info = this._outputTargetInfo(entityId);
    if (info && info.capabilities && info.capabilities.limited_controls !== undefined) return Boolean(info.capabilities.limited_controls);
    return !this._targetSupportsLiveState(entityId);
  }

  _targetCapabilitySummary(entityId) {
    if (!entityId || entityId === "browser") return "Browser has full controls.";
    const name = this._outputNameFor(entityId);
    const missing = [];
    if (!this._targetSupportsProgress(entityId)) missing.push("progress");
    if (!this._targetCanSeek(entityId)) missing.push("seek");
    if (!this._targetCanPause(entityId)) missing.push("pause/resume");
    if (!this._targetCanSpeed(entityId)) missing.push("speed");
    if (!missing.length) return `${name} supports full controls.`;
    return `${name} does not support: ${missing.join(", ")}.`;
  }

  _targetHasCapabilityNote(entityId) {
    if (!entityId || entityId === "browser") return false;
    return !this._targetSupportsProgress(entityId) || !this._targetCanSeek(entityId) || !this._targetCanPause(entityId) || !this._targetCanSpeed(entityId);
  }

  _activeOutputValue() {
    if (this._isSpeakerOutput()) return this._speakerTargetEntity();
    return this._preferredOutputTarget || "browser";
  }

  _preferredSpeakerTarget() {
    const value = this._preferredOutputTarget || "browser";
    return value !== "browser" ? value : null;
  }

  _outputNameFor(entityId) {
    if (!entityId || entityId === "browser") return "Browser";
    const state = this._hass && this._hass.states ? this._hass.states[entityId] : null;
    return (state && state.attributes && state.attributes.friendly_name) || entityId;
  }

  _renderOutputSelect() {
    const e = (v) => this._escape(v);
    const targets = this._availableOutputTargets();
    if (!targets.length) return "";
    const active = this._activeOutputValue();
    const note = active !== "browser" && this._targetHasCapabilityNote(active) ? this._targetCapabilitySummary(active) : "";
    return `
      <div class="output-control">
        <span>Output</span>
        <select id="output-select" title="Podcast output">
          <option value="browser" ${active === "browser" ? "selected" : ""}>Browser</option>
          ${targets.map((target) => `<option value="${e(target.entity_id)}" ${active === target.entity_id ? "selected" : ""}>${e(target.name)}</option>`).join("")}
        </select>
        ${note ? `<span class="cap-info" tabindex="0" aria-label="${e(note)}" title="${e(note)}">!</span>` : ""}
      </div>
    `;
  }


  _speakerState() {
    const target = this._speakerTargetEntity();
    return target && this._hass && this._hass.states ? this._hass.states[target] : null;
  }

  _selectedSpeakerTarget() {
    return this._isSpeakerOutput() ? this._speakerTargetEntity() : this._preferredSpeakerTarget();
  }

  _selectedSpeakerState() {
    const target = this._selectedSpeakerTarget();
    return target && this._hass && this._hass.states ? this._hass.states[target] : null;
  }

  _targetSupportsLiveState(target) {
    const caps = this._targetCapabilities(target);
    if (caps.live_state !== undefined) return Boolean(caps.live_state);
    if (!target || !this._hass || !this._hass.states) return false;
    const state = this._hass.states[target];
    if (!state || !state.state) return false;
    return !["unknown", "unavailable"].includes(String(state.state));
  }

  _targetSupportsProgress(target) {
    const caps = this._targetCapabilities(target);
    if (caps.progress !== undefined) return Boolean(caps.progress);
    if (!this._targetSupportsLiveState(target)) return false;
    const state = this._hass.states[target];
    const attrs = (state && state.attributes) || {};
    return attrs.media_position !== undefined || attrs.media_duration !== undefined || attrs.media_position_updated_at !== undefined;
  }

  _targetSeekMode(target) {
    if (!target || target === "browser") return "supported";
    const caps = this._targetCapabilities(target);
    return String(caps.seek || "none");
  }

  _targetPauseMode(target) {
    if (!target || target === "browser") return "supported";
    const caps = this._targetCapabilities(target);
    return String(caps.pause || "none");
  }

  _targetCanPause(target) {
    if (!target || target === "browser") return true;
    return this._targetSupportsLiveState(target) && this._targetPauseMode(target) === "supported";
  }

  _targetCanSeek(target) {
    if (!target || target === "browser") return true;
    const mode = this._targetSeekMode(target);
    // Relative seek buttons need a real current position. For devices that do
    // not report progress, seeking stays backend best-effort for resume/start,
    // but the card must not pretend -15/+30 is accurate.
    return this._targetSupportsProgress(target) && (mode === "supported" || mode === "best_effort");
  }

  _targetCanShowProgress(target) {
    return !target || target === "browser" || this._targetSupportsProgress(target);
  }

  _targetCanSpeed(target) {
    if (!target || target === "browser") return true;
    const caps = this._targetCapabilities(target);
    return caps.speed === true || caps.speed === "supported";
  }

  _selectedControlTarget() {
    const active = this._activeOutputValue();
    return !active || active === "browser" ? null : active;
  }

  _selectedCanSpeed() {
    const active = this._activeOutputValue();
    return !active || active === "browser" || this._targetCanSpeed(active);
  }

  _playPauseLabelForSelected(playing) {
    const target = this._selectedSpeakerTarget();
    if (this._isSpeakerOutput() && !this._targetCanPause(target)) return "Restart";
    return playing ? "Pause" : "Play";
  }

  _externalCapabilityNote(target) {
    return this._targetCapabilitySummary(target);
  }

  _speakerTargetSupportsLiveState() {
    return this._targetSupportsLiveState(this._speakerTargetEntity());
  }

  _speakerTargetSupportsProgress() {
    return this._targetSupportsProgress(this._speakerTargetEntity());
  }

  _selectedSpeakerSupportsLiveState() {
    return this._targetSupportsLiveState(this._selectedSpeakerTarget());
  }

  _hasSelectedLimitedExternalOutput() {
    const target = this._selectedSpeakerTarget();
    return Boolean(target) && this._targetLimitedByCapabilities(target);
  }

  _isLimitedSpeakerOutput() {
    const target = this._speakerTargetEntity();
    return this._isSpeakerOutput() && this._targetLimitedByCapabilities(target);
  }

  _limitedExternalName() {
    const target = this._selectedSpeakerTarget();
    if (this._isSpeakerOutput()) return this._speakerTargetName();
    return this._outputNameFor(target);
  }

  _speakerControlText() {
    if (this._isLimitedSpeakerOutput()) return `External playback on ${this._speakerTargetName()}`;
    return this._isSpeakerOutput() ? `Playing on ${this._speakerTargetName()}` : "Browser playback";
  }

  _speakerTiming() {
    const state = this._speakerState();
    const attrs = (state && state.attributes) || {};
    const ep = this._currentEpisode || {};
    const target = this._speakerTargetEntity();
    if (!this._targetSupportsProgress(target)) {
      return { position: 0, duration: Number(ep.duration_seconds || this._playerState().duration || 0), limited: true };
    }
    let position = Number(attrs.media_position ?? this._playerState().position ?? ep.position ?? 0);
    const duration = Number(attrs.media_duration ?? this._playerState().duration ?? ep.duration_seconds ?? 0);
    if (state && state.state === "playing" && attrs.media_position_updated_at) {
      const updated = new Date(attrs.media_position_updated_at).getTime();
      if (Number.isFinite(updated)) position += Math.max(0, (Date.now() - updated) / 1000);
    }
    if (duration > 0) position = Math.min(position, duration);
    return { position: Math.max(0, position || 0), duration: Math.max(0, duration || 0), limited: false };
  }

  _displayPositionDuration() {
    if (this._isSpeakerOutput()) return this._speakerTiming();
    const ep = this._currentEpisode || {};
    return {
      position: Number(this._audio.currentTime || ep.position || 0),
      duration: Number((Number.isFinite(this._audio.duration) && this._audio.duration) || ep.duration_seconds || 0),
    };
  }

  _isActuallyPlaying() {
    if (this._isSpeakerOutput()) {
      if (this._isLimitedSpeakerOutput()) return this._playerState().state === "playing";
      const state = this._speakerState();
      if (state && state.state) return state.state === "playing";
      return this._playerState().state === "playing";
    }
    return !this._audio.paused && !this._audio.ended;
  }

  _syncOutputState() {
    const player = this._playerState();
    if (player.current_episode_id) {
      this._selectedEpisodeId = player.current_episode_id;
      const found = this._findEpisode(player.current_episode_id);
      if (found) this._currentEpisode = found;
    }
    if (this._isSpeakerOutput()) {
      if (!this._audio.paused) this._audio.pause();
      this._shared.outputMode = "speaker";
      this._shared.targetMediaPlayer = player.target_media_player || null;
      this._shared.targetMediaPlayerName = player.target_media_player_name || null;
      if (!this._isLimitedSpeakerOutput() && this._isActuallyPlaying()) this._startProgressTimer();
    } else {
      this._shared.outputMode = "browser";
      this._shared.targetMediaPlayer = null;
      this._shared.targetMediaPlayerName = null;
    }
  }

  async _addFeed() {
    const input = this.shadowRoot.querySelector("#rss-url");
    const rssUrl = input ? input.value.trim() : "";
    if (!rssUrl) {
      this._error = "Paste a podcast RSS URL first.";
      this._render();
      return;
    }
    this._loading = true;
    this._error = null;
    this._info = "Adding feed…";
    this._render();
    try {
      await this._hass.callService("podcast_player", "add_feed", { rss_url: rssUrl });
      this._info = "Feed added.";
      if (input) input.value = "";
      this._selectedFeed = "all";
      this._writePreference("selected_feed", this._selectedFeed);
      this._selectedEpisodeId = null;
      this._library = null;
      await this._loadLibrary();
    } catch (err) {
      this._error = this._errorText(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  async _refresh() {
    this._loading = true;
    this._error = null;
    this._info = "Refreshing feeds…";
    this._render();
    try {
      const data = this._selectedFeed && this._selectedFeed !== "all" ? { feed_id: this._selectedFeed } : {};
      await this._hass.callService("podcast_player", "refresh", data);
      this._info = "Refresh complete.";
      this._library = null;
      await this._loadLibrary();
    } catch (err) {
      this._error = this._errorText(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  async _removeSelectedFeed() {
    if (!this._selectedFeed || this._selectedFeed === "all") {
      this._error = "Select a specific feed before removing.";
      this._render();
      return;
    }
    const feed = this._feedFor(this._selectedFeed);
    const title = feed ? feed.title : this._selectedFeed;
    if (!confirm(`Remove podcast feed “${title}”? Listening history will be kept.`)) return;
    try {
      await this._hass.callService("podcast_player", "remove_feed", {
        feed_id: this._selectedFeed,
        keep_history: true,
      });
      this._selectedFeed = "all";
      this._writePreference("selected_feed", this._selectedFeed);
      this._selectedEpisodeId = null;
      this._library = null;
      await this._loadLibrary();
    } catch (err) {
      this._error = this._errorText(err);
      this._render();
    }
  }

  async _selectEpisode(id, autoplay = false) {
    const ep = this._findEpisode(id);
    if (!ep) return;
    const previous = this._currentEpisode;
    if (autoplay && previous && previous.episode_id !== id && !this._audio.paused) {
      await this._saveProgressForEpisode(previous, true);
    }
    this._selectedEpisodeId = id;
    this._currentEpisode = ep;
    this._useProxyForCurrent = false;
    this._error = null;
    this._syncToShared();
    this._lastRenderKey = "";
    this._render();
    if (autoplay) {
      await this._playSelected();
    }
  }

  _resumePositionForEpisode(ep) {
    if (!ep) return 0;
    const player = this._playerState();
    const values = [];
    if (this._currentEpisode && this._currentEpisode.episode_id === ep.episode_id && !this._isSpeakerOutput()) {
      values.push(this._audio.currentTime);
    }
    values.push(ep.position);
    if (player.current_episode_id === ep.episode_id) values.push(player.position);
    for (const value of values) {
      const pos = Number(value || 0);
      if (Number.isFinite(pos) && pos > 0) return pos;
    }
    return 0;
  }

  _seekBrowserToResumePosition(ep) {
    if (!ep || this._isSpeakerOutput()) return;
    const position = Number(this._resumePositionForEpisode(ep) || 0);
    if (!(position > 0)) return;
    const applySeek = () => {
      try {
        const duration = Number.isFinite(this._audio.duration) ? this._audio.duration : position;
        const target = Math.min(position, duration || position);
        if (!Number.isFinite(this._audio.currentTime) || Math.abs((this._audio.currentTime || 0) - target) > 1.5) {
          this._audio.currentTime = target;
        }
      } catch (_) {}
    };
    if (Number.isFinite(this._audio.duration) && this._audio.duration > 0) {
      applySeek();
    } else {
      this._audio.addEventListener("loadedmetadata", applySeek, { once: true });
    }
  }

  async _playSelected() {
    if (!this._currentEpisode) return;
    const ep = this._currentEpisode;
    this._selectedEpisodeId = ep.episode_id;
    this._syncToShared();
    this._error = null;

    if (this._isSpeakerOutput() || this._preferredSpeakerTarget()) {
      await this._playSelectedOnSpeaker(ep);
      return;
    }

    try {
      await this._hass.callService("podcast_player", "play_episode", { episode_id: ep.episode_id });
    } catch (err) {
      this._error = this._errorText(err);
      this._render();
      return;
    }

    const desiredSrc = this._useProxyForCurrent ? ep.proxy_url : ep.audio_url;
    if (!desiredSrc) {
      this._error = "This episode has no playable audio URL.";
      this._render();
      return;
    }

    const absoluteDesiredSrc = new URL(desiredSrc, window.location.origin).href;
    if (this._audio.src !== absoluteDesiredSrc) {
      this._audio.src = desiredSrc;
      this._audio.load();
    }
    // Important for source switching: if the same episode URL is reused after
    // stopping an external player, the browser audio src may already match.
    // Still seek to the saved progress instead of starting from 0:00.
    this._seekBrowserToResumePosition(ep);
    this._setSharedBrowserSpeed(this._currentBrowserSpeed(ep), false, false);
    this._syncToShared();
    try {
      await this._audio.play();
      this._startProgressTimer();
      this._lastRenderKey = "";
      this._render();
    } catch (err) {
      if (!this._useProxyForCurrent) {
        this._info = "Direct playback failed. Trying Home Assistant proxy…";
        this._useProxyForCurrent = true;
        this._syncToShared();
        this._audio.src = ep.proxy_url;
        this._audio.load();
        try {
          await this._audio.play();
          this._startProgressTimer();
          this._lastRenderKey = "";
          this._render();
          return;
        } catch (proxyErr) {
          this._error = this._errorText(proxyErr) || "Audio proxy failed.";
        }
      } else {
        this._error = this._errorText(err) || "Audio playback failed.";
      }
      this._render();
    }
  }

  async _playSelectedOnSpeaker(ep, explicitTarget = null) {
    const target = explicitTarget || this._speakerTargetEntity() || this._preferredSpeakerTarget();
    if (!target) {
      this._error = "No speaker target is selected.";
      this._render();
      return;
    }
    // Capture the browser position before pausing/changing output. Without this,
    // switching Browser -> TV can restart the stream from 0:00 even though the
    // episode had a saved progress point. The backend also checks stored
    // progress, so this is only a precise hint.
    let resumePosition = this._resumePositionForEpisode(ep);
    if (!this._audio.paused) {
      resumePosition = Number(this._audio.currentTime || resumePosition || 0);
      this._audio.pause();
      await this._saveProgress(false);
    }
    const player = this._playerState();
    try {
      await this._hass.callService("podcast_player", "play_on_media_player", {
        media_player_entity_id: target,
        episode_id: ep.episode_id,
        url_mode: player.speaker_url_mode || "direct",
        media_content_type: player.speaker_media_content_type || "music",
        resume_position: resumePosition || undefined,
      });
      this._setSharedOutputTarget(target, true, true);
      this._lastSpeakerTarget = target;
      this._writePreference("last_speaker_target", target);
      this._info = `Playing on ${this._outputNameFor(target)}.`;
      this._selectedEpisodeId = ep.episode_id;
      this._currentEpisode = ep;
      if (!this._isLimitedSpeakerOutput()) this._startProgressTimer();
      this._library = null;
      await this._loadLibrary();
    } catch (err) {
      this._error = this._errorText(err);
      this._render();
    }
  }

  async _togglePlay() {
    if (!this._currentEpisode) return;
    if (this._isSpeakerOutput()) {
      const target = this._speakerTargetEntity();
      if (!this._targetCanPause(target)) {
        await this._playSelectedOnSpeaker(this._currentEpisode);
        return;
      }
      const service = this._isActuallyPlaying() ? "pause" : "resume";
      await this._hass.callService("podcast_player", service, {});
      if (this._library && this._library.player) this._library.player.state = service === "pause" ? "paused" : "playing";
      this._lastRenderKey = "";
      this._render();
      return;
    }
    if (!this._audio.paused && !this._audio.ended) {
      this._audio.pause();
      await this._hass.callService("podcast_player", "pause", {});
      await this._saveProgress(false);
      this._lastRenderKey = "";
      this._render();
      return;
    }
    await this._playSelected();
  }

  async _nativeStopTarget(target) {
    if (!target) return false;
    const attempts = [
      () => this._hass.callService("media_player", "media_stop", {}, { entity_id: target }),
      () => this._hass.callService("media_player", "media_stop", { entity_id: target }),
    ];
    let lastErr = null;
    for (const attempt of attempts) {
      try {
        await attempt();
        return true;
      } catch (err) {
        lastErr = err;
      }
    }
    if (lastErr) throw lastErr;
    return false;
  }

  async _stop() {
    const selectedTarget = this._selectedSpeakerTarget();
    const activeSpeakerTarget = this._speakerTargetEntity();
    const lastSpeakerTarget = this._lastSpeakerTarget || this._readPreference("last_speaker_target", "") || null;
    const browserPlaying = !this._audio.paused && !this._audio.ended;

    // If the UI/backend already fell back to Browser while an external DLNA TV
    // kept playing, do not lose the target. Reuse the last known speaker target
    // when browser audio itself is not playing.
    if (this._isSpeakerOutput() || selectedTarget || (!browserPlaying && lastSpeakerTarget)) {
      const target = activeSpeakerTarget || selectedTarget || lastSpeakerTarget;
      let stopped = false;
      let lastErr = null;
      try {
        this._info = target ? `Stopping ${this._outputNameFor(target)}…` : "Stopping external player…";
        this._error = null;
        this._render();
        await this._hass.callService("podcast_player", "stop_media_player", target ? { media_player_entity_id: target } : {});
        stopped = true;
      } catch (err) {
        lastErr = err;
      }
      // Only use the native HA media_stop fallback for targets that report a
      // useful state. Some DLNA targets report unknown and HA rejects
      // media_stop, so falling back there only creates fake success/errors.
      if (!stopped && target && this._targetSupportsLiveState(target)) {
        try {
          await this._nativeStopTarget(target);
          stopped = true;
        } catch (err) {
          lastErr = err;
        }
      }
      if (!stopped) {
        this._error = this._errorText(lastErr) || `Could not stop ${this._outputNameFor(target)}.`;
        this._render();
        return;
      }
      this._lastSpeakerTarget = target || this._lastSpeakerTarget;
      if (target) this._writePreference("last_speaker_target", target);
      this._setSharedOutputTarget("browser", true, true);
      this._info = target ? `Stop command sent to ${this._outputNameFor(target)}.` : "Stop command sent to external player.";
      this._error = null;
      this._library = null;
      await this._loadLibrary();
      return;
    }
    this._audio.pause();
    this._audio.currentTime = 0;
    this._syncToShared();
    await this._saveProgress(false);
    await this._hass.callService("podcast_player", "stop", {});
    this._lastRenderKey = "";
    this._render();
  }

  async _jump(delta) {
    if (!this._currentEpisode) return;
    const target = this._isSpeakerOutput() ? this._speakerTargetEntity() : null;
    if (target && !this._targetCanSeek(target)) {
      this._info = `${this._outputNameFor(target)} does not support seek.`;
      this._render();
      return;
    }
    const timing = this._displayPositionDuration();
    const seekPosition = Math.max(0, timing.position + delta);
    if (!this._isSpeakerOutput()) {
      try {
        this._audio.currentTime = seekPosition;
      } catch (_) {}
    }
    await this._hass.callService("podcast_player", "seek", {
      episode_id: this._currentEpisode.episode_id,
      position: seekPosition,
    });
    this._currentEpisode.position = seekPosition;
    this._updateDynamicUi();
  }

  async _setSpeed(speed) {
    speed = this._setSharedBrowserSpeed(speed, true, true);
    this._syncToShared();
    if (this._currentEpisode) this._currentEpisode.playback_speed = speed;
    try {
      await this._hass.callService("podcast_player", "set_speed", {
        speed,
        episode_id: this._currentEpisode ? this._currentEpisode.episode_id : undefined,
      });
      await this._saveProgress(!this._audio.paused);
    } catch (err) {
      this._error = this._errorText(err) || "Could not save playback speed.";
    }
    this._updateDynamicUi(true);
  }

  async _markPlayed(played) {
    if (!this._currentEpisode) return;
    const service = played ? "mark_played" : "mark_unplayed";
    await this._hass.callService("podcast_player", service, { episode_id: this._currentEpisode.episode_id });
    this._currentEpisode.played = played;
    this._library = null;
    this._selectedEpisodeId = this._currentEpisode.episode_id;
    await this._loadLibrary();
  }

  _startProgressTimer() {
    if (this._progressTimer) return;
    this._progressTimer = window.setInterval(() => this._saveProgress(!this._audio.paused), 10000);
  }

  _onTimeUpdate() {
    if (this._currentEpisode) {
      this._currentEpisode.position = this._audio.currentTime || 0;
      if (Number.isFinite(this._audio.duration)) this._currentEpisode.duration_seconds = this._audio.duration;
    }
    const now = Date.now();
    if (now - this._lastSave > 10000) {
      this._lastSave = now;
      this._saveProgress(!this._audio.paused);
    }
    if (now - this._lastDynamicUpdate > 250) {
      this._lastDynamicUpdate = now;
      this._updateDynamicUi();
    }
  }

  _onLoadedMetadata() {
    if (this._currentEpisode && Number.isFinite(this._audio.duration)) {
      this._currentEpisode.duration_seconds = this._audio.duration;
      this._saveProgress(!this._audio.paused);
      this._updateDynamicUi();
    }
  }

  async _onEnded() {
    if (this._currentEpisode) {
      this._currentEpisode.position = this._audio.duration || this._currentEpisode.duration_seconds || 0;
      await this._saveProgress(false);
      await this._markPlayed(true);
    }
  }

  async _onAudioError() {
    if (!this._currentEpisode || this._audio.paused) return;
    if (!this._useProxyForCurrent) {
      this._info = "Direct playback failed. Trying Home Assistant proxy…";
      this._useProxyForCurrent = true;
      this._syncToShared();
      const pos = this._audio.currentTime || this._currentEpisode.position || 0;
      this._audio.src = this._currentEpisode.proxy_url;
      this._audio.load();
      this._audio.addEventListener(
        "loadedmetadata",
        () => {
          try {
            this._audio.currentTime = pos;
          } catch (_) {}
        },
        { once: true }
      );
      try {
        await this._audio.play();
      } catch (err) {
        this._error = this._errorText(err) || "Audio proxy failed.";
      }
    } else {
      this._error = "Audio playback failed.";
    }
    this._lastRenderKey = "";
    this._render();
  }

  async _saveProgressForEpisode(episode, playing) {
    if (!this._hass || !episode) return;
    const isCurrent = this._currentEpisode && episode.episode_id === this._currentEpisode.episode_id;
    const timing = isCurrent ? this._displayPositionDuration() : { position: Number(episode.position || 0), duration: Number(episode.duration_seconds || 0) };
    const position = Number(timing.position || 0);
    const duration = Number(timing.duration || 0);
    try {
      await this._hass.callService("podcast_player", "save_progress", {
        episode_id: episode.episode_id,
        position,
        duration: duration || undefined,
        playing: Boolean(isCurrent ? this._isActuallyPlaying() : playing),
        speed: this._currentBrowserSpeed(episode),
      });
    } catch (err) {
      console.warn("Podcast progress save failed", err);
    }
  }

  async _saveProgress(playing) {
    if (!this._hass || !this._currentEpisode) return;
    this._syncToShared();
    if (this._isLimitedSpeakerOutput()) return;
    await this._saveProgressForEpisode(this._currentEpisode, playing);
  }

  _isEditingControl() {
    const active = this.shadowRoot.activeElement;
    return Boolean(active && ["SELECT", "INPUT"].includes(active.tagName));
  }

  _scheduleRender() {
    if (this._renderTimer) return;
    const delay = this._isEditingControl() || Date.now() < this._deferRenderUntil ? 1600 : 500;
    this._renderTimer = window.setTimeout(() => {
      this._renderTimer = null;
      if (this._isEditingControl() || Date.now() < this._deferRenderUntil) {
        this._scheduleRender();
        return;
      }
      const nextKey = this._renderKey();
      if (nextKey === this._lastRenderKey) {
        this._updateDynamicUi();
        return;
      }
      this._render();
    }, delay);
  }

  async _changeOutputTarget(value) {
    const nextTarget = value || "browser";

    if (nextTarget === "browser") {
      if (this._isSpeakerOutput()) {
        await this._stop();
        // _stop writes Browser only after the stop command path succeeds.
      } else {
        this._setSharedOutputTarget("browser", true, true);
        this._info = "Output set to Browser.";
        this._lastRenderKey = "";
        this._render();
      }
      return;
    }

    this._setSharedOutputTarget(nextTarget, true, true);
    this._lastSpeakerTarget = nextTarget;
    this._writePreference("last_speaker_target", nextTarget);

    if (this._currentEpisode) {
      await this._playSelectedOnSpeaker(this._currentEpisode, this._preferredOutputTarget);
    } else {
      this._info = `Output set to ${this._outputNameFor(this._preferredOutputTarget)}. Select an episode to play there.`;
      this._lastRenderKey = "";
      this._render();
    }
  }

  _changeFeed(value) {
    this._selectedFeed = value;
    this._writePreference("selected_feed", this._selectedFeed);
    this._selectedEpisodeId = null;
    this._pendingFeedScrollIntoView = true;
    this._resetEpisodeScrollOnNextRender = this._mode() === "full";
    this._library = null;
    this._loadLibrary();
  }

  _changeFilter(value) {
    this._filter = value;
    this._writePreference("filter", this._filter);
    this._selectedEpisodeId = null;
    this._resetEpisodeScrollOnNextRender = true;
    this._library = null;
    this._loadLibrary();
  }

  _formatTime(seconds) {
    seconds = Number(seconds || 0);
    if (!Number.isFinite(seconds) || seconds <= 0) return "0:00";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  _formatDate(value) {
    if (!value) return "Unknown date";
    try {
      return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(value));
    } catch (_) {
      return value;
    }
  }

  _stripHtml(value) {
    return String(value || "").replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _errorText(err) {
    if (!err) return "Unknown error";
    if (typeof err === "string") return err;
    if (err.message) return err.message;
    if (err.error && err.error.message) return err.error.message;
    if (err.body && err.body.message) return err.body.message;
    return "Unknown error";
  }

  _captureScrollState() {
    const episodes = this.shadowRoot.querySelector(".episodes");
    if (episodes) this._episodeScrollTop = episodes.scrollTop || 0;
    const strip = this.shadowRoot.querySelector(".feed-strip");
    if (strip) this._feedStripScrollLeft = strip.scrollLeft || 0;
  }

  _restoreScrollState() {
    window.requestAnimationFrame(() => {
      const episodes = this.shadowRoot.querySelector(".episodes");
      if (episodes) {
        if (this._resetEpisodeScrollOnNextRender) {
          episodes.scrollTop = 0;
          this._episodeScrollTop = 0;
          this._resetEpisodeScrollOnNextRender = false;
        } else {
          episodes.scrollTop = this._episodeScrollTop || 0;
        }
      }

      const strip = this.shadowRoot.querySelector(".feed-strip");
      if (strip) {
        if (this._pendingFeedScrollIntoView) {
          const selected = strip.querySelector(".feed-chip.selected");
          if (selected) selected.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
          this._pendingFeedScrollIntoView = false;
        } else {
          strip.scrollLeft = this._feedStripScrollLeft || 0;
        }
      }
    });
  }

  _renderKey() {
    const ep = this._currentEpisode;
    const lib = this._library;
    const counts = lib && lib.counts ? lib.counts : {};
    return JSON.stringify({
      mode: this._mode(),
      loading: this._loading,
      error: this._error,
      info: this._info,
      selectedFeed: this._selectedFeed,
      filter: this._filter,
      episodeId: ep && ep.episode_id,
      playing: this._isActuallyPlaying(),
      outputMode: this._playerState().output_mode || "browser",
      target: this._playerState().target_media_player || "",
      preferredOutputTarget: this._preferredOutputTarget || "browser",
      canSpeed: this._selectedCanSpeed(),
      currentSpeed: this._currentBrowserSpeed(ep),
      outputTargets: this._availableOutputTargets().map((t) => `${t.entity_id}:${((t.capabilities || {}).limited_controls) ? "limited" : "full"}:${((t.capabilities || {}).seek) || ""}:${((t.capabilities || {}).speed) || ""}`).join(","),
      speakerLimited: this._isLimitedSpeakerOutput(),
      playerState: this._playerState().state || "",
      feeds: counts.enabled_feeds,
      total: counts.total_episodes,
      unplayed: counts.unplayed,
      episodeCount: lib && lib.episodes ? lib.episodes.length : 0,
    });
  }

  _render() {
    if (this._isEditingControl() && Date.now() < this._deferRenderUntil) return;
    this._captureScrollState();
    const mode = this._mode();
    if (mode === "compact") {
      this._renderCompact();
    } else if (mode === "latest") {
      this._renderLatest();
    } else {
      this._renderFull();
    }
    this._lastRenderKey = this._renderKey();
    this._restoreScrollState();
  }

  _baseStyles(extra = "") {
    return `
      :host { display: block; max-width: 100%; overflow: hidden; }
      * { box-sizing: border-box; }
      ha-card { overflow: hidden; max-width: 100%; }
      .wrap { padding: 16px; color: var(--primary-text-color); max-width: 100%; overflow: hidden; }
      .top { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; min-width: 0; }
      .top > div:first-child { min-width: 0; overflow: hidden; }
      .title { font-size: 1.3rem; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .counts, .muted { color: var(--secondary-text-color); font-size: .9rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .add { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; margin-bottom: 12px; min-width: 0; }
      input, select { border: 1px solid var(--divider-color); border-radius: 10px; background: var(--card-background-color); color: var(--primary-text-color); padding: 10px; min-width: 0; }
      button { border: 0; border-radius: 10px; padding: 10px 12px; cursor: pointer; background: var(--primary-color); color: var(--text-primary-color); font-weight: 600; }
      button.secondary { background: var(--secondary-background-color); color: var(--primary-text-color); }
      button.danger { background: var(--error-color); color: white; }
      button.icon { min-width: 42px; }
      button:disabled { opacity: .45; cursor: default; }
      .notice { border-radius: 10px; padding: 10px; margin-bottom: 12px; font-size: .9rem; }
      .error { background: color-mix(in srgb, var(--error-color) 16%, transparent); color: var(--error-color); }
      .info { background: color-mix(in srgb, var(--primary-color) 16%, transparent); color: var(--primary-text-color); }
      .cap-info {
        position: relative;
        display: inline-grid;
        place-items: center;
        width: 19px;
        height: 19px;
        flex: 0 0 auto;
        border: 1px solid var(--secondary-text-color);
        border-radius: 999px;
        color: var(--secondary-text-color);
        font-size: 12px;
        font-weight: 800;
        line-height: 1;
        cursor: help;
        user-select: none;
      }
      .cap-info::after {
        content: attr(aria-label);
        display: none;
        position: absolute;
        z-index: 10;
        left: 50%;
        bottom: calc(100% + 8px);
        transform: translateX(-50%);
        width: max-content;
        max-width: min(260px, 65vw);
        padding: 6px 8px;
        border-radius: 8px;
        background: var(--card-background-color);
        border: 1px solid var(--divider-color);
        box-shadow: 0 6px 18px rgba(0,0,0,.24);
        color: var(--primary-text-color);
        font-size: .78rem;
        font-weight: 500;
        line-height: 1.25;
        white-space: normal;
        text-align: left;
      }
      .cap-info:hover::after, .cap-info:focus::after { display: block; }
      .art { width: 124px; aspect-ratio: 1; border-radius: 14px; object-fit: cover; background: var(--secondary-background-color); display: grid; place-items: center; overflow: hidden; flex: 0 0 auto; }
      .art img, .avatar img { width: 100%; height: 100%; object-fit: cover; }
      .art .fallback, .avatar .fallback { color: var(--secondary-text-color); }
      .art .fallback { font-size: 48px; }
      .avatar { width: 42px; height: 42px; border-radius: 10px; overflow: hidden; display: grid; place-items: center; background: var(--secondary-background-color); flex: 0 0 auto; }
      .avatar .fallback { font-size: 20px; }
      .feed-pill { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; min-width: 0; border-radius: 999px; padding: 3px 8px; background: color-mix(in srgb, var(--primary-color) 12%, transparent); color: var(--primary-text-color); font-size: .75rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; vertical-align: middle; }
      .player { display: grid; grid-template-columns: 124px minmax(0, 1fr); gap: 16px; align-items: start; margin: 12px 0 16px; min-width: 0; max-width: 100%; }
      .player > div { min-width: 0; max-width: 100%; overflow: hidden; }
      .ep-title { font-size: 1.05rem; font-weight: 650; margin: 4px 0; overflow-wrap: anywhere; }
      .feed-title, .meta, .desc { color: var(--secondary-text-color); font-size: .9rem; min-width: 0; max-width: 100%; }
      .desc { margin-top: 8px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
      .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 12px; min-width: 0; max-width: 100%; }
      .controls select { max-width: 100%; }
      .speed-control { display: inline-flex; align-items: center; gap: 7px; flex: 0 0 auto; color: var(--secondary-text-color); font-size: .85rem; white-space: nowrap; }
      .speed-control select { min-width: 78px; padding: 10px 30px 10px 10px; }
      .output-control { display: flex; align-items: center; gap: 8px; flex: 1 1 210px; min-width: 180px; max-width: 100%; color: var(--secondary-text-color); font-size: .85rem; }
      .output-control select { flex: 1 1 auto; min-width: 0; }
      .progress-wrap { margin-top: 12px; min-width: 0; max-width: 100%; }
      .bar { height: 8px; background: var(--secondary-background-color); border-radius: 99px; overflow: hidden; cursor: pointer; }
      .bar > div { height: 100%; width: 0%; background: var(--primary-color); transition: width .18s linear; }
      .time { display: flex; justify-content: space-between; color: var(--secondary-text-color); font-size: .8rem; margin-top: 4px; }
      .filters { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 14px 0 8px; min-width: 0; max-width: 100%; overflow: hidden; }
      .filters select { flex: 1 1 140px; min-width: 0; }
      .filters button { flex: 0 0 auto; }
      .feed-strip { display: flex; gap: 8px; overflow-x: auto; overflow-y: hidden; padding: 2px 0 10px; margin-bottom: 4px; max-width: 100%; }
      .feed-chip { display: inline-flex; align-items: center; gap: 8px; max-width: 220px; border: 1px solid color-mix(in srgb, var(--divider-color) 70%, transparent); border-radius: 14px; padding: 7px 10px; background: color-mix(in srgb, var(--secondary-background-color) 52%, transparent); color: var(--primary-text-color); cursor: pointer; backdrop-filter: blur(8px); }
      .feed-chip.selected { border-color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 18%, transparent); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-color) 18%, transparent); }
      .feed-chip .avatar { width: 28px; height: 28px; border-radius: 8px; }
      .feed-chip-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: .86rem; }
      .episodes { max-height: var(--podcast-list-height, 560px); overflow-y: auto; overflow-x: hidden; border-top: 1px solid var(--divider-color); max-width: 100%; }
      .episode { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 10px 8px; border-bottom: 1px solid var(--divider-color); cursor: pointer; border-radius: 10px; min-width: 0; max-width: 100%; }
      .episode:hover { background: var(--secondary-background-color); }
      .episode.selected { background: color-mix(in srgb, var(--primary-color) 12%, transparent); }
      .episode > div { min-width: 0; max-width: 100%; overflow: hidden; }
      .episode .row-title { font-weight: 620; line-height: 1.25; overflow-wrap: anywhere; }
      .episode .row-meta { color: var(--secondary-text-color); font-size: .8rem; margin-top: 3px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; min-width: 0; max-width: 100%; }
      .episode .badge { color: var(--secondary-text-color); font-size: .78rem; white-space: nowrap; }
      .tiny-progress { height: 3px; background: var(--divider-color); border-radius: 10px; margin-top: 6px; overflow: hidden; }
      .tiny-progress > div { height: 100%; background: var(--primary-color); }
      .empty { padding: 24px 0; color: var(--secondary-text-color); text-align: center; }
      .compact-main { display: grid; grid-template-columns: 58px minmax(0, 1fr); gap: 12px; align-items: center; min-width: 0; }
      .compact-main > div, .latest-main > div { min-width: 0; overflow: hidden; }
      .compact-main .art { width: 58px; border-radius: 12px; }
      .compact-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
      .compact-controls button { padding: 8px 10px; }
      .compact-controls .output-control { flex-basis: 100%; }
      .compact-controls .compact-speed-control { flex: 0 0 auto; }
      .compact-controls .compact-speed-control select { min-width: 84px; }
      .compact-controls .icon { min-width: 44px; }
      .compact-status { display: flex; gap: 8px; align-items: center; color: var(--secondary-text-color); font-size: .8rem; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .latest-main { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 12px; align-items: center; min-width: 0; cursor: pointer; border-radius: 12px; padding: 8px; }
      .latest-main:hover { background: var(--secondary-background-color); }
      .latest-main .art { width: 64px; border-radius: 12px; }
      @media (max-width: 620px) {
        .player { grid-template-columns: 88px minmax(0, 1fr); gap: 12px; }
        .art { width: 88px; border-radius: 10px; }
        .filters { display: flex; flex-wrap: wrap; }
        .add { grid-template-columns: 1fr; }
        .episode { grid-template-columns: minmax(0, 1fr) auto; }
        .episode .avatar { display: none; }
        button { padding: 11px 12px; }
      }
      ${extra}
    `;
  }

  _renderFull() {
    const e = (v) => this._escape(v);
    const lib = this._library;
    const ep = this._currentEpisode;
    const counts = lib && lib.counts ? lib.counts : {};
    const feeds = lib ? lib.feeds || [] : [];
    const episodes = lib ? lib.episodes || [] : [];
    const title = this._config.title || "Podcasts";
    const listHeight = this._config.max_height || "560px";

    this.shadowRoot.innerHTML = `
      <style>${this._baseStyles(`.episodes { --podcast-list-height: ${listHeight}; }`)}</style>
      <ha-card>
        <div class="wrap">
          <div class="top">
            <div>
              <div class="title">${e(title)}</div>
              <div class="counts">${e(counts.enabled_feeds || 0)} feeds · ${e(counts.unplayed || 0)} unplayed · ${e(counts.total_episodes || 0)} episodes</div>
            </div>
            <button class="secondary" id="refresh" ${this._loading ? "disabled" : ""}>Refresh</button>
          </div>

          ${this._error ? `<div class="notice error">${e(this._error)}</div>` : ""}
          ${this._info ? `<div class="notice info">${e(this._info)}</div>` : ""}

          <div class="add">
            <input id="rss-url" type="url" placeholder="Paste podcast RSS URL…" />
            <button id="add-feed" ${this._loading ? "disabled" : ""}>Add Feed</button>
          </div>

          ${this._loading && !lib ? `<div class="empty">Loading Podcast Player…</div>` : ""}
          ${ep ? this._renderPlayer(ep) : this._renderEmpty(lib)}
          ${lib ? this._renderFeedStrip(feeds) : ""}
          ${lib ? `
            <div class="filters">
              <select id="feed-select">
                <option value="all" ${this._selectedFeed === "all" ? "selected" : ""}>All feeds</option>
                ${feeds.map((f) => `<option value="${e(f.feed_id)}" ${this._selectedFeed === f.feed_id ? "selected" : ""}>${e(f.title || f.feed_id)}${f.status === "failed" ? " ⚠" : ""}</option>`).join("")}
              </select>
              <select id="filter-select">
                ${["all", "unplayed", "in_progress", "played"].map((f) => `<option value="${f}" ${this._filter === f ? "selected" : ""}>${e(f.replace("_", " "))}</option>`).join("")}
              </select>
              <button class="secondary" id="reload-library">Reload</button>
              <button class="danger" id="remove-feed" ${this._selectedFeed === "all" ? "disabled" : ""}>Remove</button>
            </div>
            <div class="episodes">
              ${episodes.length ? episodes.map((item) => this._renderEpisodeRow(item)).join("") : `<div class="empty">No episodes match this filter.</div>`}
            </div>
          ` : ""}
        </div>
      </ha-card>
    `;
    this._bindEvents();
    this._updateDynamicUi(true);
  }

  _renderCompact() {
    const e = (v) => this._escape(v);
    const ep = this._currentEpisode || (this._library && this._library.episodes && this._library.episodes[0]);
    const title = this._config.title || "Podcast";
    const timing = this._displayPositionDuration();
    const position = Number(timing.position || 0);
    const duration = Number(timing.duration || (ep && ep.duration_seconds) || 0);
    const playing = this._isActuallyPlaying();
    const selectedTarget = this._selectedControlTarget();
    const externalSelected = Boolean(selectedTarget);
    const selectedExternalName = externalSelected ? this._limitedExternalName() : "Browser";
    const showProgress = this._targetCanShowProgress(selectedTarget);
    const canSeek = this._targetCanSeek(selectedTarget);
    const canSpeed = this._selectedCanSpeed();
    const speed = this._currentBrowserSpeed(ep);
    const playLabel = this._playPauseLabelForSelected(playing);
    const output = this._isSpeakerOutput() ? `Playing on ${this._speakerTargetName()}` : (externalSelected ? `Ready for ${selectedExternalName}` : "Browser");
    this.shadowRoot.innerHTML = `
      <style>${this._baseStyles()}</style>
      <ha-card>
        <div class="wrap">
          <div class="top">
            <div>
              <div class="title">${e(title)}</div>
              <div class="counts">${ep ? e(this._feedTitleFor(ep)) : "No episode selected"}</div>
            </div>
            <button class="secondary" id="refresh" ${this._loading ? "disabled" : ""}>Refresh</button>
          </div>
          <div class="compact-main">
            <div class="art">${ep && this._artFor(ep) ? `<img src="${e(this._artFor(ep))}" alt="" />` : `<div class="fallback">🎙️</div>`}</div>
            <div>
              <div class="ep-title">${ep ? e(ep.title || "Untitled episode") : "No episode selected"}</div>
              <div class="compact-status">${showProgress ? `<span>${e(this._formatTime(position))}</span><span>·</span><span>${e(this._formatTime(duration))}</span><span>·</span><span>${playing ? "playing" : "paused"}</span><span>·</span><span>${e(output)}</span>` : `<span>${e(output)}</span>`}</div>
              ${showProgress ? `<div class="progress-wrap"><div class="bar" id="progress-bar"><div></div></div></div>` : ""}
              <div class="compact-controls">
                ${this._renderOutputSelect()}
                ${canSpeed ? `<label class="speed-control compact-speed-control" title="Playback speed"><span>Speed</span><select id="speed" aria-label="Playback speed" ${ep ? "" : "disabled"}>${PodcastPlayerCard._speedOptions().map((s) => `<option value="${s}" ${Number(speed) === s ? "selected" : ""}>${s}x</option>`).join("")}</select></label>` : ""}
                <button class="icon" id="playpause" ${ep ? "" : "disabled"}>${e(playLabel)}</button>
                ${canSeek ? `<button class="secondary icon" id="back" ${ep ? "" : "disabled"}>-15s</button><button class="secondary icon" id="forward" ${ep ? "" : "disabled"}>+30s</button>` : ""}
                ${this._isSpeakerOutput() || externalSelected ? `<button class="secondary" id="stop" ${ep ? "" : "disabled"}>Stop</button>` : ""}
              </div>
            </div>
          </div>
          ${this._error ? `<div class="notice error" style="margin-top:12px">${e(this._error)}</div>` : ""}
        </div>
      </ha-card>
    `;
    this._bindEvents();
    this._updateDynamicUi(true);
  }

  _latestEpisodesForMode() {
    const episodes = (this._library && this._library.episodes) ? this._library.episodes : [];
    if (this._selectedFeed !== "all") return episodes.length ? [episodes[0]] : [];

    // In “All feeds”, show the newest episode from every podcast, not only the
    // single newest episode overall. The backend already returns episodes sorted
    // newest-first, so the first episode we see per feed is that feed's latest.
    const byFeed = new Map();
    for (const episode of episodes) {
      if (!episode || !episode.feed_id || byFeed.has(episode.feed_id)) continue;
      byFeed.set(episode.feed_id, episode);
    }
    return Array.from(byFeed.values());
  }

  _renderLatest() {
    const e = (v) => this._escape(v);
    const feeds = this._library ? this._library.feeds || [] : [];
    const latestEpisodes = this._latestEpisodesForMode();
    const title = this._config.title || "Latest podcast";
    const selectedFeed = this._selectedFeed === "all" ? "All feeds" : ((this._feedFor(this._selectedFeed) || {}).title || "Selected feed");
    const countLabel = this._selectedFeed === "all"
      ? `${latestEpisodes.length} latest ${latestEpisodes.length === 1 ? "episode" : "episodes"}`
      : (latestEpisodes.length ? selectedFeed : "No episodes");
    this.shadowRoot.innerHTML = `
      <style>${this._baseStyles(`.feed-strip { margin-top: 4px; margin-bottom: 10px; } .latest-list { display: grid; gap: 8px; min-width: 0; } .latest-main { padding: 10px 8px; border: 1px solid color-mix(in srgb, var(--divider-color) 70%, transparent); background: color-mix(in srgb, var(--secondary-background-color) 28%, transparent); } .latest-main .row-title { font-size: .92rem; }`)}</style>
      <ha-card>
        <div class="wrap">
          <div class="top">
            <div>
              <div class="title">${e(title)}</div>
              <div class="counts">${e(countLabel)}</div>
            </div>
            <button class="secondary" id="refresh" ${this._loading ? "disabled" : ""}>Refresh</button>
          </div>
          ${this._error ? `<div class="notice error">${e(this._error)}</div>` : ""}
          ${feeds.length ? this._renderFeedStrip(feeds) : ""}
          ${latestEpisodes.length ? `
            <div class="latest-list">
              ${latestEpisodes.map((latest) => `
                <div class="latest-main episode" data-episode-id="${e(latest.episode_id)}" role="button" tabindex="0" title="Play episode">
                  <div class="art">${this._artFor(latest) ? `<img src="${e(this._artFor(latest))}" alt="" />` : `<div class="fallback">🎙️</div>`}</div>
                  <div>
                    <div class="row-title">${e(latest.title || "Untitled episode")}</div>
                    <div class="row-meta"><span class="feed-pill">${e(this._feedTitleFor(latest))}</span><span>${e(this._formatDate(latest.published))}</span><span>${e(this._formatTime(latest.duration_seconds))}</span></div>
                  </div>
                </div>
              `).join("")}
            </div>
          ` : `<div class="empty">No episodes available for ${e(selectedFeed)}.</div>`}
        </div>
      </ha-card>
    `;
    this._bindEvents();
  }

  _renderEmpty(lib) {
    if (!lib) return "";
    return `<div class="empty">No podcasts added yet.<br />Paste a podcast RSS feed URL above to get started.</div>`;
  }

  _renderArt(item, className = "art") {
    const e = (v) => this._escape(v);
    const art = this._artFor(item);
    return `<div class="${className}">${art ? `<img src="${e(art)}" alt="" />` : `<div class="fallback">🎙️</div>`}</div>`;
  }

  _renderPlayer(ep) {
    const e = (v) => this._escape(v);
    const speed = this._currentBrowserSpeed(ep);
    const timing = this._displayPositionDuration();
    const position = Number(timing.position || 0);
    const duration = Number(timing.duration || ep.duration_seconds || 0);
    const selectedTarget = this._selectedControlTarget();
    const externalSelected = Boolean(selectedTarget);
    const selectedExternalName = externalSelected ? this._limitedExternalName() : "Browser";
    const showProgress = this._targetCanShowProgress(selectedTarget);
    const canSeek = this._targetCanSeek(selectedTarget);
    const canSpeed = this._selectedCanSpeed();
    const playing = this._isActuallyPlaying();
    const playLabel = this._playPauseLabelForSelected(playing);
    const outputLabel = this._isSpeakerOutput() ? `Playing on ${this._speakerTargetName()}` : (externalSelected ? `Ready for ${selectedExternalName}` : "Browser playback");
    return `
      <div class="player">
        ${this._renderArt(ep, "art")}
        <div>
          <div class="feed-title"><span class="feed-pill">${e(this._feedTitleFor(ep))}</span> <span class="output-label">${e(outputLabel)}</span></div>
          <div class="ep-title">${e(ep.title || "Untitled episode")}</div>
          <div class="meta"><span id="player-date">${e(this._formatDate(ep.published))}</span> · <span id="time-duration-meta">${e(this._formatTime(duration || ep.duration_seconds))}</span> · <span id="player-status">${e(ep.played ? "Played" : "Unplayed")}</span>${this._useProxyForCurrent ? " · Proxy" : ""}</div>
          ${ep.description ? `<div class="desc">${e(this._stripHtml(ep.description))}</div>` : ""}
          <div class="controls">
            ${this._renderOutputSelect()}
            ${canSpeed ? `<label class="speed-control" title="Playback speed"><span>Speed</span><select id="speed" aria-label="Playback speed">${PodcastPlayerCard._speedOptions().map((s) => `<option value="${s}" ${Number(speed) === s ? "selected" : ""}>${s}x</option>`).join("")}</select></label>` : ""}
            <button class="icon" id="playpause">${e(playLabel)}</button>
            ${canSeek ? `<button class="secondary icon" id="back">-15s</button><button class="secondary icon" id="forward">+30s</button>` : ""}
            ${this._isSpeakerOutput() || externalSelected ? `<button class="secondary" id="stop">Stop</button>` : ""}
            <button class="secondary" id="mark-played">${ep.played ? "Mark unplayed" : "Mark played"}</button>
          </div>
          ${showProgress ? `<div class="progress-wrap"><div class="bar" id="progress-bar"><div></div></div><div class="time"><span id="time-current">${e(this._formatTime(position))}</span><span id="time-duration">${e(this._formatTime(duration))}</span></div></div>` : ""}
        </div>
      </div>
    `;
  }

  _renderFeedStrip(feeds) {
    const e = (v) => this._escape(v);
    if (!feeds.length) return "";
    const allSelected = this._selectedFeed === "all";
    const allChip = `<button class="feed-chip ${allSelected ? "selected" : ""}" data-feed-chip="all"><div class="avatar"><div class="fallback">∞</div></div><div class="feed-chip-title">All feeds</div></button>`;
    const chips = feeds.map((feed) => {
      const art = feed.artwork_url ? `<img src="${e(feed.artwork_url)}" alt="" />` : `<div class="fallback">🎙️</div>`;
      const warn = feed.status === "failed" ? " ⚠" : "";
      return `<button class="feed-chip ${this._selectedFeed === feed.feed_id ? "selected" : ""}" data-feed-chip="${e(feed.feed_id)}"><div class="avatar">${art}</div><div class="feed-chip-title">${e(feed.title || feed.feed_id)}${warn}</div></button>`;
    }).join("");
    return `<div class="feed-strip">${allChip}${chips}</div>`;
  }

  _renderEpisodeRow(item) {
    const e = (v) => this._escape(v);
    const pos = Number(item.position || 0);
    const dur = Number(item.duration_seconds || 0);
    const pct = dur > 0 ? Math.min(100, (pos / dur) * 100) : 0;
    const selected = item.episode_id === this._selectedEpisodeId ? " selected" : "";
    const feedTitle = this._feedTitleFor(item);
    const status = this._statusFor(item);
    return `
      <div class="episode${selected}" data-episode-id="${e(item.episode_id)}" role="button" tabindex="0" title="Play episode">
        ${this._renderArt(item, "avatar")}
        <div>
          <div class="row-title">${e(item.title || "Untitled episode")}</div>
          <div class="row-meta"><span class="feed-pill">${e(feedTitle)}</span><span>${e(this._formatDate(item.published))}</span><span>${e(this._formatTime(item.duration_seconds))}</span></div>
          ${pct > 0 ? `<div class="tiny-progress" data-progress-id="${e(item.episode_id)}"><div style="width:${pct}%"></div></div>` : `<div class="tiny-progress" data-progress-id="${e(item.episode_id)}" style="display:none"><div></div></div>`}
        </div>
        <div class="badge" data-status-id="${e(item.episode_id)}">${e(status)}</div>
      </div>
    `;
  }

  _updateDynamicUi(force = false) {
    if (!this._currentEpisode) return;
    const timing = this._displayPositionDuration();
    const position = Number(timing.position || 0);
    const duration = Number(timing.duration || this._currentEpisode.duration_seconds || 0);
    const playing = this._isActuallyPlaying();
    const percent = duration > 0 ? Math.min(100, Math.max(0, (position / duration) * 100)) : 0;
    const fill = this.shadowRoot.querySelector("#progress-bar > div");
    if (fill) fill.style.width = `${percent}%`;
    const current = this.shadowRoot.querySelector("#time-current");
    if (current) current.textContent = this._formatTime(position);
    const total = this.shadowRoot.querySelector("#time-duration");
    if (total) total.textContent = this._formatTime(duration);
    const metaTotal = this.shadowRoot.querySelector("#time-duration-meta");
    if (metaTotal) metaTotal.textContent = this._formatTime(duration);
    const selectedProgress = this.shadowRoot.querySelector(`[data-progress-id="${CSS.escape(this._currentEpisode.episode_id)}"]`);
    if (selectedProgress) {
      selectedProgress.style.display = percent > 0 ? "block" : "none";
      const pfill = selectedProgress.querySelector("div");
      if (pfill) pfill.style.width = `${percent}%`;
    }
    const selectedStatus = this.shadowRoot.querySelector(`[data-status-id="${CSS.escape(this._currentEpisode.episode_id)}"]`);
    if (selectedStatus) selectedStatus.textContent = this._statusFor(this._currentEpisode);
    const selectedTarget = this._selectedSpeakerTarget();
    const externalSelected = Boolean(selectedTarget);
    const playpause = this.shadowRoot.querySelector("#playpause");
    if (playpause) playpause.textContent = this._playPauseLabelForSelected(playing);
    const compactStatus = this.shadowRoot.querySelector(".compact-status");
    if (compactStatus) {
      const output = this._isSpeakerOutput() ? `Playing on ${this._speakerTargetName()}` : (externalSelected ? `Ready for ${this._limitedExternalName()}` : "Browser");
      if (this._targetCanShowProgress(selectedTarget)) {
        compactStatus.innerHTML = `<span>${this._formatTime(position)}</span><span>·</span><span>${this._formatTime(duration)}</span><span>·</span><span>${playing ? "playing" : "paused"}</span><span>·</span><span>${this._escape(output)}</span>`;
      } else {
        compactStatus.innerHTML = `<span>${this._escape(output)}</span>`;
      }
    }
    if (force) {
      const speed = this.shadowRoot.querySelector("#speed");
      if (speed) speed.value = String(this._currentBrowserSpeed(this._currentEpisode));
    }
  }

  _bindEvents() {
    const root = this.shadowRoot;
    root.querySelector("#add-feed")?.addEventListener("click", () => this._addFeed());
    root.querySelector("#rss-url")?.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") this._addFeed();
    });
    root.querySelector("#refresh")?.addEventListener("click", () => this._refresh());
    root.querySelector("#reload-library")?.addEventListener("click", () => {
      this._library = null;
      this._loadLibrary();
    });
    root.querySelector("#remove-feed")?.addEventListener("click", () => this._removeSelectedFeed());
    root.querySelector("#feed-select")?.addEventListener("change", (ev) => this._changeFeed(ev.target.value));
    root.querySelector("#filter-select")?.addEventListener("change", (ev) => this._changeFilter(ev.target.value));
    root.querySelectorAll("[data-feed-chip]").forEach((chip) => chip.addEventListener("click", () => this._changeFeed(chip.dataset.feedChip)));
    root.querySelector("#output-select")?.addEventListener("change", (ev) => this._changeOutputTarget(ev.target.value));
    root.querySelector("#playpause")?.addEventListener("click", () => this._togglePlay());
    root.querySelector("#stop")?.addEventListener("click", () => this._stop());
    root.querySelector("#back")?.addEventListener("click", () => this._jump(-15));
    root.querySelector("#forward")?.addEventListener("click", () => this._jump(30));
    root.querySelector("#speed")?.addEventListener("pointerdown", () => { this._deferRenderUntil = Date.now() + 3500; });
    root.querySelector("#speed")?.addEventListener("focus", () => { this._deferRenderUntil = Date.now() + 3500; });
    root.querySelector("#speed")?.addEventListener("blur", () => { this._deferRenderUntil = 0; });
    root.querySelector("#speed")?.addEventListener("change", (ev) => this._setSpeed(ev.target.value));
    root.querySelector("#mark-played")?.addEventListener("click", () => this._markPlayed(!(this._currentEpisode && this._currentEpisode.played)));
    root.querySelector("#progress-bar")?.addEventListener("click", async (ev) => {
      const rect = ev.currentTarget.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
      const timing = this._displayPositionDuration();
      const duration = Number(timing.duration || 0);
      if (duration > 0) await this._jump(ratio * duration - (timing.position || 0));
    });
    root.querySelectorAll(".episode").forEach((row) => {
      const playRow = () => this._selectEpisode(row.dataset.episodeId, true);
      row.addEventListener("click", playRow);
      row.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          playRow();
        }
      });
    });
  }
}

if (!customElements.get("podcast-player-card")) {
  customElements.define("podcast-player-card", PodcastPlayerCard);
}

class PodcastPlayerModeCard extends PodcastPlayerCard {
  static mode = "full";

  static getStubConfig() {
    const config = PodcastPlayerCard._stubConfig(this.mode);
    config.type = `custom:${this.cardType}`;
    return config;
  }

  setConfig(config) {
    super.setConfig({ ...PodcastPlayerCard._stubConfig(this.constructor.mode), ...config, mode: this.constructor.mode });
  }
}

class PodcastPlayerFullCard extends PodcastPlayerModeCard {}
PodcastPlayerFullCard.mode = "full";
PodcastPlayerFullCard.cardType = "podcast-player-full-card";

class PodcastPlayerOnlyCard extends PodcastPlayerModeCard {}
PodcastPlayerOnlyCard.mode = "compact";
PodcastPlayerOnlyCard.cardType = "podcast-player-player-card";

class PodcastPlayerLatestCard extends PodcastPlayerModeCard {}
PodcastPlayerLatestCard.mode = "latest";
PodcastPlayerLatestCard.cardType = "podcast-player-latest-card";

class PodcastPlayerCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = PodcastPlayerCard._stubConfig("full");
    this._hass = null;
  }

  setConfig(config) {
    this._config = { ...PodcastPlayerCard._stubConfig("full"), ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this.shadowRoot || !this._hass) return;
    this.shadowRoot.innerHTML = "";
    const form = document.createElement("ha-form");
    form.hass = this._hass;
    form.data = this._config;
    form.schema = [
      { name: "entity", selector: { entity: { domain: "media_player" } } },
      {
        name: "mode",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              { value: "full", label: "Podcast Player + latest episodes" },
              { value: "compact", label: "Podcast Player" },
              { value: "latest", label: "Latest episodes only" },
            ],
          },
        },
      },
      { name: "limit", selector: { number: { min: 1, max: 500, mode: "box" } } },
      { name: "default_feed", selector: { text: {} } },
      { name: "default_filter", selector: { select: { options: ["all", "unplayed", "played", "in_progress"] } } },
      { name: "remember_state", selector: { boolean: {} } },
    ];
    form.computeLabel = (schema) => ({
      entity: "Podcast Player entity",
      mode: "Card mode",
      limit: "Episode limit",
      default_feed: "Default feed ID",
      default_filter: "Default filter",
      remember_state: "Remember card state",
    }[schema.name] || schema.name);
    form.addEventListener("value-changed", (ev) => {
      this._config = ev.detail.value;
      this.dispatchEvent(new CustomEvent("config-changed", {
        detail: { config: this._config },
        bubbles: true,
        composed: true,
      }));
    });
    this.shadowRoot.appendChild(form);
  }
}

[
  ["podcast-player-full-card", PodcastPlayerFullCard],
  ["podcast-player-player-card", PodcastPlayerOnlyCard],
  ["podcast-player-latest-card", PodcastPlayerLatestCard],
  ["podcast-player-card-editor", PodcastPlayerCardEditor],
].forEach(([type, klass]) => {
  if (!customElements.get(type)) customElements.define(type, klass);
});

window.customCards = window.customCards || [];
[
  {
    type: "podcast-player-full-card",
    name: "Podcast Player + Latest Episodes",
    description: "Full podcast player with feed browsing and latest episodes.",
  },
  {
    type: "podcast-player-player-card",
    name: "Podcast Player",
    description: "Compact podcast player controls.",
  },
  {
    type: "podcast-player-latest-card",
    name: "Latest Podcast Episodes",
    description: "Latest episodes only.",
  },
].forEach((card) => {
  if (!window.customCards.some((item) => item.type === card.type)) {
    window.customCards.push(card);
  }
});
