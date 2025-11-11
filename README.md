# Home Assistant Add-on — Album Art Finder

This add-on subscribes to an MQTT topic that publishes JSON metadata (`state`, `title`, `artist`, `album`), looks up artwork via iTunes Search (no API key), and serves the current cover at `/albumart.jpg` with a status page at `/status`.

## Installation (local add-on)

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** menu (upper-right) → **Repositories** → add your local repo path after you copy this folder into `/addons/albumart_finder` on your HA host.
   - If you have Samba/SSH access, copy this folder to: `/addons/albumart_finder`
3. In **Add-on Store**, find **Album Art Finder** and install it.
4. Configure options (MQTT host/creds, topic, port).
5. Start the add-on.

## Options

```yaml
mqtt:
  host: homeassistant.local
  port: 1883
  username: ""
  password: ""
  topic: media/gym
http_port: 8099
static_dir: /data/albumart
log_level: INFO
```

## Using in Lovelace

Use an `picture` card pointing to:
```
http://<HA-IP>:8099/albumart.jpg?{{ now().timestamp()|int }}
```
Status page:
```
http://<HA-IP>:8099/status
```

## Notes
- Static files are stored under `/data/albumart` so they persist across restarts.
- The add-on runs an s6-managed service and exports configuration as environment variables for the Python app.
