# Choosing the validator LLM (researched 2026-09-29)

Current setup: DeepSeek for both drafter and validator. For a genuinely independent check the validator should be a different model family; the options below were researched against official docs on 2026-09-29.

## Validator recommendation (checked 2026-09-29)

**What I re-checked and corrected**
- OpenAI supports API use from Singapore. This is now VERIFIED; the report had it as INFERRED ([supported-countries](https://developers.openai.com/api/docs/supported-countries)).
- `gpt-6-luna` has no dated snapshot, only the alias ([model page](https://developers.openai.com/api/docs/models/gpt-6-luna)). For a pinned fallback, use `gpt-5.4-mini-2026-03-17`.
- Call Luna on OpenAI directly, not through OpenRouter. OpenRouter's `openai/gpt-6-luna` entry doesn't list `temperature` as a supported parameter ([models API](https://openrouter.ai/api/v1/models)).
- Hy3 on OpenRouter, VERIFIED: only DeepInfra (fp4), Novita and AtlasCloud accept `tool_choice:"required"`. Tencent's own endpoint does not ([endpoints API](https://openrouter.ai/api/v1/models/tencent/hy3/endpoints)).

### Top 3
1. **OpenAI `gpt-6-luna`, reasoning off.** It costs $0.10 in / $0.50 out per 1M tokens, so the whole hackathon is about $1–3. It supports `required` and `strict` natively, it's likely the fastest of these, and you can get a key today.
2. **Tencent Hy3 via OpenRouter (`tencent/hy3`).** It's the best choice for Tencent judges and costs the same as Tencent's own TokenHub. Two problems: its reported speed (11–21 tokens/s, not re-verified) means a 150-token plan takes more than 5 s. Also, `required` only reaches the quantized third-party hosts listed above. Going to TokenHub directly needs ID review, which takes 2–4 business days ([Quick Start](https://intl.cloud.tencent.com/document/product/1300/78939)).
3. **Claude Haiku 4.5 (`claude-haiku-4-5-20251001`).** It's fast and mature, and it accepts forced tool calls and temperature 0. But it costs about 10x more, and Anthropic's OpenAI-compatible layer ignores `strict` ([docs](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)).

Skip Qwen. It rejects `required` ([docs](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling)), and Alibaba is Tencent Cloud's direct competitor.

### #1 setup
```
GV_VALIDATOR_BASE_URL=https://api.openai.com/v1
GV_VALIDATOR_API_KEY=<paste key>
GV_VALIDATOR_MODEL=gpt-6-luna
GV_VALIDATOR_EXTRA_BODY={"reasoning_effort":"none"}
```
The EXTRA_BODY line is required. Luna only does tool calls in Chat Completions with `reasoning_effort: "none"`, and temperature is only allowed in that mode ([guide](https://developers.openai.com/api/docs/guides/latest-model)). Add a check at startup that it's set.

**Signup** (steps INFERRED; the help page was blocked):
1. Sign up at platform.openai.com and verify your phone.
2. Under Billing, add a card and buy $5 of credit. That gives Tier 1: 500 requests/min and 500K tokens/min (VERIFIED).
3. Under API keys, create a secret key and set a monthly budget cap.

Optional: for arguments that always match your schema, set `"strict": true` on each tool, with `additionalProperties:false` and every field required.

### Should you switch the drafter?
No. Keep DeepSeek as the drafter; don't swap models mid-hackathon. For the Tencent story, make Hy3 via OpenRouter a drafter you can switch to with env vars, with `{"reasoning":{"effort":"none"},"provider":{"only":["tencent"]}}`. That keeps the data in Singapore. Only promote it if it matches DeepSeek on your test cases and stays under 5 s.

**Fallbacks**
- Validator: `gpt-5.4-mini-2026-03-17`, a model env-var change only.
- Drafter: DeepSeek.
- If you use Hy3 through OpenRouter, make sure your "required" to "auto" fallback also catches a 404 (INFERRED), not just a 400. Done (1 Oct): `OpenAICompatLLM` falls back on 400, 404 and 422. `.env.example` has the OpenRouter Hy3 validator lines, pinned to Tencent's own endpoint with `{"provider": {"only": ["tencent"]}}`.
