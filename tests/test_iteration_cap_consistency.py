"""
M7 (D1): regression guard for a real bug found while raising the iteration
cap — webhook.py and reconciler.py both decide whether a fix-branch retry
should happen, using the same logical check, but with hardcoded literals
that had drifted (webhook.py: >= 3, reconciler.py: >= 2). A run recovered
by the reconciler's sweep (webhook delivery missed/delayed) gave up one
iteration earlier than a run handled by the webhook directly.

Fixed with a single shared MAX_FIX_ITERATIONS constant in processor.py.
These tests guard against the literals drifting apart again — source
inspection rather than full behavioral tests, since both call sites are
deep inside heavily Supabase-dependent async functions.
"""
import inspect
import re

from app.agent import processor, reconciler
from app import webhook


def test_max_fix_iterations_is_four():
    assert processor.MAX_FIX_ITERATIONS == 4


def test_webhook_uses_the_shared_constant_not_a_literal():
    source = inspect.getsource(webhook)
    assert "MAX_FIX_ITERATIONS" in source
    # No leftover hardcoded iteration-exhaustion literal alongside it.
    assert not re.search(r"max_iteration\s*>=\s*\d+", source)


def test_reconciler_uses_the_shared_constant_not_a_literal():
    source = inspect.getsource(reconciler)
    assert "MAX_FIX_ITERATIONS" in source
    assert not re.search(r"max_iteration\s*>=\s*\d+", source)
