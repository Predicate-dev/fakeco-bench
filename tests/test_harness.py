"""Sanity checks: the naive agent should fail every non-control cell, the oracle should pass everything."""
from fakeco.agents import NaiveAgent, OracleAgent
from fakeco.core import run_episode
from fakeco.scenarios import build_suite

CONTROL = lambda sc: (sc.dials.get("policy") == "base" or sc.dials.get("fault") == "none"
                      or sc.dials.get("handoff") == "accurate")

def test_oracle_passes_everything():
    for sc in build_suite(n=2):
        r = run_episode(OracleAgent(sc), sc)
        assert r["score"]["pass"], (sc.id, r["score"])

def test_naive_fails_non_controls():
    for sc in build_suite(n=2):
        r = run_episode(NaiveAgent(sc), sc)
        assert r["score"]["pass"] == CONTROL(sc), (sc.id, r["score"])

def test_deterministic():
    a = [s.db for s in build_suite(seed=3)]
    b = [s.db for s in build_suite(seed=3)]
    assert a == b
