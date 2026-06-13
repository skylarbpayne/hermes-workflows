from __future__ import annotations

import hermes_workflows


def test_retired_approval_loop_helper_is_not_public_authoring_api():
    retired_name = "approve_" + "until"
    assert not hasattr(hermes_workflows, retired_name)
    assert retired_name not in hermes_workflows.__all__
