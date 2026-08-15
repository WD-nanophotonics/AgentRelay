import pytest

from dummy_orchestrator.adapters import HumanRequired
from dummy_orchestrator.registry import safe_match_project_subject


def test_structured_project_routing_allows_descriptive_subject():
    assert safe_match_project_subject("[ORCH][generic_chess][F17][TASK] review parser", ["generic_chess", "alpha_sho"]) == "generic_chess"


def test_ambiguous_fuzzy_routing_fails_closed():
    with pytest.raises(HumanRequired, match="ambiguous_gmail_project"):
        safe_match_project_subject("progress generic_chess alpha_sho", ["generic_chess", "alpha_sho"])
