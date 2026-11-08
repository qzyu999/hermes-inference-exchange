import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets

from ie_plugin.config import Config
from ie_plugin.controller import Controller, child_environment
from ie_plugin.runner import DrainMixin, serve
from ie_plugin.store import Store


def test_config_rejects_unsafe_settings(tmp_path):
    for changes in (
        {"idle_minutes": float("nan")},
        {"drain_seconds": 21},
        {"mock": "true"},
        {"coordinator": "ws://example.com/ws/provider"},
        {"coordinator": "wss://example.com/ws/provider?token=secret"},
        {"coordinator": "wss://secret@example.com/ws/provider"},
        {"coordinator": "wss://example.com/ws/provider", "mock": True},
        {"sell_enabled": True, "ie_repo": str(tmp_path), "model": "https://host/model"},
    ):
        with pytest.raises(ValueError):
            replace(Config(), **changes).validate()
    Config(mock=True).validate()
    Config(coordinator="wss://example.com/ws/provider").validate()


def test_only_ie_credential_in_child_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "hosted-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-secret")
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    monkeypatch.setenv("HTTPS_PROXY", "credentialed-proxy")
    env = child_environment("provider-only")
    assert env["IE_PROVIDER_TOKEN"] == "provider-only"
    assert all("secret" not in value for value in env.values())
    assert "PYTHONPATH" not in env
    assert "HTTPS_PROXY" not in env


