# Model settings

Use the exact model ID supported by the selected endpoint. Reasoning effort and context size must come from model metadata or a documented provider override.

The model settings menu controls the context limit and reasoning effort. Model default clears the explicit setting. Unknown capabilities remain unknown; refreshing a catalog does not prove that the account can run a listed model.

Provider catalogs retain separately reported input context, output ceiling, effort support and thinking modes. Each thinking mode has its own support flag: a model without thinking can still accept `disabled`. Stale catalog capabilities are withheld until refreshed. The output ceiling is model metadata, distinct from a requested output cap. See the [Models API reference](https://platform.claude.com/docs/en/api/models/retrieve).

Responses connections use Codex's native reasoning and context settings. Unsupported streaming, output-cap and thinking-budget overrides are refused. Fresh Chat and Messages launches use an owned local adapter; its saved streaming and output-cap settings affect upstream requests. Messages currently supports the explicit budget path described in [compatibility](Compatibility.md). Reported adaptive support alone does not enable that mode.
