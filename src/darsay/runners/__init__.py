"""Standalone engine runner scripts, executed inside hydrated environments.

Each script is self-contained (stdlib + its engine only — darsay is not
installed in hydrated envs). Contract with hydrate.py: invoked as
`<env-python> <runner>.py --json-out FILE ...`, streams human output to
stdout/stderr, and writes a single JSON result object to --json-out.
`--probe` checks the env against the payload without loading weights.
"""
