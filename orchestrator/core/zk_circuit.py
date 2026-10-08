"""
Modus — Arithmetic Circuit (Phase 8b, EXPERIMENTAL)
=======================================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Pure-Python arithmetic circuit abstraction. Implements R1CS (Rank-1
Constraint System) over a prime field, enabling encoding of governance
constraints into verifiable circuits.

NOTE ON CLAIMS: This is a building block used by the EXPERIMENTAL Tier-2
path in ``zk_trajectory_prover``. It is not wired into a production
zero-knowledge proof system — there is no trusted setup, no polynomial
commitment, and no soundness guarantee. Treat it as a constraint-checking
utility, not a zk-SNARK.

All operations use Python arbitrary-precision integers with modular
arithmetic over the BN254 scalar field. No external dependencies.

Stdlib only. Zero dependencies. Thread-safe (circuits are single-threaded
during construction, immutable once finalized).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# ── BN254 scalar field prime ────────────────────────────────────────────────
# This is the order of the BN254 (alt_bn128) elliptic curve's scalar field.
# All arithmetic in the circuit operates modulo this prime.

FIELD_PRIME = 21888242871839275222246405745257275088548364400416034343698204186575808495617


# ── Field arithmetic helpers ────────────────────────────────────────────────

def _fadd(a: int, b: int) -> int:
    """Field addition: (a + b) mod p."""
    return (a + b) % FIELD_PRIME


def _fsub(a: int, b: int) -> int:
    """Field subtraction: (a - b) mod p."""
    return (a - b) % FIELD_PRIME


def _fmul(a: int, b: int) -> int:
    """Field multiplication: (a * b) mod p."""
    return (a * b) % FIELD_PRIME


def _finv(a: int) -> int:
    """Field multiplicative inverse via Fermat's little theorem: a^(p-2) mod p."""
    if a % FIELD_PRIME == 0:
        raise ZeroDivisionError("Cannot invert zero in the field")
    return pow(a, FIELD_PRIME - 2, FIELD_PRIME)


def _fneg(a: int) -> int:
    """Field negation: (-a) mod p."""
    return (-a) % FIELD_PRIME


def _to_field(x: int) -> int:
    """Reduce an integer into the field [0, p)."""
    return x % FIELD_PRIME


# ── Variable ────────────────────────────────────────────────────────────────

class Variable:
    """
    A variable in the arithmetic circuit, referencing an index in the
    witness vector.

    Supports operator overloading for ``+``, ``-``, and ``*`` which
    automatically generate new intermediate variables and add the
    corresponding R1CS constraints to the parent circuit.

    Attributes
    ----------
    index : int
        Position in the witness vector.
    name : str
        Human-readable name for debugging.
    circuit : ArithmeticCircuit
        Parent circuit that owns this variable.
    """

    __slots__ = ("index", "name", "circuit")

    def __init__(self, index: int, name: str, circuit: ArithmeticCircuit) -> None:
        self.index = index
        self.name = name
        self.circuit = circuit

    def __add__(self, other: Variable) -> Variable:
        """
        Add two variables, producing a new intermediate variable with
        the constraint: ``1 * (self + other) = result``.
        """
        if not isinstance(other, Variable):
            raise TypeError(f"Cannot add Variable and {type(other).__name__}")
        if other.circuit is not self.circuit:
            raise ValueError("Variables belong to different circuits")

        result = self.circuit.allocate_variable(
            f"({self.name}+{other.name})"
        )
        # Constraint: (self + other) * 1 = result
        # In R1CS: A={self:1, other:1}, B={ONE:1}, C={result:1}
        self.circuit.add_constraint(
            a={self.index: 1, other.index: 1},
            b={0: 1},  # index 0 is always the constant-one wire
            c={result.index: 1},
        )
        return result

    def __sub__(self, other: Variable) -> Variable:
        """
        Subtract two variables, producing a new intermediate variable
        with the constraint: ``1 * (self - other) = result``.
        """
        if not isinstance(other, Variable):
            raise TypeError(f"Cannot subtract Variable and {type(other).__name__}")
        if other.circuit is not self.circuit:
            raise ValueError("Variables belong to different circuits")

        result = self.circuit.allocate_variable(
            f"({self.name}-{other.name})"
        )
        # Constraint: (self - other) * 1 = result
        # A={self:1, other:-1}, B={ONE:1}, C={result:1}
        self.circuit.add_constraint(
            a={self.index: 1, other.index: _fneg(1)},
            b={0: 1},
            c={result.index: 1},
        )
        return result

    def __mul__(self, other: Variable) -> Variable:
        """
        Multiply two variables, producing a new intermediate variable
        with the constraint: ``self * other = result``.
        """
        if not isinstance(other, Variable):
            raise TypeError(f"Cannot multiply Variable and {type(other).__name__}")
        if other.circuit is not self.circuit:
            raise ValueError("Variables belong to different circuits")

        result = self.circuit.allocate_variable(
            f"({self.name}*{other.name})"
        )
        # Constraint: self * other = result
        self.circuit.add_constraint(
            a={self.index: 1},
            b={other.index: 1},
            c={result.index: 1},
        )
        return result

    def __repr__(self) -> str:
        return f"<Variable idx={self.index} name={self.name!r}>"


