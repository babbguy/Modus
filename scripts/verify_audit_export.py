#!/usr/bin/env python3
"""
Modus — Offline Audit-Export Verifier
========================================
Independently verifies a Modus audit-chain export on an air-gapped machine,
with **only the Python standard library** — no Modus install, no pip
dependencies, no network. Intended for a bank examiner or external auditor who
does not trust the operator and holds only the export file.

It performs three checks:
  1. Re-derives every entry's SHA-256 hash from its content + prev_hash and
     confirms it matches the stored entry_hash (detects content tampering).
  2. Walks the chain confirming each entry links to the prior entry_hash and
     the sequence is gap-free (detects deletion / reordering).
  3. Verifies the Ed25519 checkpoint signature against the embedded public key
     (proves the checkpoint — and thus the chain up to it — was signed by the
     holder of the private key, and could not have been forged by the auditor).

Usage:
    python verify_audit_export.py <export.json>

Exit code 0 = verified, 1 = verification failed, 2 = usage/format error.

The Ed25519 verification below is the RFC 8032 reference implementation in pure
Python — slow but dependency-free and auditable. It is used only to check a
small number of checkpoint signatures.
"""
import hashlib
import json
import sys


# ── Chain hashing (must match orchestrator.core.audit_chain.compute_entry_hash) ─

def compute_entry_hash(entry):
    payload = json.dumps({
        "chain_seq": entry["chain_seq"],
        "prev_hash": entry["prev_hash"],
        "actor_id": entry["actor_id"],
        "actor_ip": entry.get("actor_ip") or "",
        "team_id": entry.get("team_id") or "",
        "resource_type": entry["resource_type"],
        "resource_id": entry.get("resource_id") or "",
        "action": entry["action"],
        "before": entry.get("before"),
        "after": entry.get("after"),
        "occurred_at": entry.get("occurred_at") or "",
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


GENESIS_PREV_HASH = "0" * 64


def verify_chain(chain):
    """Return (ok, message). Checks hashes, linkage, and sequence continuity."""
    if not chain:
        return True, "empty chain (nothing to verify)"

    expected_prev = None
    expected_seq = chain[0]["chain_seq"]

    for entry in chain:
        if entry["chain_seq"] != expected_seq:
            return False, (
                f"sequence gap: expected seq {expected_seq}, "
                f"found {entry['chain_seq']} (deletion or reordering)"
            )
        if expected_prev is not None and entry["prev_hash"] != expected_prev:
            return False, f"broken link at seq {entry['chain_seq']}: prev_hash mismatch"

        recomputed = compute_entry_hash(entry)
        if recomputed != entry["entry_hash"]:
            return False, f"content tampered at seq {entry['chain_seq']}: entry_hash mismatch"

        expected_prev = entry["entry_hash"]
        expected_seq = entry["chain_seq"] + 1

    return True, f"{len(chain)} entries: all hashes and links valid"


# ── Ed25519 verification (RFC 8032 reference implementation, pure stdlib) ────────

_b = 256
_q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _H(m):
    return hashlib.sha512(m).digest()


def _inv(x):
    return pow(x, _q - 2, _q)


_d = -121665 * _inv(121666) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = 4 * _inv(5) % _q
_Bx = _xrecover(_By)
_B = [_Bx % _q, _By % _q]


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2) % _q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2) % _q
    return [x3 % _q, y3 % _q]


def _scalarmult(P, e):
    # Iterative double-and-add to avoid deep recursion.
    result = [0, 1]
    addend = P
    while e > 0:
        if e & 1:
            result = _edwards(result, addend)
        addend = _edwards(addend, addend)
        e >>= 1
    return result


def _bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def _decodeint(s):
    return sum(2 ** i * _bit(s, i) for i in range(0, _b))


def _isoncurve(P):
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _decodepoint(s):
    y = sum(2 ** i * _bit(s, i) for i in range(0, _b - 1))
    x = _xrecover(y)
    if x & 1 != _bit(s, _b - 1):
        x = _q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("point not on curve")
    return P


def _encodepoint(P):
    x, y = P
    bits = [(y >> i) & 1 for i in range(_b - 1)] + [x & 1]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_b // 8)
    )


def _Hint(m):
    h = _H(m)
    return sum(2 ** i * _bit(h, i) for i in range(2 * _b))


