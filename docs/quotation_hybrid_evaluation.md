# Local quotation hybrid evaluation

This prototype is separate from Django persistence and the automatic importer.
It does not connect to any database or save extracted values. The existing
rule-based extractor remains unchanged and always runs first. Only `missing`
and `ambiguous` results are candidates for Claude; `success` and `failed` do not
trigger the fallback.

## Dry run (no external calls)

```bash
source .venv/bin/activate
python -B -m budget.evaluate_quotes test-quotes
```

JSON output includes filename, SHA-256, original rules result, final result,
evidence/page numbers, and fallback outcome. No key or model is needed for a
dry run. `dry_run_candidate` means eligible, not externally processed.

## Mocked tests

```bash
python -B -m unittest budget.test_quote_extraction budget.test_quote_hybrid -v
```

All HTTP calls in tests are mocked and use synthetic PDFs and fake credentials.
Private real fixtures are used only by the existing local rule tests.

## External evaluation — only after explicit document approval

First review the exact documents and approve sending them to Anthropic. Do not
run the following until that approval has been given. Choose a model supporting
PDF input and structured JSON output that is available to your Anthropic account.

In Bash, enter the key through a hidden prompt (do not paste it into source code,
command history, reports, or chat):

```bash
set +x
read -r -s -p 'Anthropic API key: ' ANTHROPIC_API_KEY
printf '\n'
export ANTHROPIC_API_KEY
read -r -p 'Approved Claude model ID: ' ANTHROPIC_MODEL
export ANTHROPIC_MODEL
```

Then replace APPROVED_PDF_SHA256 with the hash from the reviewed dry run:

```bash
python -B -m budget.evaluate_quotes test-quotes \
  --claude --approve-sha256 APPROVED_PDF_SHA256 --max-api-calls 1
```

`--claude` alone is refused. Each approved hash refers to exact PDF bytes;
changed documents need a new approval. Repeat `--approve-sha256` for more approved
documents. Unapproved documents are processed locally only. A call budget limits
fallback attempts; there are no automatic retries. PDFs are sent inline, not
uploaded to the Files API. Requests have timeouts and document size/page limits.

```bash
unset ANTHROPIC_API_KEY ANTHROPIC_MODEL
```

## Validation and interpretation

- Strict response keys, decimal strings, currencies, and evidence page ranges.
- Amount must appear in the evidence; for text PDFs the quoted evidence must
  also occur on the cited page, allowing normalized RTL labels and whitespace.
- Unit/subtotal/before-discount evidence is rejected. Uncertain VAT semantics or
  currency evidence are flagged as ambiguous, not silently accepted.
- Scanned evidence cannot be verified independently by the local text extractor;
  it remains ambiguous for visual review. A readable page with an image-only
  section may also be rejected conservatively.
- Local partial values survive missing fallback values and API/validation errors.
  Conflicting values or currencies require review; no FX or VAT arithmetic runs.
- A `success` result still requires comparison with a human-labelled expected
  result. Structured JSON and verbatim evidence do not prove the amount is the
  final after-discount total.

API key and selected model come only from environment variables. They are never
printed by the tool. Reports contain quotation text and should remain private.
Review the selected account/model's data retention policy before approval.

## AWS Bedrock with a single API key

Official AWS documentation specifies the environment variable
`AWS_BEARER_TOKEN_BEDROCK` and HTTP `Authorization: Bearer <token>`:
https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-use.html

