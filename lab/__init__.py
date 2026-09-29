"""Experiment tooling: load generation, failure injection, consistency checks and metrics.

Everything except ``orchestrate`` and ``summarize`` runs inside the ``lab`` container on
the stack network, so load, service logs and Docker events share one clock.
"""
