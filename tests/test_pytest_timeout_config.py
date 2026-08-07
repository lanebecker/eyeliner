"""#174 — a global per-test timeout must stay configured.

`pytest-timeout` (pinned in requirements.txt) is the mechanism; `pytest.ini`'s
`timeout` is the GLOBAL that makes an infinite-loop regression fail fast with a
stack dump instead of hanging CI (or a local run) indefinitely. This pins that
the global stays set:

  - remove the `timeout` line from pytest.ini  → getini returns the empty default
    → this fails;
  - drop pytest-timeout from the environment    → `timeout` is no longer a
    registered ini option → getini raises → this fails.

The plugin's actual kill-a-hang behaviour is pytest-timeout's own (well-tested)
job; this guards only OUR contribution — that the global is wired up.
"""


def test_global_per_test_timeout_is_configured(pytestconfig):
    raw = pytestconfig.getini("timeout")
    assert raw not in (None, ""), "no global 'timeout' configured in pytest.ini (#174)"
    assert float(raw) > 0, f"global per-test timeout must be positive, got {raw!r}"
