# Podcast Player

Podcast Player is a Home Assistant custom integration for listening to RSS podcast feeds from Home Assistant. It stores a local podcast library, tracks listening progress, exposes podcast entities for dashboards and automations, and can send podcast audio to supported Home Assistant `media_player` targets.

The integration works with public podcast RSS feeds. It does not require a podcast account or a cloud API account.

## Supported Functionality

Podcast Player provides:

- UI setup from **Settings > Devices & services**.
- RSS feed validation during setup and when adding feeds.
- Local storage for feeds, episodes, listening progress, and playback state.
- Periodic RSS refresh with a configurable interval.
- Media Browser support through Home Assistant media sources.
- A state-only `media_player.podcast_player` entity for current podcast metadata.
- Feed sensors that can be selected as service action targets.
- Sensors for library, current episode, progress, speed, output, and latest episode state.
- Binary sensors for active playback and unplayed episode availability.
- Button entities for refresh, play latest, play next unplayed, and mark current played.
- Service actions for feed management, playback, progress, and external media-player control.
- Optional signed Home Assistant proxy URLs for media players that cannot play the original podcast URL directly.
- Optional enhanced DLNA controls for target media players that expose compatible DLNA transport behavior.

## Prerequisites

- Home Assistant with custom integrations enabled.
- Internet access from Home Assistant to the podcast RSS feed URLs.
- At least one podcast RSS feed URL.
- A supported Home Assistant `media_player` entity if you want speaker playback.

For browser playback through the companion dashboard card, install the card file described in the card section below.

## Installation

### HACS

1. Open HACS.
2. Go to **Integrations**.
3. Open the menu and choose **Custom repositories**.
4. Add this repository URL:

   ```text
   https://github.com/armilancode/home-assistant-podcast-player
   ```

5. Select **Integration** as the repository category.
6. Install **Podcast Player**.
7. Restart Home Assistant.
8. Go to **Settings > Devices & services > Add integration**.
9. Search for **Podcast Player** and complete the setup form.

### Manual

1. Copy `custom_components/podcast_player` into the `custom_components` directory of your Home Assistant configuration.
2. Restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration**.
4. Search for **Podcast Player** and complete the setup form.

## Setup

The setup form creates one Podcast Player config entry. Only one config entry is supported.

Setup parameters:

| Parameter | Required | Default | Description |
| --- | --- | --- | --- |
| First podcast RSS URL | No | Empty | Optional first feed to validate and add during setup. The URL must start with `http://` or `https://` and must be a playable podcast RSS feed. |
| Refresh interval | No | `120` minutes | How often Podcast Player refreshes RSS feeds. Accepted range: `15` to `1440` minutes. |
| Maximum cached episodes per feed | No | `100` | Number of recent episodes to keep cached for each feed. Accepted range: `10` to `1000`. Listening progress is preserved when older cached episodes are trimmed. |
| Default playback speed | No | `1.0` | Default playback speed. Accepted values: `0.75`, `1.0`, `1.25`, `1.5`, `1.75`, `2.0`. |
| Played threshold | No | `0.95` | Fraction of an episode that must be listened to before it is treated as played. Accepted range: `0.5` to `1.0`. |
| Media player URL preference | No | Direct podcast URL | Use the original podcast audio URL or a signed Home Assistant proxy URL when sending audio to a target media player. |
| Enhanced DLNA controls | No | On | Enables additional DLNA stop, seek, resume, and progress behavior where a target supports it. |

If the optional first feed cannot be reached or parsed, setup stays on the form and shows an error instead of creating a broken entry.

## Options

After setup, open **Settings > Devices & services > Podcast Player > Configure** to update options.

The options form includes the setup parameters above and adds:

| Parameter | Required | Description |
| --- | --- | --- |
| Add podcast RSS URL | No | Adds and immediately refreshes a new podcast feed. |
| Remove podcast feed | No | Removes a selected feed. |
| Keep listening history when removing | No | Keeps episode progress after removing the feed. |

## Companion Card

The backend integration works without the companion card. The card adds a richer dashboard experience for browsing, browser playback, and progress updates.

