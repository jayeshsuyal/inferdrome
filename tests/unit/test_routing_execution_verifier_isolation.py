"""The offline entrypoint must not import execution or transport machinery."""

from __future__ import annotations

import subprocess
import sys


def test_offline_verifier_import_is_execution_and_transport_isolated() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import inferdrome.routing_execution.verifier; "
                "assert 'inferdrome.routing_execution.executor' not in sys.modules; "
                "assert 'inferdrome.routing_execution.transport' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
