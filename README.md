# Inference Exchange for Hermes

Serve a local model while Hermes CLI is idle.
Release the hardware before Hermes starts a user turn.
This plugin implements [IE issue 224](https://github.com/qzyu999/inference-exchange/issues/224).

The plugin needs Hermes v0.23.0 or later.
Selling is off by default.
Use one Hermes CLI process per profile.
Gateway mode is outside this release.

## Install

```sh
hermes plugins install qzyu999/hermes-inference-exchange
```

Review the install scanner findings before you enable the plugin.
The plugin starts a background thread and a provider process.

Prepare IE in a separate environment.
Follow the [IE provider guide](https://github.com/qzyu999/inference-exchange/blob/0f0ce6cefd4eff1bd9353cea40677769fbccd824/docs/guides/provider-l0-apple-silicon-preview.md).
Use IE commit `0f0ce6cefd4eff1bd9353cea40677769fbccd824` for this release.
Install `llama-server` from its official source.
Choose an existing local `.gguf` model.
The plugin does not download a model or an executable.

Add these settings to the active Hermes profile config.
Replace each example path with your local path.

```yaml
plugins:
  enabled:
    - inference-exchange
  hook_callback_timeout: 30
  entries:
    inference-exchange:
      settings:
        sell_enabled: true
        idle_minutes: 5
        drain_seconds: 8
        startup_seconds: 180
        coordinator: wss://alpha.inferenceex.com/ws/provider
        ie_python: /path/to/inference-exchange/.venv/bin/python
        ie_repo: /path/to/inference-exchange
        model: /path/to/model.gguf
        llama_server: /path/to/llama-server
```

Keep the Hermes hook timeout longer than `drain_seconds`.
The drain limit must be greater than zero and at most 20 seconds.
Restart Hermes after you change settings or credentials.
The production alpha admits Apple Silicon/macOS L0 GGUF providers.
The plugin always advertises `open` trust.
It does not claim verified hardware or confidential execution.

Enroll a provider through IE.
Store the returned `IE_PROVIDER_TOKEN` through the active Hermes profile's
secret settings.
The manifest declares this name as a secret.
The plugin reads it through `agent.secret_scope.get_secret`.
It does not read credential files.
Do not put a raw token in plugin settings or a shell command.
An empty credential works only on a development coordinator that permits it.

## Controls

```text
/ie status
/ie pause
/ie resume
```

```sh
hermes ie status
hermes ie pause
hermes ie resume
```

The CLI commands read the active profile's controller status.
They do not start another provider.
Pause and resume reach the controller on its next timer tick.
A pause does not disable the plugin installation.

States: `disabled`, `idle-waiting`, `starting`, `serving`, `draining`, `error`.
`serving` means the IE agent sent its registration.
Confirm admission and routing in the IE provider dashboard.
A coordinator rejection changes the state to `error`.
Status does not claim verified provider admission.

The timer starts after session activity.
It does not run a provider during a Hermes turn, including tool execution.
The next user turn drains and stops the provider.
The adapter rejects new requests during drain.
It waits for current requests within the configured limit.
It cancels remaining requests when the limit expires.
The coordinator can still route a request during drain.
That request receives `provider_draining`.
Pause removes the provider connection.

## Test the demo

Prepare IE's Python environment with its mock dependencies.
The mock does not need a GGUF file or `llama-server`.
Start a local development coordinator in another terminal:

```sh
cd /path/to/inference-exchange
python -m inference_exchange.coordinator
```

Use these Hermes settings for the demo:

```yaml
plugins:
  enabled: [inference-exchange]
  hook_callback_timeout: 30
  entries:
    inference-exchange:
      settings:
        sell_enabled: true
        mock: true
        idle_minutes: 0.1
        drain_seconds: 8
        coordinator: ws://localhost:8000/ws/provider
        ie_python: /path/to/inference-exchange/.venv/bin/python
        ie_repo: /path/to/inference-exchange
```

1. Start Hermes CLI.
2. Send a prompt.
3. Wait six seconds after the response.
4. Run `/ie status`.
5. Check for `serving`.
6. Check the local IE provider list.
7. Send another prompt.
8. Run `/ie status`.
9. Check for `idle-waiting`.
10. Run `/ie pause`.
11. Stop the coordinator.
12. Run `/ie resume`.
13. Wait for the next idle threshold.
14. Check for `error` and a coordinator message in the Hermes log.
15. Send a prompt.
16. Check that Hermes still responds.

For a drain demo, send an IE inference request before step 7.
Check that the request finishes before the provider stops.
A request longer than the drain limit can be cancelled.
Alpha credits are simulated.

Run the automated tests from this plugin directory:

```sh
python -m pip install "pytest>=8,<10" "pytest-asyncio>=0.24,<2" "websockets>=13,<14"
export IE_REFERENCE_REPO=/path/to/inference-exchange
python -m pytest tests -q
hermes plugins validate . --install-deps
```

Use the IE Python environment for tests that import IE.
On PowerShell, set `$env:IE_REFERENCE_REPO` instead of `export`.
Without this variable, the IE runtime tests skip.
The unit tests still run.

## Use IE for Hermes inference

IE exposes an OpenAI-compatible `/v1/chat/completions` endpoint.
Use the standard Hermes custom endpoint flow:

```sh
hermes model
```

Choose a custom OpenAI-compatible endpoint.
Set the base URL to `https://alpha.inferenceex.com/v1`.
Use an IE consumer API key and an available model.
A consumer key is different from a provider credential.
This release does not register a separate Hermes model-provider profile.
Use a model that supports the tool calls your Hermes workflow needs.

## Safety and disclosures

- The plugin connects to the configured IE coordinator.
- The provider makes outbound WebSocket calls.
- The provider reads the selected local model and installed IE code.
- The production provider opens its existing model server on loopback.
- The plugin does not open a public listener.
- The plugin launches Python with an argument list.
- It does not execute a configurable shell command.
- A timer thread and provider process run until activity, pause, or exit.
- On timeout, the plugin kills its provider process tree.
- On Windows, forced cleanup uses `taskkill /T /F`.
- The child receives the IE provider credential in its environment.
- It does not inherit hosted model keys or Hermes approval settings.
- The plugin stores status, pause state, and a lock in its Hermes data directory.
- It does not store credentials.
- It does not forward provider logs, prompts, or responses into status.
- It adds no telemetry.

The adapter extends IE request handlers outside IE core.
Its compatibility target is the reference commit in `docs/plan.md`.
It uses only public Hermes registration and secret APIs.
The plugin has no Python runtime dependencies inside Hermes.
IE dependencies stay in the separate IE environment.

The documented alpha endpoint failed TLS hostname verification from the
development machine on 2026-11-08.
Live alpha availability was not verified.
Do not disable TLS verification.