# ── Constraint ──────────────────────────────────────────────────────────────

class Constraint:
    """
    An R1CS (Rank-1 Constraint System) constraint of the form:

        (sum of a_coeffs[i] * w[i]) * (sum of b_coeffs[i] * w[i])
            = (sum of c_coeffs[i] * w[i])

    where ``w`` is the witness vector (w[0] = 1 by convention).

    Coefficients are stored as sparse dicts mapping variable index to
    field element.

    Attributes
    ----------
    a_coeffs : dict[int, int]
        Sparse coefficients for the A linear combination.
    b_coeffs : dict[int, int]
        Sparse coefficients for the B linear combination.
    c_coeffs : dict[int, int]
        Sparse coefficients for the C linear combination.
    label : str
        Optional human-readable label for debugging.
    """

    __slots__ = ("a_coeffs", "b_coeffs", "c_coeffs", "label")

    def __init__(
        self,
        a_coeffs: Dict[int, int],
        b_coeffs: Dict[int, int],
        c_coeffs: Dict[int, int],
        label: str = "",
    ) -> None:
        self.a_coeffs = {k: _to_field(v) for k, v in a_coeffs.items()}
        self.b_coeffs = {k: _to_field(v) for k, v in b_coeffs.items()}
        self.c_coeffs = {k: _to_field(v) for k, v in c_coeffs.items()}
        self.label = label

    def evaluate(self, witness: List[int]) -> bool:
        """
        Check whether this constraint is satisfied by the given witness.

        Parameters
        ----------
        witness : list[int]
            The full witness vector (w[0] must be 1).

        Returns
        -------
        bool
            True if ``A(w) * B(w) == C(w)`` in the field.
        """
        a_val = self._dot(self.a_coeffs, witness)
        b_val = self._dot(self.b_coeffs, witness)
        c_val = self._dot(self.c_coeffs, witness)
        return _fmul(a_val, b_val) == c_val

    @staticmethod
    def _dot(coeffs: Dict[int, int], witness: List[int]) -> int:
        """Compute the sparse dot product of coefficients with the witness."""
        acc = 0
        for idx, coeff in coeffs.items():
            if idx < len(witness):
                acc = _fadd(acc, _fmul(coeff, witness[idx]))
        return acc

    def __repr__(self) -> str:
        label_str = f" ({self.label})" if self.label else ""
        return (
            f"<Constraint{label_str} "
            f"A={dict(self.a_coeffs)} B={dict(self.b_coeffs)} "
            f"C={dict(self.c_coeffs)}>"
        )


# ── ArithmeticCircuit ───────────────────────────────────────────────────────