The card is bundled with the integration and registered through Home Assistant's frontend module API. HACS and manual integration updates therefore install the backend and matching card together. Do not copy a JavaScript file into `www`, and do not add a dashboard resource.

After installing or updating Podcast Player:

1. Restart Home Assistant.
2. Reload the dashboard once on each browser or Companion app.
3. Add a manual dashboard card using:

   ```yaml
   type: custom:podcast-player-card
   ```

If the browser still has an older card cached, the card displays both version numbers and asks for a restart and dashboard reload.

### Migration from 0.3.0-alpha.2 and older

When Lovelace resources are managed through the UI, Podcast Player removes its exact legacy `/local/podcast-player-card/...` resource after the bundled module is active. Other resources and dashboards are not changed.

YAML-managed Lovelace resources are never edited automatically. Remove the old Podcast Player card resource from YAML after upgrading; Home Assistant logs a specific reminder when it detects one.

The card uses its own play, pause, seek, and speed controls. System media
controls are enabled by default in regular browsers. They are disabled by
default in the Home Assistant Android Companion WebView because repeated
system media-notification refreshes can trigger notification haptics on some
phones. Android users can explicitly opt back in if they prefer lock-screen
media controls:

```yaml
type: custom:podcast-player-card
system_media_controls: true
```

Set `system_media_controls: false` to keep system media controls disabled in
any browser.

### Browser and App Playback Ownership

Browser playback includes both ordinary web browsers and the Home Assistant Companion app's embedded browser. Podcast Player keeps one active browser playback owner at a time:

- While one device is playing, other cards show the active output as **Home Assistant app** or **Web browser** and offer **Take over**.
- **Take over** transfers playback at the shared position and stops the previous connected player.
- Pausing saves the shared position and releases device ownership. All cards then show **Play**; the next device to press it becomes the active output.
- Seek, speed, pause, and progress updates from a stale client are rejected after another device takes over.
- The **Browser** output-selector option names the local browser playback type. The status panel identifies the actual active client while audio is playing.

This model prevents two connected cards from independently controlling the same browser session while avoiding stale ownership after a browser tab or Companion app is restarted.

## Media Browser

Podcast Player exposes feeds and episodes through Home Assistant Media Browser. Media Browser playback is handled by the selected Home Assistant `media_player` target. Native progress, pause, seek, and stop behavior depends on that target integration.

Use Podcast Player service actions when you need more explicit control over episode selection, URL mode, resume position, or external playback behavior.

## Supported Media Player Targets

Podcast Player can send audio to Home Assistant `media_player` entities that support `play_media` with remote HTTP or HTTPS media URLs.

Targets are most likely to work when they can reach either:

- The original podcast audio URL.
- The Home Assistant instance URL when `signed_proxy` mode is used.

Unsupported or limited targets include:

- Media players that do not support `play_media`.
- Media players that cannot reach the podcast host or Home Assistant URL.
- Targets that reject long-lived remote audio streams.
- Targets whose integrations do not expose useful progress, seek, pause, resume, or stop behavior.

For target playback, use `media_player_entity_id` in action data. Do not put the output media player in the action target field unless the action specifically targets media players. Feed-selecting actions use Podcast Player feed sensors as targets.

## Entities

Podcast Player creates one device named **Podcast Player**.

Main entities include:

- `media_player.podcast_player`: state-only podcast metadata entity. It does not play browser audio through Home Assistant's native media-player popup.
- Feed sensors: one sensor per enabled podcast feed. Feed sensors expose `feed_id`, feed metadata, refresh status, episode counts, and latest episode attributes.
- Library sensors: feed count, unplayed count, latest episode, current feed, current episode, position, duration, progress, playback speed, output, and latest episode by feed.
- Binary sensors: current playback state and whether unplayed episodes exist.
- Buttons: refresh feeds, play latest episode, play next unplayed, and mark current played.

Some diagnostic or less common entities are disabled by default and can be enabled from the entity registry.

## Data Updates

Podcast Player refreshes RSS feeds on the configured interval. The default interval is 120 minutes.

During refresh:

