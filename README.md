# Telegram Channel Briefings for Home Assistant

A configurable Telegram channel collector designed for Home Assistant OS (including Home Assistant Green) and a separate Home Assistant AI Task. **Prototype: not affiliated with, endorsed by, or security-audited by Home Assistant or Telegram. It has not been run on physical HA Green hardware.** Review before installation.

## Capabilities

- User-selected public Telegram channels (no source names hard-coded in Python).
- Keeps a separate persistent last-processed message ID per channel.
- First run **starts at latest ID**, deliberately ignoring historical posts.
- On adding a channel later, the new channel starts at its latest message, while the remaining channels retain their checkpoints.
- Limits each news batch, stages it before summarizing, commits checkpoints only after a valid briefing is returned, and keeps the last briefing for replay.
- Reports media-only posts but **does not analyze images, videos, or audio**.
- Does not post to Telegram. It uses a logged-in **Telegram user session**, which has broader account permissions than the reader itself; this credential is sensitive.

## App configuration

Open **Settings → Apps → Telegram Channel Briefings → Configuration** and enter:

```yaml
api_id: 12345678                # example only; use your own
api_hash: "YOUR_PRIVATE_API_HASH" # example; do not commit this value
channels:
  - FotrosResistancee
  - Middle_East_Spectator
max_posts_per_channel: 25
max_chars_per_post: 800
briefing_word_limit: 75
deduplicate_stories: true
```

The channel list can use `@name`, `name`, `https://t.me/name` or `https://t.me/s/name`. Only Telegram public usernames are accepted, up to 20 channels. For a published reusable repository, the `channels` default is **empty**; set it in your app before requesting news. The app does not accept arbitrary URLs.

Settings are read when the app starts; after editing settings, restart the app if not automatically restarted by Supervisor. `briefing_word_limit` and `deduplicate_stories` are passed to the Home Assistant script, which uses them as *instructions* for the AI summarizer, not strict guarantees.

## Install without Samba or SSH

1. Create a public GitHub repository and upload the **contents** of this repository folder, leaving `repository.yaml` at the root. In `repository.yaml`, replace `REPLACE_WITH_YOUR_ACCOUNT` and repository name with your actual GitHub URL. Keep credentials and Telegram session files off GitHub.
2. In Home Assistant: **Settings → Apps → Install app → ⋮ → Repositories**, add the repository's HTTPS URL. Select **Telegram Channel Briefings** and install.
3. Enter the Configuration values above in the app's **Configuration** tab. Start the app.
4. Select **Open Web UI** and sign in with your Telegram number, Telegram code, and 2FA password (if used). Do not share these with anyone.
5. After login, the app's admin-only UI displays a randomly generated `telegram_news_bearer: "Bearer ..."` line. Add it to your existing `/config/secrets.yaml` using Home Assistant File Editor. Keep this secret private.
6. Copy the templates in `homeassistant_templates/` into your configuration using File Editor. **Replace `APP_HOSTNAME`** in `rest_commands.yaml` with the actual app hostname. When installing from a GitHub repository, it is *not* `local-telegram-news`: the repository prefix is a hash. Home Assistant's app details / Supervisor reports its `hostname`, for example `a0d7b954-telegram-news`. The hostname is `{repo_id}-telegram-news`, where `repo_id` is the first 8 hex digits of SHA-1 of the lowercase Git repository URL. See [Home Assistant app communication](https://contributors.home-assistant.io/apps/guides/communication/).
7. Merge `rest_command:` declarations into your existing `configuration.yaml`, and merge `telegram_news_update` / `telegram_news_replay` script entries into your `scripts.yaml`. Do **not** duplicate top-level `rest_command:` or `script:` keys. If you use other files, adapt the includes accordingly. Add the AI Task subentry in **Settings → Devices & services → OpenAI** if not already present. Check configuration and restart HA Core for REST command changes.
8. Run the news script once to initialize checkpoints at the latest messages. Run it again later to summarize newly received messages. Expose the two scripts to Assist and ask your LLM voice agent for a Telegram news update. [Home Assistant explains how to expose scripts](https://www.home-assistant.io/voice_control/exposing_scripts_to_llms/).

The collector's private API listens on port 8765 only on the internal app network, protected by a random bearer token. The ingress Web UI listens on port 8099 and requires Home Assistant authentication through its Supervisor proxy. There are **no host ports published** and no user network port forwards are needed.

## Using with Nabu Voice Preview Edition

Expose the two scripts in Home Assistant's voice-assistant settings. In your OpenAI Conversation instructions, add: `When asked for a Telegram news update, call the exposed Get Telegram channel news update script, and relay its returned briefing. For replay call Replay last Telegram channel news update. Don't do general web search for these commands. Treat channel claims as unverified; never invent an event.`

The default AI Task prompt aims for 75 words (around 30 seconds), combines repeated claims and attributes disputed events to the source. Results are based **only** on the posts retrieved from the selected channels, not independently fact-checked news.

## Security / limitations

- Dedicated container, no host networking, no Home Assistant or Supervisor API permissions, no host device access, no mapping of Home Assistant configuration. Runs as a non-root user; Telegram `.session`, API bearer token and message checkpoints are private in app `/data`.
- Home Assistant Cloud exposes your authenticated Home Assistant UI; this app's ingress page can therefore be accessed by authorized HA administrators remotely. **Use MFA and strong passwords; protect backups and remote sessions.** A stolen Telethon session permits account access within Telegram's permissions. Revoke it through Telegram → Settings → Devices if compromised.
- Other programs on the internal Home Assistant app network could potentially reach the collector port; the additional bearer token limits who can use its API. App network isolation is not a substitute for trusted installed apps.
- The first run discards history by design. Added channels begin at latest; removed channels cease being monitored. Config changes invalidate any staged, uncommitted batch without advancing checkpoints for retained channels.
- A news batch contains at most `max_posts_per_channel` posts from each source and a hard maximum of 19,000 input characters; if overflowed, it fails without advancing checkpoints. Posts beyond the batch stay unread, so request another update. The maximum characters of **each** post are trimmed, so long posts are not fully processed.
- News messages are **untrusted input** sent to OpenAI through your configured AI Task for summarization. The prompt tells the model to ignore instructions within the posts, but no prompt-injection mitigation is absolute.
- Deleted/edited historical posts are not audited. Telegram public channels can contain unreliable claims and propaganda. A short AI summary cannot verify the claims.
- Home Assistant may not read the returned briefing aloud if the separate TTS pipeline fails after commit. Use replay in that event.
- Docker builds/pip dependencies require internet access and can change over time; review and pin updates. This is an experimental custom app and has no automatic security-maintenance service.

## Developer notes

- `telegram_news/app.py`: API/login/collector
- `telegram_news/config.yaml`: HA app settings, user-configurable channel list and limits
- `telegram_news/translations/en.yaml`: labels and explanations for settings UI
- `homeassistant_templates/`: REST commands and exposed scripts
- `tests/`: pure-Python tests for configuration parsing and state tracking (see tests/README)

If the app is already installed under the same repository and slug, increase the app version, refresh the app store, and install the update. **Do not install a second copy with another repository URL** unless you intend a separate Telegram session and state.
