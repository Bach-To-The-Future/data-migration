---
status: accepted
---

# v1 can promise zero retention at every model location

Clients choose where the tool's model runs: Claude through Anthropic's API directly, through Amazon Bedrock, or through Google Vertex. Some require that their code is not retained by the model provider. Retention depends on the location, the model and the feature (checked on 17 October 2026):

- **Anthropic's most capable models** (Claude Fable 5 and 5.1, and Mythos) require 30-day retention at every location. On Bedrock that comes with possible review by AWS, and on Vertex with sharing with Anthropic.
- **The Batch API** keeps data for 29 days.
- **Other models and features** can run with nothing retained.

We decided that v1 uses only models and features that allow zero retention at all three locations:

- **Models:** no Fable or Mythos.
- **Features:** only adaptive thinking with effort, structured outputs and strict tools, and prompt caching. No Batch API, Files API, code execution or server tools.

Each engagement declares its policy, zero retention or the provider's default. Under zero retention, the tool checks the location before any call and refuses to run if a check fails:

- **Bedrock:** the effective retention mode must be `none`.
- **Vertex:** request-response logging must be off, and no Advanced AI consent may be granted.
- **Claude API, direct:** a named operator's attestation of ZDR is recorded, because it can't be read from the API.

We chose this because a promise that holds only for some models or features would depend on configuration a client cannot see.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/30#issuecomment-6235946119).

## Considered options

- **Use Fable for the hardest steps.** It is the most capable model. Rejected for v1: it is retained for 30 days at every location, so no engagement needing zero retention could use the tool as designed.
- **Use the Batch API where zero retention isn't required.** It halves the price. Rejected: it adds a second code path whose retention differs, for a saving the complexity routing partly recovers.

## Consequences

- **Each location's remaining exposure is stated in the promise.**
  - **Claude API, direct:** content flagged by Anthropic's trust and safety systems can be kept up to 2 years, even under ZDR.
  - **Bedrock:** content never leaves AWS and is never shared with Anthropic.
  - **Vertex:** prompt-cache hashes are Google service data.
- **The direct API's promise rests on an attestation.** The report says so.
- **Adding Fable later is a new decision.** It would make zero retention impossible for the steps that use it.

## Amendment, 18 October 2026

- **Checked or attested.** The promise is *checked* on Bedrock and Vertex, and *attested* on the direct API, where ZDR cannot be read. Both report documents say which. When the direct API is used, both also state that flagged content can be kept for up to 2 years.
- **Refusal fallbacks** are restricted to the engagement's allowed models, never Fable or Mythos, and the same retention check runs on the fallback model before it is used.

Decided in [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28), finding R15 and Further Note 2.