def test_store_lease_and_stale_status(tmp_path):
    first, second = Store(tmp_path), Store(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
    first.write("status", {"state": "serving", "updated": time.time() - 40})
    assert "no active" in first.status()


def test_idle_busy_pause_and_launch_failure(tmp_path):
    now = [0]
    launches = []

    def fail(*args, **kwargs):
        launches.append(kwargs)
        raise OSError("secret must not be logged")

    store = Store(tmp_path)
    controller = Controller(
        Config(sell_enabled=True, idle_minutes=1),
        store,
        clock=lambda: now[0],
        launch=fail,
    )
    controller.state = "idle-waiting"
    controller.tick()
    assert not launches
    controller.turns.add(("session", "turn"))
    now[0] = 1000
    controller.tick()
    assert not launches
    controller.finished(session_id="session")
    now[0] += 61
    controller.tick()
    assert controller.state == "error"
    assert len(launches) == 1
    controller.tick()
    assert len(launches) == 1
    now[0] += 6
    controller.tick()
    assert len(launches) == 2
    store.write("paused", True)
    controller.tick()
    assert controller.state == "disabled"
    now[0] += 100
    controller.tick()
    assert len(launches) == 2
    store.write("paused", False)
    controller.tick()
    assert controller.state == "idle-waiting"
    assert not controller.turns


def test_activity_clears_turn_after_interrupted_end(tmp_path):
    controller = Controller(Config(), Store(tmp_path))
    controller.activity(session_id="one", turn_id="a")
    controller.activity(session_id="two", turn_id="b")
    controller.finished(session_id="one")
    assert controller.turns == {("two", "b")}


def test_plugin_registers_without_runtime_side_effects(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "test_ie_plugin",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    hooks, commands = {}, {}

    class Context:
        state = SimpleNamespace(data_dir=tmp_path)

        def get_config(self, key, default):
            return default

        def register_hook(self, name, callback):
            hooks[name] = callback

        def register_command(self, name, handler, *args):
            commands[name] = handler

        def register_cli_command(self, *args):
            pass

        def on_unload(self, callback):
            self.close = callback

    ctx = Context()
    module.register(ctx)
    assert set(hooks) == {
        "on_session_start",
        "pre_llm_call",
        "post_llm_call",
        "on_session_end",
        "on_session_finalize",
    }
    assert not list(tmp_path.iterdir())
    ctx.close()


@pytest.mark.asyncio
async def test_drain_rejects_new_and_finishes_existing():
    started, finish = asyncio.Event(), asyncio.Event()
    sent = []

    class Backend:
        async def _handle_request(self, ws, req):
            started.set()
            await finish.wait()
            sent.append("completed")

    class Agent(DrainMixin, Backend):
        pending = set()

    class Socket:
        async def send(self, message):
            sent.append(json.loads(message)["error"])

    # This mixin uses IE's protocol model in the separate runtime.
    reference = os.environ.get("IE_REFERENCE_REPO")
    if not reference:
        pytest.skip("Set IE_REFERENCE_REPO for IE runtime tests")
    sys.path.insert(0, reference)
    agent = Agent()
    request = SimpleNamespace(request_id="request")
    task = asyncio.create_task(agent._handle_request(Socket(), request))
    await started.wait()
    agent.draining = True
    await agent._handle_request(Socket(), request)
    assert sent == ["provider_draining"]
    assert task in agent.pending
    finish.set()
    await task
    assert sent == ["provider_draining", "completed"]
    assert not agent.pending


async def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Lifecycle state timed out")
        await asyncio.sleep(0.03)


def runtime_config(coordinator):
    reference = os.environ.get("IE_REFERENCE_REPO")
    if not reference:
        pytest.skip("Set IE_REFERENCE_REPO for subprocess tests")
    return Config(
        sell_enabled=True,
        mock=True,
        ie_python=sys.executable,
        ie_repo=reference,
        coordinator=coordinator,
        idle_minutes=0.001,
        drain_seconds=8,
    ).validate()


@pytest.mark.asyncio
async def test_real_mock_registers_authenticates_and_drains(tmp_path):
    registered, sent = [], []
    socket_ready = asyncio.Future()

    async def coordinator(ws):
        assert ws.path == "/ws/provider"
        assert ws.request_headers["Authorization"] == "Bearer provider-test"
        registered.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "registered", "provider_id": "test"}))
        socket_ready.set_result(ws)
        async for raw in ws:
            sent.append(json.loads(raw))

    async with websockets.serve(coordinator, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        controller = Controller(
            runtime_config(f"ws://127.0.0.1:{port}/ws/provider"),
            Store(tmp_path),
            "provider-test",
        )
        try:
            controller.begin()
            await wait_until(lambda: controller.state == "serving")
            ws = await asyncio.wait_for(socket_ready, 3)
            assert registered[0]["type"] == "register"
            assert registered[0]["capabilities"]["trust_level"] == "open"
            await ws.send(
                json.dumps(
                    {
                        "type": "inference_request",
                        "request_id": "test-request",
                        "model": "default",
                        "messages": [{"role": "user", "content": "Hello"}],
                        "max_tokens": 15,
                    }
                )
            )
            await asyncio.sleep(0.2)
            started = time.monotonic()
            await asyncio.to_thread(controller.activity, session_id="demo", turn_id="one")
            assert time.monotonic() - started < 8
            assert controller.process is None
            assert any(item["type"] == "inference_done" for item in sent)
            assert controller.turns == {("demo", "one")}
        finally:
            await asyncio.to_thread(controller.close)
        assert controller.state == "disabled"


@pytest.mark.asyncio
async def test_real_coordinator_down_does_not_crash(tmp_path, caplog):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    controller = Controller(
        runtime_config(f"ws://127.0.0.1:{port}/ws/provider"),
        Store(tmp_path),
        "must-not-be-logged",
    )
    try:
        controller.begin()
        await wait_until(lambda: controller.state == "error")
        assert controller.thread.is_alive()
        assert controller.retry_at > controller.clock()
        assert "Coordinator unavailable" in caplog.text
        assert "must-not-be-logged" not in caplog.text
        controller.activity(session_id="demo", turn_id="one")
        assert controller.process is None
    finally:
        await asyncio.to_thread(controller.close)


@pytest.mark.asyncio
async def test_runner_eof_stops_backend(monkeypatch):
    from io import StringIO

    stopped = asyncio.Event()

    class Agent:
        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr(sys, "stdin", StringIO(""))
    await serve(Agent(), 8)
    assert stopped.is_set()


def test_force_stop_has_a_bound(tmp_path):
    config = Config(drain_seconds=3)
    controller = Controller(config, Store(tmp_path))
    kwargs = (
        {"start_new_session": True}
        if os.name != "nt"
        else {
            "creationflags": subprocess.CREATE_NO_WINDOW,
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        **kwargs,
    )
    controller.process = process
    started = time.monotonic()
    try:
        controller.stop_process()
        assert time.monotonic() - started < 3.5
        assert process.poll() is not None
        assert controller.process is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_failed_cleanup_blocks_local_work(tmp_path, monkeypatch):
    controller = Controller(Config(), Store(tmp_path))
    controller.process = object()

    def fail():
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(controller, "stop_process", fail)
    result = controller.activity()
    assert result["action"] == "block"
    assert controller.state == "error"


def test_stale_process_event_cannot_mark_new_provider_serving(tmp_path):
    controller = Controller(Config(), Store(tmp_path))
    controller.state = "idle-waiting"
    controller.events.put((object(), "serving"))
    controller.tick()
    assert controller.state == "idle-waiting"


def test_status_write_recovers_from_brief_windows_file_lock(tmp_path, monkeypatch):
    store = Store(tmp_path)
    replace_file = os.replace
    calls = []

    def locked_once(source, destination):
        calls.append(destination)
        if len(calls) == 1:
            raise PermissionError("file is busy")
        return replace_file(source, destination)

    monkeypatch.setattr(os, "replace", locked_once)
    store.write("status", {"state": "serving"})
    assert store.read("status") == {"state": "serving"}
    assert len(calls) == 2
