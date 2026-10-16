"""Fail-closed preflight for normal pytest invocations."""

import os

INTERNAL_NEGATIVE_RUN = "GSD_RELEASE_CHECKS_INTERNAL"


def pytest_sessionstart(session):
    if os.environ.get(INTERNAL_NEGATIVE_RUN) == "1":
        return
    from run_release_checks import run_packet_gate

    run_packet_gate()


def pytest_collection_finish(session):
    if os.environ.get(INTERNAL_NEGATIVE_RUN) == "1":
        return
    acceptance_selected = any(
        item.path.name == "test_web_acceptance.py" for item in session.items
    )
    if acceptance_selected:
        from run_release_checks import required_acceptance_inputs

        required_acceptance_inputs()