This adapter does not need `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, boto3,
or an Anthropic API key. It uses regional Bedrock Runtime Converse with an
inline base64 PDF and citations enabled. No S3 upload is performed. The selected
Claude model must support Converse PDF/citations in the supplied region; model
access and API-key permissions/expiry must be confirmed by the AWS account owner.
Cross-region inference profiles may process data in another AWS region.

Bedrock has its own request/response format and model-specific feature support.
This adapter does not assume availability of Anthropic `output_config`.
It requests JSON with the schema in the prompt, then performs the same strict
local validation and evidence checks. Provider-enforced structured output is
not enabled in this Bedrock prototype. Non-JSON, incomplete, or conflicting
answers are rejected or flagged, and local partial values are preserved.

In WSL Bash, after activating the project virtual environment:

```bash
set +x
read -r -s -p 'Bedrock API key: ' AWS_BEARER_TOKEN_BEDROCK
printf '\n'
export AWS_BEARER_TOKEN_BEDROCK
read -r -p 'AWS region from your account owner: ' AWS_REGION
export AWS_REGION
read -r -p 'Authorized Claude Bedrock model/profile ID or ARN: ' BEDROCK_MODEL_ID
export BEDROCK_MODEL_ID
```

Use the region/model supplied by the account owner, not a direct Anthropic model
name. Do not print these environment variables or include keys in shell history.
The evaluation adapter currently supports commercial AWS region endpoints only.

Dry run on one document (no external calls and no credential requirements):

```bash
python -B -m budget.evaluate_quotes test-quotes --provider bedrock \
  --filename workorder_19363.pdf --pretty
sha256sum test-quotes/workorder_19363.pdf
```

Only after approving this exact document for AWS Bedrock processing, run:

```bash
python -B -m budget.evaluate_quotes test-quotes --provider bedrock \
  --filename workorder_19363.pdf --pretty --claude \
  --approve-sha256 APPROVED_PDF_SHA256 --max-api-calls 1
```

Replace APPROVED_PDF_SHA256 with the reviewed hash. The approval gate remains
mandatory. The call limit counts attempts; errors cause no automatic retries.
The script does not contact Django or save results. Examine `rules`, `final`,
`fallback_outcome`, and evidence before trusting the output.

Safe HTTP diagnostics appear as `bedrock_http_401`, `bedrock_http_403`, etc.,
without provider response bodies or credentials. For 401 check expiry/token;
for 403 ask the owner to check model/profile permissions; for 400 check model,
region, and PDF/citation support; for 429 check quota. No automatic provider
switch or retry is performed.

```bash
unset AWS_BEARER_TOKEN_BEDROCK AWS_REGION BEDROCK_MODEL_ID
```

Mocked regression tests:

```bash
python -B -m unittest budget.test_quote_extraction budget.test_quote_hybrid \
  budget.test_quote_bedrock -v
