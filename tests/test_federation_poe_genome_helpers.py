"""
Federation engine, PoE ledger, policy genome,
constitutional engine helpers.

Targets:
  - orchestrator.core.federation_engine (delta extraction, sigma proof,
    blind aggregator, consent, encryption, weekly nonce)
  - orchestrator.core.poe_ledger (hash_decision, should_sample, ChainEntry,
    cache, append_entry, verify_chain)
  - orchestrator.core.policy_genome (gene types, genome CRUD, crossover,
    mutation, diff, from_yaml, to_yaml)
  - orchestrator.core.constitutional_engine (_tournament_select,
    EvolutionResult)
"""
from __future__ import annotations

import random
import uuid

import pytest
# ═══════════════════════════════════════════════════════════════════════════════
# 1. FEDERATION ENGINE — extract_delta
# ═══════════════════════════════════════════════════════════════════════════════

def test_extract_delta_basic():
    from orchestrator.core.federation_engine import extract_delta
    before = {"gene_a": 1.0, "gene_b": 2.0}
    after = {"gene_a": 1.5, "gene_b": 2.0, "gene_c": 3.0}
    delta = extract_delta(before, after, 0.5, 0.8, 1, 2)
    assert delta.gene_diffs["gene_a"] == 0.5
    assert "gene_b" not in delta.gene_diffs  # no change
    assert delta.gene_diffs["gene_c"] == 3.0
    assert delta.fitness_before == 0.5
    assert delta.fitness_after == 0.8
    assert delta.generation_from == 1
    assert delta.generation_to == 2


def test_extract_delta_removal():
    from orchestrator.core.federation_engine import extract_delta
    before = {"gene_a": 1.0, "gene_b": 2.0}
    after = {"gene_a": 1.0}
    delta = extract_delta(before, after, 0.5, 0.6, 1, 2)
    assert delta.gene_diffs["gene_b"] == -2.0  # 0 - 2


def test_extract_delta_no_changes():
    from orchestrator.core.federation_engine import extract_delta
    g = {"gene_a": 1.0}
    delta = extract_delta(g, g, 0.5, 0.5, 1, 1)
    assert delta.gene_diffs == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. FEDERATION ENGINE — weekly nonce
# ═══════════════════════════════════════════════════════════════════════════════

def test_weekly_nonce_deterministic():
    from orchestrator.core.federation_engine import _weekly_nonce
    n1 = _weekly_nonce("test-key-123")
    n2 = _weekly_nonce("test-key-123")
    assert n1 == n2
    assert len(n1) == 32


def test_weekly_nonce_different_keys():
    from orchestrator.core.federation_engine import _weekly_nonce
    n1 = _weekly_nonce("key-a")
    n2 = _weekly_nonce("key-b")
    assert n1 != n2


# ═══════════════════════════════════════════════════════════════════════════════
# 3. FEDERATION ENGINE — sigma proof
# ═══════════════════════════════════════════════════════════════════════════════

def test_generate_sigma_proof():
    from orchestrator.core.federation_engine import (
        generate_sigma_proof, ConstitutionDelta,
    )
    delta = ConstitutionDelta(
        gene_diffs={"gene_a": 0.5, "gene_b": -0.2},
        fitness_before=0.5,
        fitness_after=0.8,
        generation_from=1,
        generation_to=2,
    )
    proof = generate_sigma_proof(delta, 0.3, 2, 1)
    assert proof.commitment
    assert proof.challenge
    assert proof.response
    assert len(proof.commitment) == 64  # SHA-256 hex
    assert "fitness_improvement_hash" in proof.public_inputs


# ═══════════════════════════════════════════════════════════════════════════════
# 4. FEDERATION ENGINE — blind aggregator
# ═══════════════════════════════════════════════════════════════════════════════

def test_blind_aggregator_empty():
    from orchestrator.core.federation_engine import BlindAggregator
    result = BlindAggregator.merge_deltas([])
    assert result["participating_instances"] == 0
    assert result["gene_improvements"] == {}


