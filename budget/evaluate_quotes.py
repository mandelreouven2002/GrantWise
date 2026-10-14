"""python -m budget.evaluate_quotes test-quotes (dry run by default)."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .quote_hybrid import ClaudeFallback, evaluate_pdf
from .quote_bedrock import BedrockFallback
from .quote_diagnostics import safe_exception


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local comparison only; never saves to Django or modifies PDFs.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--claude", action="store_true", help="Enable external processing of explicitly approved PDF hashes")
    parser.add_argument("--provider", choices=("anthropic", "bedrock"), default="anthropic")
    parser.add_argument("--approve-sha256", action="append", default=[], help="Explicit consent to send this exact PDF to the selected provider")
    parser.add_argument("--filename", help="Evaluate only this relative PDF filename")
    parser.add_argument("--pretty", action="store_true", help="Print indented JSON")
    parser.add_argument("--check-config", action="store_true", help="Check Bedrock configuration locally without exposing values or calling AWS")
    parser.add_argument("--max-api-calls", type=int, default=1)
    parser.add_argument("--max-output-tokens", type=int, help="Bedrock only; defaults to BEDROCK_MAX_OUTPUT_TOKENS or 1024")
    parser.add_argument("--disable-thinking", action="store_true", help="Bedrock only; verified Haiku 4.5/5.5 identifiers, not opaque profile ARNs")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), help="Bedrock Haiku 5.5 only; defaults to BEDROCK_EFFORT if set")
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        parser.error("Folder does not exist")
    if args.claude and not args.approve_sha256:
        parser.error("Real API calls require --approve-sha256 for each approved document")
    if args.max_api_calls < 1:
        parser.error("--max-api-calls must be positive")
    if args.provider == "bedrock" and args.max_api_calls != 1:
        parser.error("Bedrock evaluation is limited to one network attempt per run; use --max-api-calls 1")
    if args.max_output_tokens is not None and (args.provider != "bedrock" or not 1 <= args.max_output_tokens <= 4096):
        parser.error("--max-output-tokens requires Bedrock and a value from 1 to 4096")
    if args.disable_thinking and args.provider != "bedrock":
        parser.error("--disable-thinking requires --provider bedrock")
    if args.effort is not None and args.provider != "bedrock":
        parser.error("--effort requires --provider bedrock")
    if args.check_config:
        if args.provider != "bedrock" or args.claude:
            parser.error("--check-config requires --provider bedrock and forbids --claude")
        try:
            diagnostic = BedrockFallback(max_output_tokens=args.max_output_tokens, disable_thinking=args.disable_thinking, effort=args.effort).preflight()
        except Exception as exc:
            diagnostic = safe_exception(exc, "configuration")
        print(json.dumps({"diagnostic": diagnostic}, ensure_ascii=False, indent=2))
        return
    calls = 0
    for path in sorted(args.folder.rglob('*')):
        if not path.is_file() or path.suffix.lower() != '.pdf':
            continue
        if args.filename and str(path.relative_to(args.folder)) != args.filename:
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        fallback = None
        client = None
        if args.claude and digest in args.approve_sha256 and calls < args.max_api_calls:
            client = (BedrockFallback(args.approve_sha256, max_output_tokens=args.max_output_tokens, disable_thinking=args.disable_thinking, effort=args.effort)
                      if args.provider == "bedrock" else ClaudeFallback(args.approve_sha256))
            def fallback(pdf_bytes, prompt):
                nonlocal calls
                calls += 1
                return client(pdf_bytes, prompt)
        evaluation = evaluate_pdf(data, fallback=fallback)
        print(json.dumps({"filename": str(path.relative_to(args.folder)), "sha256": digest,
                          "provider": args.provider, **asdict(evaluation),
                          "api_usage": client.usage if isinstance(client, BedrockFallback) else None,
                          "response_blocks": client.response_blocks if isinstance(client, BedrockFallback) else None,
                          "response_lengths": client.response_lengths if isinstance(client, BedrockFallback) else None,
                          "requested_effort": client.effort if isinstance(client, BedrockFallback) else None,
                          "thinking_policy": ("disabled" if client.disable_thinking else "model_default") if isinstance(client, BedrockFallback) else None,
                          "max_output_tokens": client.max_output_tokens if isinstance(client, BedrockFallback) else None,
                          "sent_pdf_pages": client.document_pages if isinstance(client, BedrockFallback) else None},
                         ensure_ascii=False, default=str, indent=2 if args.pretty else None))


if __name__ == '__main__':
    main()