```

## Safe failure diagnostics

`diagnostic` is now included in the evaluation report. It contains a failure
stage and fixed code, and where relevant a validation field, HTTP status,
allowlisted AWS exception type, known stop reason, or JSON syntax line/column.
Raw exception messages, HTTP bodies, headers, keys, PDF text, model output, and
account-specific model/profile ARNs are never included in diagnostics. The
normal extraction report still contains local quotation evidence; keep it private.

Before retrying an API call, check the configuration without contacting AWS:

```bash
python -B -m budget.evaluate_quotes test-quotes --provider bedrock --check-config
```

This reports missing variable names or malformed configuration without printing
their values. A valid local configuration does not verify model existence,
regional support, API-key permissions, or expiry. `--check-config` cannot be
combined with `--claude`.

Examples: `response_json/model_output_not_json` (including a Markdown-fence
flag), `response_envelope/response_not_complete` with `max_tokens`,
`validation/evidence_not_on_page`, and
`validation/non_final_or_unlabelled_evidence`. These distinctions are diagnostic
only; extraction rules, JSON parsing, and evidence validation remain strict.

An old generic rejected result cannot reveal its precise original cause. A new
explicitly approved test is required if no original detailed diagnostic exists.

## Low-cost Bedrock evaluation

Every Converse request sets `inferenceConfig.maxTokens` explicitly. Its default
is 1024, configurable with `BEDROCK_MAX_OUTPUT_TOKENS` or `--max-output-tokens`
(CLI takes precedence). The local prototype allows 1–4096; the chosen model may
have additional limits. Missing/ambiguous values and evidence remain mandatory.
If the configured limit cuts off the response, it is rejected; no automatic retry runs.

The adapter uses a fresh HTTP session with `HTTPAdapter(max_retries=0)` and no
redirect following. There is no AWS SDK retry layer. `--max-api-calls 1` counts
attempts, including unsuccessful ones, and prevents another request in the run.
The Bedrock evaluation CLI now rejects `--max-api-calls` values other than 1.
A timeout does not prove the service performed no inference; usage may be unknown.

A compact prompt replaces duplicate schema/instruction text. Only the selected
PDF is sent once. Original pages are retained because blindly taking the last
page can omit discounts, charges, currency conditions or change evidence page
numbers. The current workorder PDF is one page; no other PDFs/pages are included.

`maxTokens` limits output only; Converse does not provide a separate input-token
cap that truncates PDFs safely. Input cost depends on all PDF pages and the prompt.
`api_usage` reports allowlisted integer `inputTokens`, `outputTokens`,
`totalTokens`, and optional cache token counts from the response. Counts are
reported even if the model output is subsequently rejected. Missing usage is
null, not zero. No keys, document text or server response bodies are included
in usage metadata. A token count is not a currency cost estimate.

After reviewing configuration and explicitly approving the same PDF:

```bash
python -B -m budget.evaluate_quotes test-quotes --provider bedrock \
  --filename workorder_19363.pdf --pretty --claude \
  --approve-sha256 APPROVED_PDF_SHA256 --max-api-calls 1 --max-output-tokens 1024
```

## Converse content formats

The response parser supports plain `text`, cited answer text under
`citationsContent.content[].text`, and ordered mixtures of those blocks. Each
ContentBlock is validated as a union with exactly one member. Document citations
are metadata, not a substitute for the existing quotation evidence validation.

Documented `reasoningContent.reasoningText` (with optional signature) and
`reasoningContent.redactedContent` are explicitly validated as metadata. They
are never used as JSON answers and never exposed in reports. Valid `cachePoint`
metadata is also explicitly counted. `response_blocks` reports only known type
names and counts. Reasoning/cache without answer text is rejected.

Other known block types, such as `toolUse`, image, or audio, are rejected because
this evaluation requests only a quotation JSON answer and executes no tools.
Diagnostics include the block index and allowlisted type names. Unknown keys
are represented by a count, never printed verbatim. Unknown types, malformed
unions, and invalid reasoning/citation structures are not silently ignored.

Sources:
https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ContentBlock.html
https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_CitationsContentBlock.html
https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ReasoningContentBlock.html

The old `content_block_unsupported` result cannot reveal the actual block type.
Reasoning was previously a legitimate but unsupported type; it is a plausible
cause, not proof about the prior response. The next explicitly approved run
will report accepted block counts or a precise safe block diagnostic.

## Haiku truncation and thinking controls

The request does not explicitly enable thinking. This does not establish that
the selected model runs without it: defaults differ by model version. AWS
documents adaptive thinking as enabled by default on Haiku 5.5; Haiku 4.5 uses
explicit extended-thinking settings. Thinking tokens count toward the output
limit and are billed as output. There is no guaranteed separation of reasoning
tokens in the ordinary usage counters.

Previously a `max_tokens` response was rejected before block counts were
recorded. Now `response_blocks` records only known top-level type counts even
on truncation. `{}` in an older report therefore does not prove absence of
reasoning or answer blocks. Partial JSON remains rejected, even if it appears
complete; no retry or automatic output-limit increase occurs.

The prompt requests one compact object, short original supporting quotes,
and no explanations, translation, citation prose or full-page transcription.
The required keys, monetary semantics, and evidence validator are unchanged.

For a verified Haiku 4.5 or 5.5 identifier, `--disable-thinking` sends:

```json
{"additionalModelRequestFields":{"thinking":{"type":"disabled"}}}
```

It is opt-in. Unknown model versions and opaque application profile ARNs are
refused before any request; do not guess the model behind an ARN. No generic
`effort`, `temperature`, or `budget_tokens` setting is applied to all Haiku
versions. Haiku 5.5 supports effort controls, while older Haiku versions cannot
be assumed to do so. Explicitly disabling thinking is the smaller experiment
for this extraction task.

First verify the exact model/profile target with the account owner, then run
this local check (no network):

```bash
python -B -m budget.evaluate_quotes test-quotes --provider bedrock \
  --check-config --disable-thinking --max-output-tokens 1024