def test_blind_aggregator_single_delta():
    from orchestrator.core.federation_engine import BlindAggregator
    deltas = [{"gene_diffs": {"a": 0.5}, "fitness_improvement": 0.1}]
    result = BlindAggregator.merge_deltas(deltas)
    assert result["participating_instances"] == 1
    assert result["gene_improvements"]["a"] == 0.5


def test_blind_aggregator_multiple_deltas():
    from orchestrator.core.federation_engine import BlindAggregator
    deltas = [
        {"gene_diffs": {"a": 0.5, "b": 1.0}, "fitness_improvement": 0.1},
        {"gene_diffs": {"a": 0.3, "b": 0.8}, "fitness_improvement": 0.2},
        {"gene_diffs": {"a": 0.4, "c": 0.5}, "fitness_improvement": 0.15},
    ]
    result = BlindAggregator.merge_deltas(deltas)
    assert result["participating_instances"] == 3
    assert abs(result["gene_improvements"]["a"] - 0.4) < 0.01  # mean of 0.5, 0.3, 0.4
    assert result["confidence_score"] > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. FEDERATION ENGINE — encryption
# ═══════════════════════════════════════════════════════════════════════════════

def test_encrypt_delta_payload():
    from orchestrator.core.federation_engine import encrypt_delta_payload
    key = b"test-encryption-key-for-modus"
    plaintext = '{"gene_a": 0.5}'
    encrypted = encrypt_delta_payload(plaintext, key)
    assert isinstance(encrypted, bytes)
    assert len(encrypted) > len(plaintext)
    # Encrypted payload should differ from plaintext
    assert encrypted != plaintext.encode()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. FEDERATION ENGINE — _sha256_hex
# ═══════════════════════════════════════════════════════════════════════════════

def test_sha256_hex():
    from orchestrator.core.federation_engine import _sha256_hex
    h = _sha256_hex("hello", "world")
    assert len(h) == 64
    # Deterministic
    assert h == _sha256_hex("hello", "world")
    # Different input = different hash
    assert h != _sha256_hex("hello", "earth")


# ═══════════════════════════════════════════════════════════════════════════════
# 7. FEDERATION ENGINE — consent checkpoint
# ═══════════════════════════════════════════════════════════════════════════════

async def test_consent_has_no_consent(db_session):
    from orchestrator.core.federation_engine import ConsentCheckpoint
    result = await ConsentCheckpoint.has_consent(db_session)
    assert result is False


async def test_consent_record_and_check(db_session):
    from orchestrator.core.federation_engine import ConsentCheckpoint
    result = await ConsentCheckpoint.record_consent(
        db_session,
        participation_mode="passive",
        consented_by="admin-user",
        disclosure_text="I agree to anonymous data sharing.",
    )
    assert "consent_id" in result
    assert result["participation_mode"] == "passive"
    await db_session.commit()

    has = await ConsentCheckpoint.has_consent(db_session)
    assert has is True


async def test_consent_withdraw(db_session):
    from orchestrator.core.federation_engine import ConsentCheckpoint
    await ConsentCheckpoint.record_consent(
        db_session, "active", "admin", "disclosure text",
    )
    await db_session.commit()

    withdrawn = await ConsentCheckpoint.withdraw_consent(db_session)
    assert withdrawn is True
    await db_session.commit()

    has = await ConsentCheckpoint.has_consent(db_session)
    assert has is False


async def test_consent_withdraw_none(db_session):
    from orchestrator.core.federation_engine import ConsentCheckpoint
    withdrawn = await ConsentCheckpoint.withdraw_consent(db_session)
    assert withdrawn is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. POE LEDGER — hash_decision
# ═══════════════════════════════════════════════════════════════════════════════

def test_hash_decision():
    from orchestrator.core.poe_ledger import hash_decision
    h = hash_decision("allow", "ok", "app-1", 0.05)
    assert len(h) == 64  # SHA-256 hex
    # Deterministic
    assert h == hash_decision("allow", "ok", "app-1", 0.05)
    # Different inputs produce different hash
    assert h != hash_decision("deny", "budget", "app-1", 0.05)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. POE LEDGER — should_sample
# ═══════════════════════════════════════════════════════════════════════════════

