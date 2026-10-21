# Plugins

No plugin ships with muse-discord right now; this folder is where one
goes. A plugin is a folder `plugins/<name>/` with two files:

- `plugin.yaml`: its id, the hooks it uses and its settings.
- `plugin.py`: a `register(api)` function returning `{hook name:
  function}`.

```yaml
# plugins/example/plugin.yaml
id: example
hooks: [gate_message]
safe_in_new_servers: true   # false: off in servers until one turns it on
settings:                   # values live at plugins.example.<key>
  enabled:
    type: on-off
    default: off
    scopes: [everywhere, dms, all_servers, server, channel]
```

```python
# plugins/example/plugin.py
def register(api):
    def gate_message(ctx, msg, guild_id):
        api.log("example: saw", msg["id"])
        return None  # or a dict merged into the queue item

    return {"gate_message": gate_message}
```

The monitor loads a plugin once some scope turns it on
(`plugins.<id>.enabled: on` in `config.yaml`, or by DMing the bot "turn
on example"; `/settings` lists installed plugins but has no controls for
them, and server admins can turn one off there), and
runs its hooks only where it is on. The hooks, and what `ctx` carries,
are listed in `monitor.py` (search for "Plugins. A plugin is a folder");
the setting types are those of `muse_settings.load_manifests`. A
failing plugin is logged and skipped, never fatal.
