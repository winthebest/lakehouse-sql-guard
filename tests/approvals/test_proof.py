"""The proof of approval: what it binds, how long it lives, and what the service insists on."""

from __future__ import annotations

from datetime import timedelta

import pytest

from sqlguard.approvals import HmacProofs, ReviewService
from sqlguard.safety import Guard, ProofRequest

REQUEST = ProofRequest("a" * 32, "h" * 64, "approve", "alice")


def test_a_minted_proof_verifies_for_exactly_its_request(clock):
    proofs = HmacProofs(clock=clock)
    proof = proofs.mint(REQUEST)
    assert proofs.verify(REQUEST, proof) is True
    for changed in (
        ProofRequest("b" * 32, REQUEST.action_hash, "approve", "alice"),
        ProofRequest(REQUEST.action_id, "x" * 64, "approve", "alice"),
        ProofRequest(REQUEST.action_id, REQUEST.action_hash, "reject", "alice"),
        ProofRequest(REQUEST.action_id, REQUEST.action_hash, "approve", "bob"),
    ):
        assert proofs.verify(changed, proof) is False


def test_every_proof_is_different_and_tampering_breaks_it(clock):
    proofs = HmacProofs(clock=clock)
    first, second = proofs.mint(REQUEST), proofs.mint(REQUEST)
    assert first != second and proofs.verify(REQUEST, second)
    version, nonce, expiry, mac = first.split(".")
    for forged in (
        ".".join((version, nonce, str(int(expiry) + 3600), mac)),  # a longer life
        ".".join((version, nonce[:-1] + "0", expiry, mac)),
        ".".join((version, nonce, expiry, mac[:-1] + ("0" if mac[-1] != "0" else "1"))),
        first + ".extra",
        first.upper(),
        "",
    ):
        assert proofs.verify(REQUEST, forged) is False


def test_a_proof_lives_for_its_lifetime_and_no_longer(clock):
    proofs = HmacProofs(ttl=timedelta(minutes=5), clock=clock)
    proof = proofs.mint(REQUEST)
    clock.advance(minutes=4, seconds=50)
    assert proofs.verify(REQUEST, proof)
    clock.advance(seconds=20)
    assert not proofs.verify(REQUEST, proof)


def test_two_processes_cannot_verify_each_others_proofs(clock):
    assert not HmacProofs(clock=clock).verify(REQUEST, HmacProofs(clock=clock).mint(REQUEST))
    shared = b"k" * 32
    assert HmacProofs(shared, clock=clock).verify(
        REQUEST, HmacProofs(shared, clock=clock).mint(REQUEST)
    )


def test_the_arguments_are_validated():
    with pytest.raises(ValueError):
        HmacProofs(b"short")
    with pytest.raises(ValueError):
        HmacProofs(ttl=timedelta(0))


def test_the_service_needs_a_store_and_the_guards_own_verifier(make_guard, store_path, proofs):
    with pytest.raises(ValueError, match="action store"):
        ReviewService(make_guard(proof_verifier=proofs), proofs, "alice")
    guard: Guard = make_guard(store=store_path, proof_verifier=HmacProofs())
    with pytest.raises(ValueError, match="same HmacProofs"):
        ReviewService(guard, proofs, "alice")
    for label in ("", "   ", "x" * 65, "a\x00b"):
        with pytest.raises(ValueError, match="approver label"):
            ReviewService(make_guard(store=store_path, proof_verifier=proofs), proofs, label)