def test_should_sample_disabled():
    from orchestrator.core.poe_ledger import should_sample
    from orchestrator.core.config import settings
    orig = settings.poe_ledger_enabled
    settings.poe_ledger_enabled = False
    assert should_sample("medium") is False
    settings.poe_ledger_enabled = orig


# ═══════════════════════════════════════════════════════════════════════════════
# 10. POE LEDGER — ChainEntry
# ═══════════════════════════════════════════════════════════════════════════════

def test_chain_entry_compute_hash():
    from orchestrator.core.poe_ledger import ChainEntry
    entry = ChainEntry(
        team_id="team-1",
        session_id="sess-1",
        seq_num=0,
        prev_hash=None,
        decision_hash="abc123",
        trajectory_proof_id=None,
        proof_type="hash_chain",
        risk_level="medium",
    )
    h = entry.compute_hash()
    assert len(h) == 64
    # Deterministic
    assert h == entry.compute_hash()


def test_chain_entry_with_prev_hash():
    from orchestrator.core.poe_ledger import ChainEntry
    entry = ChainEntry(
        team_id="team-1",
        session_id="sess-2",
        seq_num=1,
        prev_hash="deadbeef" * 8,
        decision_hash="abc123",
        trajectory_proof_id=None,
        proof_type="hash_chain",
        risk_level="high",
    )
    h = entry.compute_hash()
    assert len(h) == 64


# ═══════════════════════════════════════════════════════════════════════════════
# 11. POE LEDGER — cache
# ═══════════════════════════════════════════════════════════════════════════════

def test_poe_cache_update():
    from orchestrator.core.poe_ledger import _update_cache, _CHAIN_CACHE
    _CHAIN_CACHE.pop("test-team-cache", None)
    _update_cache("test-team-cache", 0, "hash-0")
    _update_cache("test-team-cache", 1, "hash-1")
    assert len(_CHAIN_CACHE["test-team-cache"]) == 2
    # Cleanup
    _CHAIN_CACHE.pop("test-team-cache", None)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. POE LEDGER — append_entry and verify_chain
# ═══════════════════════════════════════════════════════════════════════════════

async def test_append_entry_genesis(db_session):
    import hashlib
    from orchestrator.core.poe_ledger import append_entry, _CHAIN_CACHE
    team_id = str(uuid.uuid4())
    sess_id = str(uuid.uuid4())
    _CHAIN_CACHE.pop(team_id, None)
    dec_hash = hashlib.sha256(b"genesis-decision").hexdigest()
    result = await append_entry(
        db_session,
        team_id=team_id,
        session_id=sess_id,
        decision_hash=dec_hash,
        risk_level="medium",
    )
    assert result["seq_num"] == 0
    assert result["prev_hash"] is None
    assert len(result["entry_hash"]) == 64
    _CHAIN_CACHE.pop(team_id, None)


async def test_append_entry_chain(db_session):
    import hashlib
    from orchestrator.core.poe_ledger import append_entry, _CHAIN_CACHE
    team_id = str(uuid.uuid4())
    sess_id = str(uuid.uuid4())
    _CHAIN_CACHE.pop(team_id, None)
    h1 = hashlib.sha256(b"chain-decision-1").hexdigest()
    h2 = hashlib.sha256(b"chain-decision-2").hexdigest()
    r1 = await append_entry(
        db_session, team_id, sess_id, h1,
    )
    r2 = await append_entry(
        db_session, team_id, sess_id, h2,
    )
    assert r2["seq_num"] == 1
    assert r2["prev_hash"] == r1["entry_hash"]
    _CHAIN_CACHE.pop(team_id, None)


async def test_verify_chain_empty(db_session):
    from orchestrator.core.poe_ledger import verify_chain
    result = await verify_chain(db_session, "nonexistent-session")
    assert result["valid"] is True
    assert result["entries"] == 0


