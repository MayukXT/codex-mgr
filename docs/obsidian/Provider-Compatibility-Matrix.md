# Provider Compatibility Matrix

`codex-mgr` provides native compatibility profiles across major 2026 AI providers.

## Compatibility Table

| Provider | Supported Wire API | Auth Type | Default Model | Max Context |
| :--- | :--- | :--- | :--- | :--- |
| **OpenAI (Official)** | `responses` | OAuth / Subscription | `gpt-6.1-sol` | 512,000 |
| **Anthropic (Direct)** | `anthropic_proxy` | Bearer Token (`sk-ant-...`) | `claude-5-5-sonnet-20260928` | 1,000,000 |
| **AgentRouter** | `responses` | Bearer Token | `gpt-5.6-sol` | 256,000 |
| **OpenRouter** | `responses` / `chat` | Bearer Token | `anthropic/claude-5-5-sonnet` | 1,000,000 |
| **DeepSeek** | `chat` | Bearer Token | `deepseek-r2` | 128,000 |
| **Groq** | `chat` | Bearer Token | `llama-3.3-70b-versatile` | 128,000 |
| **Google Gemini** | `chat` | API Key | `gemini-2.5-pro` | 1,000,000 |
| **Local Ollama** | `chat` | None | `llama-3.3-70b` | Variable (128k) |
| **Local LM Studio** | `chat` | None | `local-model` | Variable |
| **Custom Proxy** | Customizable | Bearer / Env Var | User Defined | Custom |

## Conversion Rules
- **To OpenAI:** Purges unauthenticated synthetic reasoning tags; resets provider to `openai`.
- **To Non-OpenAI:** Strips `encrypted_content` Fernet tokens; binds provider to target.
- **Between Non-OpenAI Providers:** Preserves all message history; updates provider binding.

## Related Links
- [[Architecture]]
- [[Anthropic-5.5-Proxy-Adapter]]
- [[Reasoning-Blobs-And-Fernet]]
- [[Model-Discovery-And-Cache-Sync]]
