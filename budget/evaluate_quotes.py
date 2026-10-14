"""python -m budget.evaluate_quotes test-quotes (dry run by default)."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .quote_hybrid import ClaudeFallback, evaluate_pdf
from .quote_bedrock import BedrockFallback


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local comparison only; never saves to Django or modifies PDFs.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--claude", action="store_true", help="Enable external processing of explicitly approved PDF hashes")
    parser.add_argument("--provider", choices=("anthropic", "bedrock"), default="anthropic")
    parser.add_argument("--approve-sha256", action="append", default=[], help="Explicit consent to send this exact PDF to the selected provider")
    parser.add_argument("--filename", help="Evaluate only this relative PDF filename")
    parser.add_argument("--pretty", action="store_true", help="Print indented JSON")
    parser.add_argument("--max-api-calls", type=int, default=1)
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        parser.error("Folder does not exist")
    if args.claude and not args.approve_sha256:
        parser.error("Real API calls require --approve-sha256 for each approved document")
    if args.max_api_calls < 1:
        parser.error("--max-api-calls must be positive")
    calls = 0
    for path in sorted(args.folder.rglob('*')):
        if not path.is_file() or path.suffix.lower() != '.pdf':
            continue
        if args.filename and str(path.relative_to(args.folder)) != args.filename:
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        fallback = None
        if args.claude and digest in args.approve_sha256 and calls < args.max_api_calls:
            client = (BedrockFallback if args.provider == "bedrock" else ClaudeFallback)(args.approve_sha256)
            def fallback(pdf_bytes, prompt):
                nonlocal calls
                calls += 1
                return client(pdf_bytes, prompt)
        evaluation = evaluate_pdf(data, fallback=fallback)
        print(json.dumps({"filename": str(path.relative_to(args.folder)), "sha256": digest,
                          "provider": args.provider, **asdict(evaluation)},
                         ensure_ascii=False, default=str, indent=2 if args.pretty else None))


if __name__ == '__main__':
    main()
