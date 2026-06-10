import logging

import common
from common.hashing import content_hash
from common.logging import get_logger
from common.token_budgeting import TokenBudget


def test_imports() -> None:
    assert common.hashing is not None
    assert common.logging is not None
    assert common.token_budgeting is not None


def test_content_hash_is_deterministic() -> None:
    assert content_hash("hello") == content_hash("hello")
    assert content_hash(b"hello") == content_hash("hello")
    assert (
        content_hash("hello")
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert content_hash("hello") != content_hash("hello ")


def test_get_logger_returns_logger() -> None:
    assert isinstance(get_logger("kb.test"), logging.Logger)


def test_token_budget_arithmetic() -> None:
    budget = TokenBudget(max_tokens=100, used_tokens=40)
    assert budget.remaining_tokens == 60
    assert budget.can_spend(60)
    assert not budget.can_spend(61)
    assert TokenBudget(max_tokens=10, used_tokens=25).remaining_tokens == 0
