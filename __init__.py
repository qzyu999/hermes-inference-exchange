"""Hermes public plugin entry point."""

import logging
import threading
from dataclasses import fields


def register(ctx):
    from .ie_plugin.config import Config
    from .ie_plugin.controller import Controller
    from .ie_plugin.store import Store

    store = None
    controller = None
    config_error = ""
    init_lock = threading.RLock()
    try:
        config = Config(
            **{field.name: ctx.get_config(field.name, field.default) for field in fields(Config)}
        ).validate()
    except (ValueError, OSError, TypeError):
        config_error = "Invalid IE settings. Check local model, paths, URL, and time limits."
        logging.getLogger("inference-exchange").warning(config_error)

    def ensure():
        nonlocal controller, store
        with init_lock:
            if store is None:
                # Capture the active profile before any background thread starts.
                store = Store(ctx.state.data_dir)
            if controller is None and not config_error:
                from agent.secret_scope import get_secret

                token = get_secret("IE_PROVIDER_TOKEN") or ""
                controller = Controller(config, store, token)
            return controller

    def command(raw_args="status"):
        ensure()
        action = (raw_args or "status").strip().lower()
        if action == "status":
            return "IE: error: " + config_error if config_error else "IE: " + store.status()
        if action in {"pause", "resume"}:
            store.write("paused", action == "pause")
            if controller and controller.thread:
                controller.tick()
            return "IE: paused" if action == "pause" else "IE: resumed"
        return "Use /ie status|pause|resume."

    def setup(parser):
        parser.add_argument(
            "action", choices=("status", "pause", "resume"), default="status", nargs="?"
        )

    ctx.register_command("ie", command, "IE provider status and controls", "status|pause|resume")
    ctx.register_cli_command(
        "ie",
        "IE provider status and controls",
        setup,
        lambda args: print(command(args.action)),
    )

    def safe(method):
        def hook(**kwargs):
            try:
                active = ensure()
                if active:
                    return getattr(active, method)(**kwargs)
            except Exception:
                logging.getLogger("inference-exchange").warning(
                    "IE lifecycle failed. Hermes can continue."
                )
                if method == "activity" and controller and controller.process:
                    return {
                        "action": "block",
                        "message": (
                            "IE could not release local hardware. Pause IE and check its process."
                        ),
                    }

        return hook

    def close(**kwargs):
        if controller:
            controller.close()

    ctx.register_hook("on_session_start", safe("session_start"))
    ctx.register_hook("pre_llm_call", safe("activity"))
    ctx.register_hook("post_llm_call", safe("finished"))
    ctx.register_hook("on_session_end", safe("finished"))
    ctx.register_hook("on_session_finalize", close)
    ctx.on_unload(close)