async def test_get_prev_hash_from_db(db_session):
    import hashlib
    from orchestrator.core.poe_ledger import (
        append_entry, get_prev_hash, _CHAIN_CACHE,
    )
    team_id = str(uuid.uuid4())
    sess_id = str(uuid.uuid4())
    _CHAIN_CACHE.pop(team_id, None)
    dec_hash = hashlib.sha256(b"test-decision").hexdigest()
    await append_entry(
        db_session, team_id, sess_id, dec_hash,
    )
    await db_session.flush()
    # Clear cache to force DB lookup
    _CHAIN_CACHE.pop(team_id, None)
    seq, prev = await get_prev_hash(db_session, team_id)
    assert seq == 1
    assert prev is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 13. POE LEDGER — sign_entry_slh_dsa
# ═══════════════════════════════════════════════════════════════════════════════

def test_sign_entry_slh_dsa():
    from orchestrator.core.poe_ledger import sign_entry_slh_dsa
    import hashlib
    entry_hash = hashlib.sha256(b"test").hexdigest()
    signer_key = b"test-key-material-at-least-16-bytes"
    sig = sign_entry_slh_dsa(entry_hash, signer_key)
    assert "algorithm" in sig
    assert "signature" in sig
    assert "signed_at" in sig


def test_sign_entry_slh_dsa_short_key():
    from orchestrator.core.poe_ledger import sign_entry_slh_dsa
    import pytest as _pt
    with _pt.raises(ValueError, match="signer_key"):
        sign_entry_slh_dsa("abc", b"short")


def test_aggregate_proofs():
    import hashlib
    from orchestrator.core.poe_ledger import aggregate_proofs
    h1 = hashlib.sha256(b"entry-1").hexdigest()
    h2 = hashlib.sha256(b"entry-2").hexdigest()
    entries = [
        {"entry_hash": h1, "seq_num": 0},
        {"entry_hash": h2, "seq_num": 1},
    ]
    signer_key = b"signing-key-data-at-least-16-bytes"
    result = aggregate_proofs(entries, signer_key)
    assert "aggregate_hash" in result
    assert result["entry_count"] == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 14. POLICY GENOME — Gene types
# ═══════════════════════════════════════════════════════════════════════════════

def test_float_gene():
    from orchestrator.core.policy_genome import FloatGene
    g = FloatGene("cap_usd", 100.0, 10.0, 500.0)
    assert g.value() == 100.0
    assert g.name == "cap_usd"
    d = g.to_dict()
    assert d["type"] == "float"
    assert d["value"] == 100.0
    clone = g.clone()
    assert clone.value() == g.value()
    assert clone is not g


def test_float_gene_clamped():
    from orchestrator.core.policy_genome import FloatGene
    g = FloatGene("test", 600.0, 10.0, 500.0)
    assert g.value() == 500.0  # clamped to max
    g2 = FloatGene("test", -5.0, 10.0, 500.0)
    assert g2.value() == 10.0  # clamped to min


def test_float_gene_mutate():
    from orchestrator.core.policy_genome import FloatGene
    g = FloatGene("cap", 100.0, 10.0, 500.0)
    rng = random.Random(42)
    mutated = g.mutate(rng)
    assert mutated.value() >= 10.0
    assert mutated.value() <= 500.0


def test_int_gene():
    from orchestrator.core.policy_genome import IntGene
    g = IntGene("max_calls", 100, 1, 1000)
    assert g.value() == 100
    d = g.to_dict()
    assert d["type"] == "int"
    mutated = g.mutate(random.Random(42))
    assert mutated.value() >= 1
    assert mutated.value() <= 1000


def test_int_gene_clamped():
    from orchestrator.core.policy_genome import IntGene
    g = IntGene("test", 2000, 1, 1000)
    assert g.value() == 1000


def test_categorical_gene():
    from orchestrator.core.policy_genome import CategoricalGene
    g = CategoricalGene("effect", "deny", ["deny", "throttle", "warn"])
    assert g.value() == "deny"
    d = g.to_dict()
    assert d["type"] == "categorical"
    assert d["options"] == ["deny", "throttle", "warn"]
    mutated = g.mutate(random.Random(42))
    assert mutated.value() in ["throttle", "warn"]  # not "deny"


def test_categorical_gene_invalid_value():
    from orchestrator.core.policy_genome import CategoricalGene
    g = CategoricalGene("effect", "invalid", ["deny", "throttle"])
    assert g.value() == "deny"  # falls back to first option