- Enabled feeds are fetched from their RSS URLs.
- Feed and episode metadata is parsed and stored locally.
- New episodes are added to the local cache.
- Old cached episodes are trimmed according to **Maximum cached episodes per feed**.
- Listening progress is preserved even when old cached episodes are trimmed.
- Feed failures are stored on the feed sensor attributes and a refresh-failed event is fired.

Refreshing can also be started manually from the **Refresh feeds** button or the `podcast_player.refresh` action.

## Actions

Podcast Player registers the following service actions under the `podcast_player` domain.

Common playback fields:

| Field | Required | Description |
| --- | --- | --- |
| `media_player_entity_id` | No, except where noted | Output Home Assistant media player for external playback. Leave empty for browser/card playback where supported. |
| `url_mode` | No | `direct` sends the original podcast audio URL. `signed_proxy` sends a temporary Home Assistant proxy URL. |
| `media_content_type` | No | `music` or `podcast`. Most targets work best with `music`. |
| `resume_position` | No | Start position in seconds. If omitted, saved progress is used when available. |

Feed selection:

- Actions with a Home Assistant target selector expect Podcast Player feed sensor entities.
- Use `feed_id` or `feed_name` in action data when provided by the action schema.
- Use `media_player_entity_id` for the output speaker.

### Feed Management

| Action | Description | Parameters |
| --- | --- | --- |
| `podcast_player.add_feed` | Add an RSS podcast feed and refresh it immediately. | `rss_url` required. |
| `podcast_player.remove_feed` | Remove a podcast feed. | `feed_id` required, `keep_history` optional. |
| `podcast_player.refresh` | Refresh selected feed targets or all feeds. | Optional feed sensor target. |
| `podcast_player.refresh_feeds` | Alias for refreshing selected feed targets or all feeds. | Optional feed sensor target. |

### Playback

| Action | Description | Parameters |
| --- | --- | --- |
| `podcast_player.play_episode` | Play a specific episode. | `episode_id` required. Supports the common playback fields. |
| `podcast_player.play_current` | Play the current episode, optionally restricted by selected feed targets. | Optional feed sensor target. Supports the common playback fields. |
| `podcast_player.play_latest` | Play the newest episode from selected feeds, or all feeds. | Optional feed sensor target. Supports the common playback fields. |
| `podcast_player.play_next_unplayed` | Play the newest unplayed episode from selected feeds, or all feeds. | Optional feed sensor target. Supports the common playback fields. |
| `podcast_player.play_on_media_player` | Send current, latest, next-unplayed, or selected podcast audio to a media player. | `media_player_entity_id` required. Optional `episode_id`, `episode_mode`, `url_mode`, `media_content_type`, and `resume_position`. |

### Playback Control and Progress

| Action | Description | Parameters |
| --- | --- | --- |
| `podcast_player.resume` | Resume the current podcast state or active external playback when supported. | None. |
| `podcast_player.pause` | Pause the current podcast state or active external playback when supported. | None. |
| `podcast_player.stop` | Stop the active Podcast Player output. | Optional `force`. |
| `podcast_player.stop_output` | Stop the current output, or a specific output media player. | Optional `media_player_entity_id`, `force`. |
| `podcast_player.stop_media_player` | Stop a media player used for podcast output. | Media player target, optional `force`. |
| `podcast_player.seek` | Store a new playback position and seek the active external media player when supported. | `episode_id` and `position` required. |
| `podcast_player.save_progress` | Save playback progress from the card or an automation. | `episode_id` and `position` required. Optional `duration`, `playing`, `speed`. |
| `podcast_player.set_speed` | Set the default playback speed and optionally update one episode. | `speed` required. Optional `episode_id`. |

### Played State

| Action | Description | Parameters |
| --- | --- | --- |
| `podcast_player.mark_played` | Mark one episode as played. | `episode_id` required. |
| `podcast_player.mark_unplayed` | Mark one episode as unplayed. | `episode_id` required. |
| `podcast_player.mark_current_played` | Mark the current episode as played or unplayed. | Optional `played`. |
| `podcast_player.mark_feed_played` | Mark active episodes from selected feed targets as played or unplayed. | Feed sensor target required, optional `played`. |