```

After approval, keep the same 1024 limit and one attempt, adding
`--disable-thinking` to the existing single-document evaluation command.
If `max_tokens` occurs again, compare only safe block counts and token usage;
do not print reasoning or raw model answers for diagnosis.

Official references:
https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html
https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html
https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html

## Haiku 5.5 low-effort experiment

`--effort low` (or `BEDROCK_EFFORT=low`) sets
`additionalModelRequestFields.output_config.effort`. The CLI overrides the
environment. No effort is sent if neither is provided. This prototype enables
effort only for recognizable Haiku 5.5 IDs; older Haiku and opaque profile ARNs
are rejected locally. `low`, `medium`, and `high` can combine with disabled
thinking. `xhigh`/`max` plus disabled thinking is refused before a request.

The requested low-effort/disabled combination is documented by AWS, but does
not prove absence of reasoning in a returned response. `requested_effort` and
`thinking_policy` describe the requested settings, not confirmed server behavior.

`response_lengths` reports counts, per-block lengths, and total lengths for
plain text, cited generated text, and reasoning text, measured in Unicode
characters. Redacted reasoning reports base64 string lengths separately. It
never includes reasoning text, signatures, citation source text, keys, or raw
model responses. Counts are retained even on `max_tokens` rejection. No response
means null metrics. These are not token counts and cannot identify the exact
billed reasoning-token share.

With the existing key and region environment variables set, use:

```bash
export BEDROCK_MODEL_ID='global.anthropic.claude-haiku-5-5'
python -B -m budget.evaluate_quotes test-quotes --provider bedrock \
  --check-config --disable-thinking --effort low --max-output-tokens 1024
```

The configuration check has no network request or inference charge. After
explicit document approval and checking that its hash has not changed:

```bash
python -B -m budget.evaluate_quotes test-quotes --provider bedrock \
  --filename workorder_19363.pdf --pretty --claude \
  --disable-thinking --effort low --max-output-tokens 1024 --max-api-calls 1 \
  --approve-sha256 58883c90ef864139881d27973c2481f849eb5c5cd2422a569bd91ac5f64ca390
```

Examine `api_usage`, `response_blocks`, `response_lengths`, `diagnostic`, and
the validated extraction result. No automatic retries or token-limit increases
occur. No database reads/writes, migrations, or funding actions are performed.

## Hebrew evidence grounding

Evidence normalization applies Unicode NFKC, normalizes typographic quotes and
removes presentation controls/whitespace while preserving numerical punctuation
and monetary signs. If a literal normalized quote does not match, a Hebrew quote
with exactly one number may match the complete token multiset of a contiguous
1–3-line window on its cited page. This handles RTL row order, not arbitrary
fuzzy similarity or page-wide amount searches. No extra words, different
currency notation, different numbers, or multi-total reassignments are accepted
through this path. Substring matches cannot hide a leading sign or match only
part of a number.

Exact amount, page range, currency, final-total labels, and before-discount
rejection remain mandatory. A reordered quote for workorder 19363's including-
VAT total can pass, but the before-VAT total stays null. No amount is computed.
Local private-fixture tests use mocked model responses; no document is sent
externally. Since the historical Claude quote was not retained in diagnostics,
a real retry is optional for end-to-end confirmation, not required to test this
normalization locally.
