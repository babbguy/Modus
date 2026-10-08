"""
Modus — Policy Genome Representation (Phase 8d)
=====================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Policy genome representation for evolutionary optimization of governance
policies. Encodes policy parameters as typed genes that support crossover,
mutation, and serialization to/from YAML policy format.

Gene types:
  - FloatGene   — continuous parameters (budget caps, thresholds)
  - IntGene     — discrete parameters (rate limits, counts)
  - CategoricalGene — enum parameters (model names, effects)
  - BoolGene    — toggle parameters (enable/disable flags)

The genome is the atomic unit of the constitutional evolution engine.
Genomes can be crossed, mutated, evaluated, and compared. The diff()
method produces human-readable constitutional diffs for proposal review.

All computation runs locally. No data leaves the customer's
infrastructure. Pure Python stdlib — no external dependencies.
"""

from __future__ import annotations

import copy
import logging
import random
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# YAML is used throughout the project for policy serialization.
try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# ── Gene base and concrete types ────────────────────────────────────────────

class Gene(ABC):
    """Abstract base for a single gene in a policy genome."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def value(self) -> Any:
        """Return the current gene value."""

    @abstractmethod
    def mutate(self, rng: random.Random) -> Gene:
        """Return a new Gene with a mutated value."""

    @abstractmethod
    def clone(self) -> Gene:
        """Return a deep copy of this gene."""

    @abstractmethod
    def to_dict(self) -> dict:
        """Serialize gene to a dict for debugging/logging."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}={self.value()!r}>"


class FloatGene(Gene):
    """Continuous gene for budget caps, cost thresholds, etc."""

    __slots__ = ("_value", "min_val", "max_val")

    def __init__(
        self,
        name: str,
        value: float,
        min_val: float,
        max_val: float,
    ) -> None:
        super().__init__(name)
        self._value = max(min_val, min(max_val, float(value)))
        self.min_val = float(min_val)
        self.max_val = float(max_val)

    def value(self) -> float:
        return self._value

    def mutate(self, rng: random.Random) -> FloatGene:
        """Gaussian perturbation within bounds (sigma = 10% of range)."""
        spread = (self.max_val - self.min_val) * 0.1
        new_val = rng.gauss(self._value, spread)
        new_val = max(self.min_val, min(self.max_val, new_val))
        return FloatGene(self.name, new_val, self.min_val, self.max_val)

    def clone(self) -> FloatGene:
        return FloatGene(self.name, self._value, self.min_val, self.max_val)

    def to_dict(self) -> dict:
        return {
            "type": "float",
            "name": self.name,
            "value": round(self._value, 6),
            "min": self.min_val,
            "max": self.max_val,
        }


class IntGene(Gene):
    """Discrete gene for rate limits, call counts, token caps, etc."""

    __slots__ = ("_value", "min_val", "max_val")

    def __init__(
        self,
        name: str,
        value: int,
        min_val: int,
        max_val: int,
    ) -> None:
        super().__init__(name)
        self._value = max(min_val, min(max_val, int(value)))
        self.min_val = int(min_val)
        self.max_val = int(max_val)

    def value(self) -> int:
        return self._value

    def mutate(self, rng: random.Random) -> IntGene:
        """Random integer within +/-20% of range around current value."""
        spread = max(1, int((self.max_val - self.min_val) * 0.2))
        delta = rng.randint(-spread, spread)
        new_val = max(self.min_val, min(self.max_val, self._value + delta))
        return IntGene(self.name, new_val, self.min_val, self.max_val)

    def clone(self) -> IntGene:
        return IntGene(self.name, self._value, self.min_val, self.max_val)

    def to_dict(self) -> dict:
        return {
            "type": "int",
            "name": self.name,
            "value": self._value,
            "min": self.min_val,
            "max": self.max_val,
        }


class CategoricalGene(Gene):
    """Enumerated gene for model names, effect types, etc."""

    __slots__ = ("_value", "options")

    def __init__(
        self,
        name: str,
        value: str,
        options: List[str],
    ) -> None:
        super().__init__(name)
        if not options:
            raise ValueError(f"CategoricalGene '{name}' requires at least one option")
        self.options = list(options)
        self._value = value if value in self.options else self.options[0]

    def value(self) -> str:
        return self._value

    def mutate(self, rng: random.Random) -> CategoricalGene:
        """Uniform random selection from options (excluding current)."""
        if len(self.options) <= 1:
            return self.clone()
        candidates = [o for o in self.options if o != self._value]
        new_val = rng.choice(candidates)
        return CategoricalGene(self.name, new_val, self.options)

    def clone(self) -> CategoricalGene:
        return CategoricalGene(self.name, self._value, list(self.options))

    def to_dict(self) -> dict:
        return {
            "type": "categorical",
            "name": self.name,
            "value": self._value,
            "options": self.options,
        }