## Examples

Play the next unplayed episode from a feed on a kitchen speaker:

```yaml
action: podcast_player.play_next_unplayed
target:
  entity_id: sensor.podcast_player_feed_example_podcast
data:
  media_player_entity_id: media_player.kitchen_speaker
  url_mode: signed_proxy
```

Refresh all feeds:

```yaml
action: podcast_player.refresh
```

Mark all episodes from a feed as played:

```yaml
action: podcast_player.mark_feed_played
target:
  entity_id: sensor.podcast_player_feed_example_podcast
data:
  played: true
```

Stop the current external output:

```yaml
action: podcast_player.stop_output
data:
  force: true
```

## Known Limitations

- Podcast Player supports RSS podcast feeds with playable audio enclosures. Feeds without audio enclosures are rejected.
- It is not a podcast search directory. Add feeds by RSS URL.
- The built-in `media_player.podcast_player` entity is a metadata/status entity, not a native browser audio output.
- Browser playback requires the companion dashboard card.
- A fully suspended or offline browser cannot receive an immediate takeover stop command. It relinquishes stale ownership when it reconnects; normal connected and background Companion-app playback uses the single-owner behavior described above.
- External playback depends on the selected Home Assistant media player integration and network reachability.
- Native progress, seek, pause, resume, and stop support varies by target media player.
- `signed_proxy` URLs require the target media player to reach your Home Assistant instance.

## Troubleshooting

### The feed will not add

- Confirm the URL starts with `http://` or `https://`.
- Open the URL in a browser and confirm it returns RSS/XML.
- Confirm the feed contains episode audio enclosures.
- Check that Home Assistant can reach the URL from its network.

### The media player does not play audio

- Try `url_mode: signed_proxy` if the player cannot reach the original podcast host.
- Try `media_content_type: music` if the target rejects `podcast`.
- Confirm the target media player supports `play_media`.
- Confirm the target can reach your Home Assistant URL when using `signed_proxy`.

### Pause, seek, resume, or stop does not work as expected

- Check whether the target media player integration exposes those controls.
- Keep **Enhanced DLNA controls** enabled for compatible DLNA targets.
- Use `podcast_player.stop_output` with `force: true` if the integration needs to stop a stale external session.

### Every browser shows Paused elsewhere after an update

Version `0.3.0-alpha.2` and newer release browser ownership whenever playback is paused and repair older paused sessions during startup. Restart Home Assistant and reload each client once after updating.

### The card reports a version mismatch

The browser is running a different card version from the backend integration. Restart Home Assistant, then reload the dashboard. With `0.3.0-alpha.3` and newer, HACS installs both pieces together and no manual dashboard resource is required.

### Feed targets and output media players are mixed up

Podcast Player feed actions use feed sensors as the action target. The speaker goes in `data.media_player_entity_id`.

## Use Cases

- Keep a local Home Assistant view of podcast feeds and unplayed episode counts.
- Add podcast feed sensors to dashboards and automations.
- Start the latest or next unplayed episode from a selected feed on a kitchen, office, or bedroom speaker.
- Use `signed_proxy` playback for media players that can reach Home Assistant but cannot reach the original podcast host.
- Track listening progress from the companion card and use automations to mark episodes played.
- Refresh feeds on demand before a routine or announcement automation.

## Removing the Integration

1. Go to **Settings > Devices & services**.
2. Open **Podcast Player**.
3. Select the menu and choose **Delete**.
4. Confirm removal.

After removal:

- Podcast Player entities are removed from Home Assistant.
- Stored feed, episode, and progress data is no longer used by the integration.
- If you installed through HACS, remove Podcast Player from HACS if you also want to remove the custom integration files.
- If you installed manually, remove `custom_components/podcast_player` from your Home Assistant configuration.
- If you upgraded from `0.3.0-alpha.2` or older and still have a manually copied `www/podcast-player-card` directory, it can be removed after the bundled card is working.

## Development

Development notes are in `docs/development.md`. Architecture notes are in `docs/architecture.md`.

Do not commit Home Assistant runtime data such as `.storage`, logs, backups, databases, or secrets.
