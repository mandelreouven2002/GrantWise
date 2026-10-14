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