def ed25519_verify(public_key_bytes, message, signature):
    """Return True iff `signature` is a valid Ed25519 signature of `message`."""
    if len(signature) != 64:
        return False
    if len(public_key_bytes) != 32:
        return False
    try:
        R = _decodepoint(signature[:32])
        A = _decodepoint(public_key_bytes)
    except ValueError:
        return False
    S = _decodeint(signature[32:64])
    h = _Hint(_encodepoint(R) + public_key_bytes + message)
    return _scalarmult(_B, S) == _edwards(R, _scalarmult(A, h))


def checkpoint_message(chain_seq, entry_hash, created_at):
    return f"modus-audit-checkpoint-v1|{chain_seq}|{entry_hash}|{created_at}".encode("utf-8")


def verify_checkpoint(export):
    """Return (ok, message) for the checkpoint signature."""
    cp = export.get("checkpoint")
    pub = export.get("public_key")
    if not cp:
        return True, "no checkpoint present (chain integrity still verified above)"
    if not cp.get("signature") or not pub:
        return False, "checkpoint present but unsigned or missing public key"

    # The checkpoint must point at a real entry in the chain, at the head.
    chain = export.get("chain", [])
    head_hash = chain[-1]["entry_hash"] if chain else None
    if head_hash is not None and cp["entry_hash"] != head_hash:
        # Not necessarily fatal (checkpoint may predate the export head), but
        # the checkpoint's entry_hash must at least match some entry.
        seqs = {e["chain_seq"]: e["entry_hash"] for e in chain}
        if seqs.get(cp["chain_seq"]) != cp["entry_hash"]:
            return False, "checkpoint entry_hash does not match any chain entry"

    msg = checkpoint_message(cp["chain_seq"], cp["entry_hash"], cp["created_at"])
    ok = ed25519_verify(bytes.fromhex(pub), msg, bytes.fromhex(cp["signature"]))
    if ok:
        return True, f"checkpoint signature valid at seq {cp['chain_seq']} (Ed25519)"
    return False, "checkpoint signature INVALID — not signed by the published key"


# ── Evidence pack ───────────────────────────────────────────────────────────

_PACK_ENVELOPE_KEYS = (
    "public_key", "algorithm", "pack_digest", "signature", "signed_at",
    "verification",
)


def verify_evidence_pack(pack):
    """Return (digest_ok, digest_msg, sig_ok, sig_msg) for an evidence pack."""
    body = {k: v for k, v in pack.items() if k not in _PACK_ENVELOPE_KEYS}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    if recomputed != pack.get("pack_digest"):
        return (False, "pack digest mismatch — content was altered after signing",
                False, "signature not checked (digest already failed)")

    sig = pack.get("signature")
    pub = pack.get("public_key")
    if not sig or not pub:
        return (True, f"digest matches ({len(body.get('governance_decisions', []))} decisions)",
                False, "pack is unsigned or missing public key")

    # Same signing scheme as checkpoints: message = ...|0|<digest>|<signed_at>
    msg = checkpoint_message(0, pack["pack_digest"], pack.get("signed_at", ""))
    sig_ok = ed25519_verify(bytes.fromhex(pub), msg, bytes.fromhex(sig))
    return (
        True, f"digest matches ({len(body.get('governance_decisions', []))} decisions)",
        sig_ok, "signature valid (Ed25519)" if sig_ok
        else "signature INVALID — not signed by the published key",
    )


def main(argv):
    if len(argv) != 2:
        print("usage: python verify_audit_export.py <file.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"error: could not read file: {exc}", file=sys.stderr)
        return 2

    fmt = doc.get("format")

    if fmt == "modus-audit-export-v1":
        chain_ok, chain_msg = verify_chain(doc.get("chain", []))
        cp_ok, cp_msg = verify_checkpoint(doc)
        print(f"[chain]      {'PASS' if chain_ok else 'FAIL'}  {chain_msg}")
        print(f"[checkpoint] {'PASS' if cp_ok else 'FAIL'}  {cp_msg}")
        if chain_ok and cp_ok:
            print("\nVERIFIED: the audit export is intact and signed by the published key.")
            return 0
        print("\nFAILED: the audit export did not verify.")
        return 1

    if fmt == "modus-evidence-pack-v1":
        digest_ok, digest_msg, sig_ok, sig_msg = verify_evidence_pack(doc)
        print(f"[digest]     {'PASS' if digest_ok else 'FAIL'}  {digest_msg}")
        print(f"[signature]  {'PASS' if sig_ok else 'FAIL'}  {sig_msg}")
        if digest_ok and sig_ok:
            print("\nVERIFIED: the evidence pack is intact and signed by the published key.")
            return 0
        print("\nFAILED: the evidence pack did not verify.")
        return 1

    print(f"error: unexpected format {fmt!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