class BoolGene(Gene):
    """Boolean gene for enable/disable flags."""

    __slots__ = ("_value",)

    def __init__(self, name: str, value: bool) -> None:
        super().__init__(name)
        self._value = bool(value)

    def value(self) -> bool:
        return self._value

    def mutate(self, rng: random.Random) -> BoolGene:
        """Flip with 50% probability."""
        return BoolGene(self.name, not self._value)

    def clone(self) -> BoolGene:
        return BoolGene(self.name, self._value)

    def to_dict(self) -> dict:
        return {"type": "bool", "name": self.name, "value": self._value}


# ── Policy type → gene extraction ───────────────────────────────────────────

_KNOWN_MODELS = [
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "claude-opus-4-5-20251022", "claude-sonnet-4-20250514",
    "claude-haiku-4-5-20251001",
    "gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-pro", "gemini-2.0-flash",
    "o1", "o1-mini", "o3", "o3-mini",
]

_KNOWN_EFFECTS = ["deny", "throttle", "warn"]


def _extract_budget_cap_genes(config: dict, prefix: str) -> List[Gene]:
    """Extract genes from a budget_cap policy config."""
    cap = float(config.get("cap_usd", 100))
    return [
        FloatGene(f"{prefix}.cap_usd", cap, max(0.01, cap * 0.1), cap * 5.0),
    ]


