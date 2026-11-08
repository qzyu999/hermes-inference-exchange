# Issue 224 plan

Source: https://github.com/qzyu999/inference-exchange/issues/224

The project owner approved the requirements in the issue execution brief.
This plugin implements those requirements.
It does not change IE requirements or core code.

1. Check IE provider commands, authentication, model support, and shutdown.
2. Check the public Hermes API against release v0.23.0.
3. Add the manifest and plugin entry point.
4. Add an idle controller with bounded shutdown and retry backoff.
5. Add a provider adapter outside IE core.
6. Add status and pause controls through public Hermes APIs.
7. Test activity, failures, shutdown, and local coordinator registration.
8. Run the Hermes plugin validator.
9. Create and merge the plugin PR.
10. Open a catalog PR with the merged commit SHA.

## Source findings

IE reference commit: `0f0ce6c`.

- `ocip_agent/agent.py::main` starts the production provider.
  Use `python -m ocip_agent.agent --model MODEL.gguf --llama-server llama-server`.
  Set `--coordinator`, `--trust open`, and `--max-concurrent 1`.
- `inference_exchange/shared/provider_auth.py` sends `IE_PROVIDER_TOKEN`
  in the WebSocket Authorization header.
- `ocip_agent/agent.py::OCIPAgent.run` stops the model server in `finally`.
  The provider has no drain command.
  The plugin adapter rejects new requests during drain.
  It waits for current requests before it cancels the agent.
- `ocip_agent/agent.py::InferenceServerManager.start` supports a local GGUF
  through `llama-server` or `ocip_server.main`.
  Ollama and vLLM endpoints are not supported by this provider.
- IE exposes `/v1/chat/completions`.
  The README gives the Hermes custom-provider configuration.
  A separate model-provider plugin is outside the initial demo scope.
- `docs/guides/provider-l0-apple-silicon-preview.md` documents
  `wss://alpha.inferenceex.com/ws/provider`.
  A health check on 2026-11-08 failed TLS hostname verification.
  This check does not establish coordinator availability.

## Limits

The plugin targets one Hermes CLI process per profile.
The production alpha admits Apple Silicon/macOS L0 GGUF providers.
Local development can use the mock provider.
The provider adapter depends on IE request-handler methods at the reference commit.
The plugin does not replace or patch Hermes methods.
Catalog admission needs a human maintainer review.
The demo video and hackathon form need the project owner.
The issue does not establish whether the hackathon needs a merged catalog PR.
