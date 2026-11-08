"""Separate IE process with stdin drain control. Does not import Hermes."""

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path


def emit(event):
    print("IE_EVENT:" + event, flush=True)


class Events(logging.Handler):
    def emit(self, record):
        # Do not forward provider logs: they can contain prompts or identifiers.
        if record.levelno >= logging.WARNING:
            emit("error")


class DrainMixin:
    draining = False

    async def _handle_request(self, ws, req):
        from inference_exchange.shared.protocol import InferenceError

        if self.draining:
            await ws.send(
                InferenceError(
                    request_id=req.request_id,
                    error="provider_draining",
                ).model_dump_json()
            )
            return
        task = asyncio.current_task()
        self.pending.add(task)
        try:
            await super()._handle_request(ws, req)
        finally:
            self.pending.discard(task)

    async def _handle_inference(self, ws, req):
        from inference_exchange.shared.protocol import InferenceError

        if self.draining:
            await ws.send(
                InferenceError(
                    request_id=req.request_id,
                    error="provider_draining",
                ).model_dump_json()
            )
            return
        task = asyncio.current_task()
        self.pending.add(task)
        try:
            await super()._handle_inference(ws, req)
        finally:
            self.pending.discard(task)

    async def _heartbeat_loop(self, ws):
        # The IE agent has sent registration. Admission can still fail.
        emit("serving")
        await super()._heartbeat_loop(ws)


async def serve(agent, timeout):
    agent.pending = set()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def controls():
        # EOF also drains: the controlling Hermes process may have exited.
        for line in sys.stdin:
            if line.strip() == "drain":
                break
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=controls, daemon=True).start()
    run_task = asyncio.create_task(agent.run())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {run_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if run_task in done:
            await run_task
            return
        agent.draining = True
        # Yield once so requests already queued before drain are accounted for.
        await asyncio.sleep(0)
        if agent.pending:
            await asyncio.wait(list(agent.pending), timeout=max(0.01, timeout - 6))
    finally:
        run_task.cancel()
        stop_task.cancel()
        await asyncio.gather(run_task, stop_task, return_exceptions=True)


def main():
    config = json.loads(sys.stdin.readline())
    # Direct script execution does not add the IE checkout to sys.path.
    sys.path.insert(0, str(Path.cwd()))
    logging.basicConfig(handlers=[Events()], level=logging.INFO, force=True)
    token = os.environ.get("IE_PROVIDER_TOKEN", "")
    if config["mock"]:
        from inference_exchange.config import ProviderConfig
        from inference_exchange.provider.agent import ProviderAgent
        from inference_exchange.provider.mock_engine import MockEngine

        class Agent(DrainMixin, ProviderAgent):
            pass

        agent = Agent(
            ProviderConfig(
                coordinator_url=config["coordinator"],
                provider_token=token,
                provider_name="hermes-idle-mock",
                max_concurrent=1,
            ),
            MockEngine(tps=20),
            trust_level="open",
        )
    else:
        from ocip_agent.agent import OCIPAgent

        class Agent(DrainMixin, OCIPAgent):
            pass

        agent = Agent(
            coordinator_url=config["coordinator"],
            provider_token=token,
            model_path=config["model"],
            llama_server_path=config["llama_server"],
            provider_name="hermes-idle",
            max_concurrent=1,
            trust_level="open",
        )
    asyncio.run(serve(agent, config["drain_seconds"]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emit("error")
        raise SystemExit(1) from None