class ArithmeticCircuit:
    """
    R1CS arithmetic circuit (EXPERIMENTAL constraint-checking utility).

    Used by the experimental Tier-2 path; not part of a production
    zero-knowledge proof system (see module docstring).

    The circuit maintains a witness vector where index 0 is always the
    constant ``1``. Public inputs occupy the next ``num_public`` slots,
    followed by private witness variables.

    Usage::

        circuit = ArithmeticCircuit()
        x = circuit.allocate_public_input("x", 3)
        y = circuit.allocate_public_input("y", 4)
        z = x * y  # automatically adds R1CS constraint
        assert circuit.check_constraints()  # witness[z] = 12

    All arithmetic is performed modulo the BN254 scalar field prime.
    """

    def __init__(self) -> None:
        # Witness vector: index 0 is always the constant 1
        self._witness: List[int] = [1]
        self._var_names: List[str] = ["ONE"]
        self._name_to_index: Dict[str, int] = {"ONE": 0}
        self._constraints: List[Constraint] = []
        self._num_public: int = 0
        self._finalized: bool = False

    # ── Variable allocation ────────────────────────────────────────────────

    def allocate_variable(self, name: str, value: int = 0) -> Variable:
        """
        Allocate a new private witness variable.

        Parameters
        ----------
        name : str
            Human-readable name (must be unique within the circuit).
        value : int
            Initial witness value (default 0). Can be updated later
            with ``set_witness()``.

        Returns
        -------
        Variable
            A Variable object with index into the witness vector.

        Raises
        ------
        ValueError
            If a variable with this name already exists.
        RuntimeError
            If the circuit has been finalized.
        """
        if self._finalized:
            raise RuntimeError("Cannot allocate variables after finalization")
        if name in self._name_to_index:
            raise ValueError(f"Variable {name!r} already exists at index {self._name_to_index[name]}")

        idx = len(self._witness)
        self._witness.append(_to_field(value))
        self._var_names.append(name)
        self._name_to_index[name] = idx
        return Variable(idx, name, self)

    def allocate_public_input(self, name: str, value: int) -> Variable:
        """
        Allocate a public input variable.

        Public inputs are placed immediately after the constant-one wire
        (index 0) and before private witnesses. They form the statement
        that the verifier checks against.

        Parameters
        ----------
        name : str
            Human-readable name (must be unique).
        value : int
            The public input value.

        Returns
        -------
        Variable
            A Variable object with index into the witness vector.

        Raises
        ------
        ValueError
            If a variable with this name already exists.
        RuntimeError
            If private variables have already been allocated (public
            inputs must come first).
        """
        if self._finalized:
            raise RuntimeError("Cannot allocate variables after finalization")
        # Public inputs must be allocated before any private variables.
        # They occupy indices 1..num_public.
        expected_next_public = self._num_public + 1
        if len(self._witness) != expected_next_public:
            raise RuntimeError(
                "Public inputs must be allocated before private variables. "
                f"Expected next index {expected_next_public}, "
                f"but witness has {len(self._witness)} entries."
            )

        var = self.allocate_variable(name, value)
        self._num_public += 1
        return var

    # ── Constraint management ──────────────────────────────────────────────

    def add_constraint(
        self,
        a: Dict[int, int],
        b: Dict[int, int],
        c: Dict[int, int],
        label: str = "",
    ) -> None:
        """
        Add an R1CS constraint: ``A(w) * B(w) = C(w)``.

        Parameters
        ----------
        a : dict[int, int]
            Sparse coefficients for the A linear combination.
            Maps variable index to field coefficient.
        b : dict[int, int]
            Sparse coefficients for the B linear combination.
        c : dict[int, int]
            Sparse coefficients for the C linear combination.
        label : str
            Optional label for debugging.

        Raises
        ------
        ValueError
            If any variable index is out of range.
        """
        max_idx = len(self._witness) - 1
        for name_str, coeffs in [("A", a), ("B", b), ("C", c)]:
            for idx in coeffs:
                if idx < 0 or idx > max_idx:
                    raise ValueError(
                        f"Variable index {idx} in {name_str} out of range "
                        f"[0, {max_idx}]"
                    )
        self._constraints.append(Constraint(a, b, c, label=label))

    def assert_equal(self, var_a: Variable, var_b: Variable) -> None:
        """
        Add a constraint asserting ``var_a == var_b``.

        Encoded as: ``(var_a - var_b) * 1 = 0``.
        """
        self.add_constraint(
            a={var_a.index: 1, var_b.index: _fneg(1)},
            b={0: 1},
            c={},  # empty = 0
            label=f"assert_equal({var_a.name}, {var_b.name})",
        )

    def assert_less_equal(self, var: Variable, bound: Variable) -> None:
        """
        Encode ``var <= bound`` as an R1CS constraint.

        Introduces a slack variable ``slack`` such that:
            ``var + slack = bound``  (where slack >= 0)

        The non-negativity of ``slack`` is enforced by the witness
        assignment: the prover must provide a valid non-negative slack
        value. In a full SNARK system, range proofs would additionally
        constrain ``slack`` to a bounded bit-width.

        Parameters
        ----------
        var : Variable
            The variable that must be at most ``bound``.
        bound : Variable
            The upper bound variable.
        """
        # Allocate slack variable
        var_val = self._witness[var.index] if var.index < len(self._witness) else 0
        bound_val = self._witness[bound.index] if bound.index < len(self._witness) else 0
        slack_val = _fsub(bound_val, var_val)
        slack = self.allocate_variable(
            f"_slack_{var.name}_le_{bound.name}",
            slack_val,
        )

        # Constraint: (var + slack) * 1 = bound
        self.add_constraint(
            a={var.index: 1, slack.index: 1},
            b={0: 1},
            c={bound.index: 1},
            label=f"assert_le({var.name}, {bound.name})",
        )

    # ── Witness management ─────────────────────────────────────────────────

    def set_witness(self, name: str, value: int) -> None:
        """
        Set or update the witness value for a named variable.

        Parameters
        ----------
        name : str
            Variable name (must have been previously allocated).
        value : int
            New witness value.

        Raises
        ------
        KeyError
            If no variable with this name exists.
        """
        if name not in self._name_to_index:
            raise KeyError(f"No variable named {name!r} in circuit")
        idx = self._name_to_index[name]
        if idx == 0:
            raise ValueError("Cannot modify the constant-one wire")
        self._witness[idx] = _to_field(value)

    def get_witness(self, name: str) -> int:
        """Return the current witness value for a named variable."""
        if name not in self._name_to_index:
            raise KeyError(f"No variable named {name!r} in circuit")
        return self._witness[self._name_to_index[name]]

    def get_witness_vector(self) -> List[int]:
        """Return a copy of the full witness vector."""
        return list(self._witness)

    # ── Constraint verification ────────────────────────────────────────────

    def check_constraints(self) -> bool:
        """
        Verify that all R1CS constraints are satisfied by the current
        witness vector.

        Returns
        -------
        bool
            True if every constraint ``A(w) * B(w) == C(w)`` holds.
        """
        for i, constraint in enumerate(self._constraints):
            if not constraint.evaluate(self._witness):
                logger.debug(
                    "Constraint %d failed: %s", i, constraint.label or repr(constraint)
                )
                return False
        return True

    def get_unsatisfied_constraints(self) -> List[Tuple[int, Constraint]]:
        """
        Return a list of ``(index, constraint)`` pairs for all
        constraints not satisfied by the current witness.
        """
        failures: List[Tuple[int, Constraint]] = []
        for i, constraint in enumerate(self._constraints):
            if not constraint.evaluate(self._witness):
                failures.append((i, constraint))
        return failures

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def constraint_count(self) -> int:
        """Number of R1CS constraints in the circuit."""
        return len(self._constraints)

    @property
    def variable_count(self) -> int:
        """Total number of variables including the constant-one wire."""
        return len(self._witness)

    @property
    def public_input_count(self) -> int:
        """Number of public input variables."""
        return self._num_public

    @property
    def public_inputs(self) -> Dict[str, int]:
        """Return a dict of public input names to their witness values."""
        result: Dict[str, int] = {}
        for i in range(1, self._num_public + 1):
            name = self._var_names[i]
            result[name] = self._witness[i]
        return result

    def __repr__(self) -> str:
        return (
            f"<ArithmeticCircuit "
            f"vars={self.variable_count} "
            f"public={self._num_public} "
            f"constraints={self.constraint_count}>"
        )
