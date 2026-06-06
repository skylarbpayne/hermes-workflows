from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from examples.redact_hackathon_review_packet import load_json, public_packet, render_public_html


def render_packet(*, snapshot_path: Path, receipt_path: Path, out_path: Path, summary_json_path: Path | None = None) -> dict[str, Any]:
    """Render a public-safe HTML packet and optional JSON summary.

    This wrapper intentionally delegates redaction to
    ``examples.redact_hackathon_review_packet`` so old callers cannot produce a
    raw-body "redacted" packet by accident.
    """
    packet = public_packet(snapshot=load_json(snapshot_path), receipt=load_json(receipt_path))
    render_public_html(packet, out_path)
    if summary_json_path is not None:
        summary_json_path.parent.mkdir(parents=True, exist_ok=True)
        summary_json_path.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
    return packet


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a public-safe Hack the Valley dry-run packet.")
    parser.add_argument("--snapshot", type=Path, required=True, help="Private snapshot JSON built from registration/submission exports.")
    parser.add_argument("--receipt", type=Path, required=True, help="Private workflow receipt JSON from the dry run.")
    parser.add_argument("--out", type=Path, required=True, help="HTML output path.")
    parser.add_argument("--summary-json", type=Path, help="Optional safe JSON packet output path.")
    args = parser.parse_args()

    packet = render_packet(
        snapshot_path=args.snapshot,
        receipt_path=args.receipt,
        out_path=args.out,
        summary_json_path=args.summary_json,
    )
    workflow = packet["workflow"]
    print(
        json.dumps(
            {
                "approvals": workflow.get("approvals") or [],
                "drafts": len(packet["coverage"]["drafts"]),
                "participants": len(packet["coverage"]["participants"]),
                "side_effects": workflow.get("side_effects") or {},
                "out": str(args.out),
                "summary_json": str(args.summary_json) if args.summary_json else None,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