def test_categorical_gene_single_option():
    from orchestrator.core.policy_genome import CategoricalGene
    g = CategoricalGene("x", "only", ["only"])
    mutated = g.mutate(random.Random(42))
    assert mutated.value() == "only"


def test_categorical_gene_no_options_raises():
    from orchestrator.core.policy_genome import CategoricalGene
    with pytest.raises(ValueError):
        CategoricalGene("x", "val", [])


def test_bool_gene():
    from orchestrator.core.policy_genome import BoolGene
    g = BoolGene("enabled", True)
    assert g.value() is True
    d = g.to_dict()
    assert d["type"] == "bool"
    mutated = g.mutate(random.Random(42))
    assert mutated.value() is False


def test_gene_repr():
    from orchestrator.core.policy_genome import FloatGene, IntGene
    fg = FloatGene("cap", 100.0, 0.0, 500.0)
    assert "FloatGene" in repr(fg)
    ig = IntGene("calls", 50, 0, 100)
    assert "IntGene" in repr(ig)


# ═══════════════════════════════════════════════════════════════════════════════
# 15. POLICY GENOME — gene extractors
# ═══════════════════════════════════════════════════════════════════════════════

def test_extract_budget_cap_genes():
    from orchestrator.core.policy_genome import _extract_budget_cap_genes
    genes = _extract_budget_cap_genes({"cap_usd": 100}, "p0.budget_cap")
    assert len(genes) == 1
    assert genes[0].name == "p0.budget_cap.cap_usd"
    assert genes[0].value() == 100.0


def test_extract_rate_limit_genes():
    from orchestrator.core.policy_genome import _extract_rate_limit_genes
    genes = _extract_rate_limit_genes(
        {"max_calls": 200, "window_seconds": 3600}, "p0.rate_limit",
    )
    assert len(genes) == 2


def test_extract_token_cap_genes():
    from orchestrator.core.policy_genome import _extract_token_cap_genes
    genes = _extract_token_cap_genes({"max_tokens": 5000}, "p0.token_cap")
    assert len(genes) == 1


def test_extract_amplification_gate_genes():
    from orchestrator.core.policy_genome import _extract_amplification_gate_genes
    genes = _extract_amplification_gate_genes(
        {"max_amplification": 5.0}, "p0.amp",
    )
    assert len(genes) == 1


def test_extract_degradation_ladder_genes():
    from orchestrator.core.policy_genome import _extract_degradation_ladder_genes
    genes = _extract_degradation_ladder_genes({
        "budget_usd": 500,
        "tiers": [
            {"pct": 80, "model": "gpt-4o-mini"},
            {"pct": 100, "action": "deny"},
        ],
    }, "p0.ladder")
    assert len(genes) >= 3  # budget + 2 tier pcts + model + action


def test_extract_model_list_genes():
    from orchestrator.core.policy_genome import _extract_model_list_genes
    genes = _extract_model_list_genes(
        {"models": ["gpt-4o", "gpt-4-turbo"]}, "p0.denylist", "model_denylist",
    )
    assert len(genes) >= 3  # 2 models + 1 enabled flag


def test_extract_pqc_migration_genes():
    from orchestrator.core.policy_genome import _extract_pqc_migration_genes
    genes = _extract_pqc_migration_genes({
        "stage": "hybrid",
        "enforcement": "warn",
        "deadline_days": 120,
    }, "p0.pqc")
    assert len(genes) == 3


def test_extract_pqc_migration_genes_defaults():
    from orchestrator.core.policy_genome import _extract_pqc_migration_genes
    genes = _extract_pqc_migration_genes({}, "p0.pqc")
    assert len(genes) == 3
    # Should use defaults
    stage_gene = [g for g in genes if "stage" in g.name][0]
    assert stage_gene.value() == "assessment"


# ═══════════════════════════════════════════════════════════════════════════════
# 16. POLICY GENOME — from_yaml / to_yaml / crossover / mutate / diff
# ═══════════════════════════════════════════════════════════════════════════════

