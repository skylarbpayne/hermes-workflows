from __future__ import annotations

import hashlib
import json
import sys


GENERATED_WORKFLOW_SOURCE = '''
from hermes_workflows import workflow

@workflow
async def process_item(item):
    return {"processed": {"item_id": item["id"], "label": item["label"].upper()}}
'''


def main() -> int:
    request = json.load(sys.stdin)
    rendered_prompt = request.get("rendered_prompt", "")
    if request.get("returns") == "workflow":
        output = {"source": GENERATED_WORKFLOW_SOURCE, "symbol": "process_item"}
    else:
        output = {
            "kind": "example.agent_response.v1",
            "name": request.get("name"),
            "rendered_prompt_sha256": hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest(),
            "variables": request.get("variables", {}),
        }
    json.dump(
        {
            "output": output,
            "provenance": {"runner": "examples.runners.static_json_agent", "version": 1},
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
