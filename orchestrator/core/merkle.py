"""
Modus — Merkle Tree Implementation
=========================================
Copyright 2026 babbguy
SPDX-License-Identifier: Apache-2.0

Pure-Python Merkle tree for cryptographic attestation of enforcement decisions.
Used by the attestation engine to anchor batches of signed decisions into a
single tamper-evident root hash.

Stdlib only. Zero dependencies. Thread-safe (built trees are immutable snapshots).
"""

from __future__ import annotations

import hashlib
import threading
from typing import Optional

def _hash_pair(left: str, right: str) -> str:
    """Hash two hex-encoded SHA-256 digests into their parent node."""
    combined = bytes.fromhex(left) + bytes.fromhex(right)
    return hashlib.sha256(combined).hexdigest()


class MerkleTree:
    """
    Append-only Merkle tree.

    Usage::

        tree = MerkleTree()
        tree.add_leaf(hashlib.sha256(b"decision-1").hexdigest())
        tree.add_leaf(hashlib.sha256(b"decision-2").hexdigest())
        root = tree.build()
        proof = tree.get_proof(0)
        assert MerkleTree.verify_proof(tree._leaves[0], proof, root)

    Thread-safety: ``add_leaf`` and ``build`` acquire a lock. Once built, the
    tree layers are an immutable snapshot — concurrent reads are safe.
    """

    __slots__ = ("_leaves", "_layers", "_root", "_lock")

    def __init__(self) -> None:
        self._leaves: list[str] = []
        self._layers: list[list[str]] = []
        self._root: Optional[str] = None
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def add_leaf(self, data_hash: str) -> None:
        """Append a leaf (hex-encoded SHA-256 hash). Invalidates any prior build."""
        if len(data_hash) != 64:
            raise ValueError("Leaf must be a 64-char hex SHA-256 digest")
        # Validate hex
        try:
            bytes.fromhex(data_hash)
        except ValueError:
            raise ValueError("Leaf must be a valid hex string")
        with self._lock:
            self._leaves.append(data_hash)
            # Invalidate previous build
            self._root = None
            self._layers = []

    def build(self) -> str:
        """
        Compute the Merkle root from current leaves.

        Returns the root hash as a hex string.
        Raises ValueError if the tree has no leaves.
        """
        with self._lock:
            if not self._leaves:
                raise ValueError("Cannot build Merkle tree with no leaves")

            # Snapshot leaves into layer 0
            layer: list[str] = list(self._leaves)
            layers: list[list[str]] = [layer]

            while len(layer) > 1:
                next_layer: list[str] = []
                # If odd number of nodes, duplicate the last one
                if len(layer) % 2 == 1:
                    layer = layer + [layer[-1]]
                    # Update the current layer in our snapshot for proof traversal
                    layers[-1] = layer

                for i in range(0, len(layer), 2):
                    next_layer.append(_hash_pair(layer[i], layer[i + 1]))

                layers.append(next_layer)
                layer = next_layer

            self._layers = layers
            self._root = layer[0]
            return self._root

    def get_proof(self, leaf_index: int) -> list[dict]:
        """
        Return the inclusion proof for the leaf at *leaf_index*.

        Each element is ``{"hash": "<hex>", "position": "left"|"right"}``,
        where *position* indicates which side the sibling sits on.

        Must call ``build()`` first.
        """
        if not self._layers:
            raise RuntimeError("Tree not built — call build() first")
        if leaf_index < 0 or leaf_index >= len(self._leaves):
            raise IndexError(
                f"leaf_index {leaf_index} out of range [0, {len(self._leaves)})"
            )

        proof: list[dict] = []
        idx = leaf_index

        for layer in self._layers[:-1]:  # all layers except the root
            if idx % 2 == 0:
                # Sibling is to the right
                sibling_idx = idx + 1
                if sibling_idx < len(layer):
                    proof.append({"hash": layer[sibling_idx], "position": "right"})
            else:
                # Sibling is to the left
                sibling_idx = idx - 1
                proof.append({"hash": layer[sibling_idx], "position": "left"})

            # Move up to the parent index
            idx = idx // 2

        return proof

    @staticmethod
    def verify_proof(leaf_hash: str, proof: list[dict], root: str) -> bool:
        """
        Verify that *leaf_hash* is included in a tree with the given *root*
        using the supplied inclusion *proof*.

        Returns True if the proof is valid.
        """
        current = leaf_hash
        for step in proof:
            sibling = step["hash"]
            if step["position"] == "right":
                current = _hash_pair(current, sibling)
            else:
                current = _hash_pair(sibling, current)
        return current == root

    @property
    def leaf_count(self) -> int:
        """Number of leaves added to the tree."""
        return len(self._leaves)

    @property
    def root(self) -> Optional[str]:
        """Root hash, or None if the tree has not been built."""
        return self._root

    def __repr__(self) -> str:
        state = "built" if self._root else "pending"
        return f"<MerkleTree leaves={len(self._leaves)} state={state}>"