def test_genome_from_yaml_budget_cap():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: budget_cap
  effect: deny
  config:
    cap_usd: 500
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    assert len(genome.genes) >= 2  # effect + cap_usd


def test_genome_from_yaml_rate_limit():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: rate_limit
  config:
    max_calls: 100
    window_seconds: 3600
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    assert len(genome.genes) >= 2


def test_genome_from_yaml_wrapper_format():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
policies:
  - type: budget_cap
    effect: deny
    config:
      cap_usd: 200
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    assert len(genome.genes) >= 1


def test_genome_from_yaml_invalid():
    from orchestrator.core.policy_genome import PolicyGenome
    genome = PolicyGenome.from_yaml("not: [valid: yaml: {{")
    assert len(genome.genes) == 0


def test_genome_from_yaml_empty_list():
    from orchestrator.core.policy_genome import PolicyGenome
    genome = PolicyGenome.from_yaml("[]")
    assert len(genome.genes) == 0


def test_genome_to_yaml():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: budget_cap
  effect: deny
  config:
    cap_usd: 500
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    output = genome.to_yaml()
    assert "budget_cap" in output
    assert "deny" in output


def test_genome_to_yaml_empty():
    from orchestrator.core.policy_genome import PolicyGenome
    genome = PolicyGenome(genes=[], fitness=0.0)
    assert genome.to_yaml() == ""


def test_genome_crossover():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml1 = """
- type: budget_cap
  effect: deny
  config:
    cap_usd: 100
"""
    yaml2 = """
- type: budget_cap
  effect: warn
  config:
    cap_usd: 500
"""
    g1 = PolicyGenome.from_yaml(yaml1)
    g2 = PolicyGenome.from_yaml(yaml2)
    child = g1.crossover(g2, crossover_rate=0.5, rng=random.Random(42))
    assert len(child.genes) == max(len(g1.genes), len(g2.genes))
    assert child.fitness == 0.0  # reset


def test_genome_mutate():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: rate_limit
  config:
    max_calls: 100
    window_seconds: 3600
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    mutated = genome.mutate(mutation_rate=1.0, rng=random.Random(42))
    assert len(mutated.genes) == len(genome.genes)
    # At least one gene should have changed
    any_changed = False
    for g1, g2 in zip(genome.genes, mutated.genes):
        if g1.value() != g2.value():
            any_changed = True
    assert any_changed


def test_genome_clone():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: budget_cap
  effect: deny
  config:
    cap_usd: 250
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    genome.fitness = 0.85
    cloned = genome.clone()
    assert cloned.fitness == 0.85
    assert len(cloned.genes) == len(genome.genes)
    # Independent copies
    assert cloned is not genome


def test_genome_diff():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml1 = """
- type: budget_cap
  effect: deny
  config:
    cap_usd: 100
"""
    yaml2 = """
- type: budget_cap
  effect: warn
  config:
    cap_usd: 200
"""
    g1 = PolicyGenome.from_yaml(yaml1)
    g2 = PolicyGenome.from_yaml(yaml2)
    diff_str = g1.diff(g2)
    assert "Constitutional Diff" in diff_str
    assert "~" in diff_str  # changed genes


def test_genome_diff_no_changes():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: budget_cap
  effect: deny
  config:
    cap_usd: 100
