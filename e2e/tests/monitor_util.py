"""Import a copy of the repo's monitor.py offline, under a given config.

monitor.py (and muse_settings.py next to it) are copied into a temp dir
and imported from there with MUSE_DISCORD_CONFIG pointing at a
config.yaml written from a dict, so every file the monitor might write
lands in the temp dir. Import does no network I/O.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SELF = "200000000000000009"        # the monitor's own bot id


def load_monitor_with(tmp, config, name="monitor_under_test"):
    """Import monitor.py from tmp with config (a dict) as config.yaml.
    Returns (module, stdout printed at import). The module's bot id is
    SELF."""
    shutil.copy(os.path.join(REPO, "monitor.py"), tmp)
    shutil.copy(os.path.join(REPO, "muse_settings.py"), tmp)
    plugins = os.path.join(REPO, "plugins")
    if os.path.isdir(plugins) and not os.path.exists(
            os.path.join(tmp, "plugins")):
        shutil.copytree(plugins, os.path.join(tmp, "plugins"))
    cfg = os.path.join(tmp, "config.yaml")
    import yaml
    with open(cfg, "w") as f:
        yaml.safe_dump(config, f)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(tmp, "monitor.py"))
    mod = importlib.util.module_from_spec(spec)
    out = io.StringIO()
    old_env = os.environ.get("MUSE_DISCORD_CONFIG")
    old_argv = sys.argv
    os.environ["MUSE_DISCORD_CONFIG"] = cfg
    sys.argv = [sys.argv[0]]
    try:
        with contextlib.redirect_stdout(out):
            spec.loader.exec_module(mod)
    finally:
        sys.argv = old_argv
        if old_env is None:
            os.environ.pop("MUSE_DISCORD_CONFIG", None)
        else:
            os.environ["MUSE_DISCORD_CONFIG"] = old_env
    mod.BOT_ID = mod.BOT_USER_ID = SELF
    mod.BOT_NAME = "muse"
    return mod, out.getvalue()