def _extract_rate_limit_genes(config: dict, prefix: str) -> List[Gene]:
    """Extract genes from a rate_limit policy config."""
    max_calls = int(config.get("max_calls", 100))
    window = int(config.get("window_seconds", 3600))
    return [
        IntGene(f"{prefix}.max_calls", max_calls, max(1, max_calls // 10), max_calls * 5),
        IntGene(f"{prefix}.window_seconds", window, 60, 86400),
    ]


def _extract_token_cap_genes(config: dict, prefix: str) -> List[Gene]:
    """Extract genes from a token_cap policy config."""
    max_tokens = int(config.get("max_tokens", 10000))
    return [
        IntGene(f"{prefix}.max_tokens", max_tokens, max(100, max_tokens // 10), max_tokens * 5),
    ]


def _extract_amplification_gate_genes(config: dict, prefix: str) -> List[Gene]:
    """Extract genes from an amplification_gate policy config."""
    max_amp = float(config.get("max_amplification", 5.0))
    return [
        FloatGene(f"{prefix}.max_amplification", max_amp, 1.0, max_amp * 3.0),
    ]


def _extract_degradation_ladder_genes(config: dict, prefix: str) -> List[Gene]:
    """Extract genes from a degradation_ladder policy config."""
    budget = float(config.get("budget_usd", 500))
    genes: List[Gene] = [
        FloatGene(f"{prefix}.budget_usd", budget, max(1.0, budget * 0.1), budget * 5.0),
    ]
    tiers = config.get("tiers", [])
    for i, tier in enumerate(tiers):
        pct = float(tier.get("pct", 50))
        genes.append(
            FloatGene(f"{prefix}.tier_{i}.pct", pct, 10.0, 100.0)
        )
        if "model" in tier:
            genes.append(
                CategoricalGene(f"{prefix}.tier_{i}.model", tier["model"], _KNOWN_MODELS)
            )
        if "action" in tier:
            genes.append(
                CategoricalGene(f"{prefix}.tier_{i}.action", tier["action"], ["deny", "throttle", "warn"])
            )
    return genes


def _extract_model_list_genes(config: dict, prefix: str, policy_type: str) -> List[Gene]:
    """Extract genes from model_denylist or model_allowlist policies."""
    models = config.get("models", [])
    genes: List[Gene] = []
    for i, model in enumerate(models):
        genes.append(
            CategoricalGene(f"{prefix}.model_{i}", model, _KNOWN_MODELS)
        )
    # Enable/disable flag for the list itself
    genes.append(BoolGene(f"{prefix}.enabled", True))
    return genes


_PQC_MIGRATION_STAGES = [
    "inventory", "assessment", "planning", "hybrid", "full_pqc", "verification",
]

_PQC_ENFORCEMENT_OPTIONS = ["warn", "block"]


def _extract_pqc_migration_genes(config: dict, prefix: str) -> List[Gene]:
    """Extract genes from a pqc_migration policy config."""
    genes: List[Gene] = []

    stage = config.get("stage", "assessment")
    if not isinstance(stage, str) or stage not in _PQC_MIGRATION_STAGES:
        stage = "assessment"
    genes.append(
        CategoricalGene(
            f"{prefix}.pqc_migration_stage",
            stage,
            _PQC_MIGRATION_STAGES,
        )
    )

    enforcement = config.get("enforcement", "warn")
    if not isinstance(enforcement, str) or enforcement not in _PQC_ENFORCEMENT_OPTIONS:
        enforcement = "warn"
    genes.append(
        CategoricalGene(
            f"{prefix}.pqc_enforcement",
            enforcement,
            _PQC_ENFORCEMENT_OPTIONS,
        )
    )

    deadline = config.get("deadline_days", 90)
    try:
        deadline = int(deadline)
    except (TypeError, ValueError):
        deadline = 90
    deadline = max(30, min(365, deadline))
    genes.append(
        IntGene(f"{prefix}.pqc_deadline_days", deadline, 30, 365)
    )

    return genes


_GENE_EXTRACTORS = {
    "budget_cap": _extract_budget_cap_genes,
    "rate_limit": _extract_rate_limit_genes,
    "token_cap": _extract_token_cap_genes,
    "amplification_gate": _extract_amplification_gate_genes,
    "degradation_ladder": _extract_degradation_ladder_genes,
    "pqc_migration": _extract_pqc_migration_genes,
}


# ── PolicyGenome ─────────────────────────────────────────────────────────────

class PolicyGenome:
    """
    Genome representation of one or more governance policies.

    A genome is an ordered list of typed genes extracted from YAML policy
    definitions. It supports genetic operations (crossover, mutation) and
    can be serialized back to YAML for deployment as governance policies.
    """

    __slots__ = ("genes", "fitness", "_policy_templates")

    def __init__(
        self,
        genes: List[Gene],
        fitness: float = 0.0,
        policy_templates: Optional[List[dict]] = None,
    ) -> None:
        self.genes = genes
        self.fitness = fitness
        # Store the original policy structure for to_yaml reconstruction
        self._policy_templates = policy_templates or []

    # ── Factory: parse YAML into genome ──────────────────────────────────

    @classmethod
    def from_yaml(cls, yaml_str: str) -> PolicyGenome:
        """
        Parse a YAML policy string into a PolicyGenome.

        Supports single policy dicts, lists of policies, and the
        ``{policies: [...]}`` wrapper format used in GovernancePolicy.
        """
        if yaml is None:
            raise ImportError("PyYAML is required for genome YAML parsing")

        try:
            parsed = yaml.safe_load(yaml_str)
        except Exception as exc:
            logger.warning("Failed to parse policy YAML: %s", exc)
            return cls(genes=[], fitness=0.0)

        # Normalize to list
        policies: List[dict] = []
        if isinstance(parsed, dict):
            if "policies" in parsed and isinstance(parsed["policies"], list):
                policies = [p for p in parsed["policies"] if isinstance(p, dict)]
            else:
                policies = [parsed]
        elif isinstance(parsed, list):
            policies = [p for p in parsed if isinstance(p, dict)]

        if not policies:
            return cls(genes=[], fitness=0.0)

        all_genes: List[Gene] = []
        templates: List[dict] = []

        for idx, policy in enumerate(policies):
            ptype = policy.get("type", "")
            config = policy.get("config", {}) or {}
            prefix = f"p{idx}.{ptype}"

            # Store template for reconstruction
            templates.append(copy.deepcopy(policy))

            # Add effect gene if present
            effect = policy.get("effect", "")
            if effect:
                all_genes.append(
                    CategoricalGene(f"{prefix}.effect", effect, _KNOWN_EFFECTS)
                )

            # Type-specific gene extraction
            if ptype in ("model_denylist", "model_allowlist"):
                all_genes.extend(_extract_model_list_genes(config, prefix, ptype))
            elif ptype in _GENE_EXTRACTORS:
                all_genes.extend(_GENE_EXTRACTORS[ptype](config, prefix))
            else:
                # Unknown policy type — extract what we can generically
                for key, val in config.items():
                    if isinstance(val, bool):
                        all_genes.append(BoolGene(f"{prefix}.{key}", val))
                    elif isinstance(val, int):
                        all_genes.append(
                            IntGene(f"{prefix}.{key}", val, 0, max(val * 5, 1))
                        )
                    elif isinstance(val, float):
                        all_genes.append(
                            FloatGene(f"{prefix}.{key}", val, 0.0, max(val * 5.0, 1.0))
                        )

        return cls(genes=all_genes, fitness=0.0, policy_templates=templates)

    # ── Serialize genome back to YAML ────────────────────────────────────

    def to_yaml(self) -> str:
        """
        Reconstruct YAML policy text from the genome's current gene values.

        Uses the stored policy templates and overlays the evolved gene values
        onto the original structure.
        """
        if yaml is None:
            raise ImportError("PyYAML is required for genome YAML serialization")

        if not self._policy_templates:
            return ""

        # Build gene lookup: name → value
        gene_map: Dict[str, Any] = {g.name: g.value() for g in self.genes}

        policies = copy.deepcopy(self._policy_templates)

        for idx, policy in enumerate(policies):
            ptype = policy.get("type", "")
            prefix = f"p{idx}.{ptype}"
            config = policy.get("config", {}) or {}

            # Update effect if gene exists
            effect_key = f"{prefix}.effect"
            if effect_key in gene_map:
                policy["effect"] = gene_map[effect_key]

            # Update config values from genes
            if ptype == "budget_cap":
                cap_key = f"{prefix}.cap_usd"
                if cap_key in gene_map:
                    config["cap_usd"] = str(round(gene_map[cap_key], 2))

            elif ptype == "rate_limit":
                calls_key = f"{prefix}.max_calls"
                window_key = f"{prefix}.window_seconds"
                if calls_key in gene_map:
                    config["max_calls"] = gene_map[calls_key]
                if window_key in gene_map:
                    config["window_seconds"] = gene_map[window_key]

            elif ptype == "token_cap":
                tokens_key = f"{prefix}.max_tokens"
                if tokens_key in gene_map:
                    config["max_tokens"] = gene_map[tokens_key]

            elif ptype == "amplification_gate":
                amp_key = f"{prefix}.max_amplification"
                if amp_key in gene_map:
                    config["max_amplification"] = round(gene_map[amp_key], 2)

            elif ptype == "degradation_ladder":
                budget_key = f"{prefix}.budget_usd"
                if budget_key in gene_map:
                    config["budget_usd"] = str(round(gene_map[budget_key], 2))
                tiers = config.get("tiers", [])
                for i, tier in enumerate(tiers):
                    pct_key = f"{prefix}.tier_{i}.pct"
                    model_key = f"{prefix}.tier_{i}.model"
                    action_key = f"{prefix}.tier_{i}.action"
                    if pct_key in gene_map:
                        tier["pct"] = round(gene_map[pct_key], 1)
                    if model_key in gene_map:
                        tier["model"] = gene_map[model_key]
                    if action_key in gene_map:
                        tier["action"] = gene_map[action_key]

            elif ptype == "pqc_migration":
                stage_key = f"{prefix}.pqc_migration_stage"
                enf_key = f"{prefix}.pqc_enforcement"
                dl_key = f"{prefix}.pqc_deadline_days"
                if stage_key in gene_map:
                    config["stage"] = gene_map[stage_key]
                if enf_key in gene_map:
                    config["enforcement"] = gene_map[enf_key]
                if dl_key in gene_map:
                    config["deadline_days"] = gene_map[dl_key]

            elif ptype in ("model_denylist", "model_allowlist"):
                models = config.get("models", [])
                for i in range(len(models)):
                    model_key = f"{prefix}.model_{i}"
                    if model_key in gene_map:
                        models[i] = gene_map[model_key]
                config["models"] = models

            else:
                # Generic: overlay known config keys
                for key in list(config.keys()):
                    gene_key = f"{prefix}.{key}"
                    if gene_key in gene_map:
                        config[key] = gene_map[gene_key]

            policy["config"] = config

        return yaml.dump(policies, default_flow_style=False, sort_keys=False)

    # ── Genetic operators ────────────────────────────────────────────────

    def crossover(
        self,
        other: PolicyGenome,
        crossover_rate: float = 0.7,
        rng: Optional[random.Random] = None,
    ) -> PolicyGenome:
        """
        Uniform crossover: for each gene position, randomly select from
        self or other with probability crossover_rate.

        If genomes have different lengths (different policy structures),
        excess genes come from the longer parent.
        """
        if rng is None:
            rng = random.Random()

        child_genes: List[Gene] = []
        max_len = max(len(self.genes), len(other.genes))

        for i in range(max_len):
            if i < len(self.genes) and i < len(other.genes):
                # Both parents have this gene — crossover
                if rng.random() < crossover_rate:
                    child_genes.append(other.genes[i].clone())
                else:
                    child_genes.append(self.genes[i].clone())
            elif i < len(self.genes):
                child_genes.append(self.genes[i].clone())
            else:
                child_genes.append(other.genes[i].clone())

        # Merge templates: prefer self's structure
        templates = copy.deepcopy(self._policy_templates) if self._policy_templates else \
            copy.deepcopy(other._policy_templates)

        return PolicyGenome(genes=child_genes, fitness=0.0, policy_templates=templates)

    def mutate(
        self,
        mutation_rate: float = 0.1,
        rng: Optional[random.Random] = None,
    ) -> PolicyGenome:
        """
        Per-gene mutation: each gene is independently mutated with
        probability mutation_rate.

        Returns a new PolicyGenome — does not modify self.
        """
        if rng is None:
            rng = random.Random()

        mutated_genes: List[Gene] = []
        for gene in self.genes:
            if rng.random() < mutation_rate:
                mutated_genes.append(gene.mutate(rng))
            else:
                mutated_genes.append(gene.clone())

        return PolicyGenome(
            genes=mutated_genes,
            fitness=0.0,
            policy_templates=copy.deepcopy(self._policy_templates),
        )

    def clone(self) -> PolicyGenome:
        """Return a deep copy of this genome."""
        return PolicyGenome(
            genes=[g.clone() for g in self.genes],
            fitness=self.fitness,
            policy_templates=copy.deepcopy(self._policy_templates),
        )

    # ── Comparison & diff ────────────────────────────────────────────────

    def diff(self, other: PolicyGenome) -> str:
        """
        Produce a human-readable constitutional diff between this genome
        and another. Shows gene-by-gene changes in a compact format.

        Returns a multi-line string suitable for proposal review.
        """
        lines: List[str] = []
        lines.append("=== Constitutional Diff ===")
        lines.append("")

        # Build gene maps for both genomes
        self_map = {g.name: g for g in self.genes}
        other_map = {g.name: g for g in other.genes}

        all_names = list(dict.fromkeys(
            [g.name for g in self.genes] + [g.name for g in other.genes]
        ))

        changes = 0
        for name in all_names:
            sg = self_map.get(name)
            og = other_map.get(name)

            if sg is not None and og is not None:
                sv = sg.value()
                ov = og.value()
                if sv != ov:
                    changes += 1
                    if isinstance(sg, FloatGene):
                        delta = ov - sv  # type: ignore[operator]
                        sign = "+" if delta > 0 else ""
                        lines.append(
                            f"  ~ {name}: {sv:.4f} -> {ov:.4f} ({sign}{delta:.4f})"
                        )
                    elif isinstance(sg, IntGene):
                        delta = ov - sv  # type: ignore[operator]
                        sign = "+" if delta > 0 else ""
                        lines.append(
                            f"  ~ {name}: {sv} -> {ov} ({sign}{delta})"
                        )
                    else:
                        lines.append(f"  ~ {name}: {sv!r} -> {ov!r}")
            elif sg is None and og is not None:
                changes += 1
                lines.append(f"  + {name}: {og.value()!r}")
            elif sg is not None and og is None:
                changes += 1
                lines.append(f"  - {name}: {sg.value()!r}")

        if changes == 0:
            lines.append("  (no changes)")

        lines.append("")
        lines.append(f"Total changes: {changes} / {len(all_names)} genes")
        return "\n".join(lines)

    # ── Utilities ────────────────────────────────────────────────────────

    def gene_count(self) -> int:
        """Return the number of genes in this genome."""
        return len(self.genes)

    def get_gene(self, name: str) -> Optional[Gene]:
        """Look up a gene by name. Returns None if not found."""
        for g in self.genes:
            if g.name == name:
                return g
        return None

    def summary(self) -> Dict[str, Any]:
        """Return a compact summary dict for logging/storage."""
        return {
            "gene_count": len(self.genes),
            "fitness": round(self.fitness, 6),
            "policy_count": len(self._policy_templates),
            "genes": [g.to_dict() for g in self.genes],
        }

    def __repr__(self) -> str:
        return (
            f"<PolicyGenome genes={len(self.genes)} "
            f"fitness={self.fitness:.4f} "
            f"policies={len(self._policy_templates)}>"
        )

    def __len__(self) -> int:
        return len(self.genes)