"""
    g = PolicyGenome.from_yaml(yaml_str)
    diff_str = g.diff(g)
    assert "no changes" in diff_str


# ═══════════════════════════════════════════════════════════════════════════════
# 17. POLICY GENOME — unknown policy types (generic extraction)
# ═══════════════════════════════════════════════════════════════════════════════

def test_genome_unknown_policy_type():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: custom_policy
  config:
    threshold: 50
    enabled: true
    ratio: 0.75
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    # Should extract generic genes: int, bool, float
    assert len(genome.genes) >= 3


# ═══════════════════════════════════════════════════════════════════════════════
# 18. CONSTITUTIONAL ENGINE — _tournament_select
# ═══════════════════════════════════════════════════════════════════════════════

def test_tournament_select():
    from orchestrator.core.constitutional_engine import _tournament_select
    from orchestrator.core.policy_genome import PolicyGenome, FloatGene

    pop = []
    for i in range(10):
        g = PolicyGenome(
            genes=[FloatGene("x", float(i), 0.0, 10.0)],
            fitness=float(i) / 10.0,
        )
        pop.append(g)

    rng = random.Random(42)
    winner = _tournament_select(pop, tournament_size=3, rng=rng)
    # Winner should be one from the population
    assert winner.fitness >= 0


def test_tournament_select_small_pop():
    from orchestrator.core.constitutional_engine import _tournament_select
    from orchestrator.core.policy_genome import PolicyGenome, FloatGene

    pop = [PolicyGenome(genes=[FloatGene("x", 1.0, 0.0, 10.0)], fitness=0.5)]
    rng = random.Random(42)
    winner = _tournament_select(pop, tournament_size=3, rng=rng)
    assert winner.fitness == 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# 19. CONSTITUTIONAL ENGINE — EvolutionResult
# ═══════════════════════════════════════════════════════════════════════════════

def test_evolution_result():
    from orchestrator.core.constitutional_engine import EvolutionResult
    from orchestrator.core.policy_genome import PolicyGenome, FloatGene
    genome = PolicyGenome(genes=[FloatGene("x", 1.0, 0.0, 10.0)], fitness=0.92)
    result = EvolutionResult(
        best_genome=genome,
        best_fitness=0.92,
        avg_fitness=0.75,
        generations_run=5,
        population_size=20,
        constitutional_diff="=== Constitutional Diff ===\n  (no changes)",
        prover_result=None,
        elapsed_ms=1500,
    )
    assert result.best_fitness == 0.92
    assert result.generations_run == 5
    d = result.to_dict()
    assert d["best_fitness"] == 0.92
    assert d["elapsed_ms"] == 1500


# ═══════════════════════════════════════════════════════════════════════════════
# 20. POLICY GENOME — to_yaml for various types
# ═══════════════════════════════════════════════════════════════════════════════

def test_genome_to_yaml_rate_limit():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: rate_limit
  config:
    max_calls: 100
    window_seconds: 3600
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    output = genome.to_yaml()
    assert "rate_limit" in output
    assert "max_calls" in output


def test_genome_to_yaml_degradation_ladder():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: degradation_ladder
  config:
    budget_usd: 1000
    tiers:
      - pct: 80
        model: gpt-4o-mini
      - pct: 100
        action: deny
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    output = genome.to_yaml()
    assert "degradation_ladder" in output
    assert "tiers" in output


def test_genome_to_yaml_pqc_migration():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: pqc_migration
  config:
    stage: hybrid
    enforcement: warn
    deadline_days: 120
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    output = genome.to_yaml()
    assert "pqc_migration" in output


def test_genome_to_yaml_model_denylist():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: model_denylist
  config:
    models:
      - gpt-4o
      - gpt-4-turbo
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    output = genome.to_yaml()
    assert "model_denylist" in output


def test_genome_to_yaml_amplification_gate():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: amplification_gate
  config:
    max_amplification: 5.0
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    output = genome.to_yaml()
    assert "amplification_gate" in output


def test_genome_to_yaml_token_cap():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml_str = """
- type: token_cap
  config:
    max_tokens: 10000
"""
    genome = PolicyGenome.from_yaml(yaml_str)
    output = genome.to_yaml()
    assert "token_cap" in output


def test_genome_crossover_unequal_lengths():
    from orchestrator.core.policy_genome import PolicyGenome
    yaml1 = """
- type: budget_cap
  effect: deny
  config:
    cap_usd: 100
- type: rate_limit
  config:
    max_calls: 50
    window_seconds: 1800
"""
    yaml2 = """
- type: budget_cap
  effect: warn
  config:
    cap_usd: 200
"""
    g1 = PolicyGenome.from_yaml(yaml1)
    g2 = PolicyGenome.from_yaml(yaml2)
    child = g1.crossover(g2, rng=random.Random(42))
    # Child should have genes from the longer parent
    assert len(child.genes) == max(len(g1.genes), len(g2.genes))
