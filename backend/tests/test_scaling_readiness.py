"""What a second API worker would break, and what no longer does (SCALE-01/02).

Every test here simulates the same thing: **a second process that never saw the
write.** In one process that is a cache miss, so it is reproduced by clearing
the in-memory index and asking again — which is exactly the state worker B is in
when worker A handled the signup.

The rule under test is that an in-memory structure is a *cache* or it is a bug.
A cache is one whose miss is paid by an authoritative read. Anything else is a
value that exists on one worker and not the other, and the symptom is always the
same: it works, then intermittently does not, at a rate the load balancer sets,
with nothing in the logs to say why.

`docs/SCALING-AUDIT.md` is the full list, including the two things this cannot
yet assert because they are still process-local.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.services import accounts, jobs, oauth, persistence
from main import app

client = TestClient(app)


def _forget_everything_cached() -> None:
    """Become the worker that has never seen any of this."""
    for index in (accounts._ACCOUNTS, accounts._EMAIL_INDEX, accounts._APIKEY_INDEX,
                  accounts._STRIPE_INDEX, accounts._OAUTH_INDEX):
        index.clear()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def test_a_signup_on_another_worker_can_still_sign_in():
    """The bug with no symptom anyone can debug: invalid credentials for an
    account that plainly exists, at a rate the load balancer sets."""
    email = f"scale-{time.time_ns()}@example.com"
    accounts.create_account(email, "correct horse battery")
    _forget_everything_cached()

    acc = accounts.authenticate_password(email, "correct horse battery")
    assert acc.email == email


def test_a_wrong_password_is_still_wrong_after_a_datastore_lookup():
    email = f"scale-{time.time_ns()}@example.com"
    accounts.create_account(email, "correct horse battery")
    _forget_everything_cached()
    with pytest.raises(accounts.AuthError):
        accounts.authenticate_password(email, "wrong")


def test_an_account_id_resolves_without_the_index():
    email = f"scale-{time.time_ns()}@example.com"
    created = accounts.create_account(email, "correct horse battery")
    _forget_everything_cached()
    assert accounts.get_account(created.id) is not None


def test_an_api_key_issued_on_another_worker_authenticates():
    email = f"scale-{time.time_ns()}@example.com"
    acc = accounts.create_account(email, "correct horse battery")
    accounts.set_plan(acc, "pro")   # API keys are a Pro entitlement
    key = accounts.issue_api_key(acc, "ci")
    _forget_everything_cached()
    assert accounts.authenticate_api_key(key).id == acc.id


def test_a_bad_api_key_is_still_refused_after_the_scan():
    _forget_everything_cached()
    with pytest.raises(accounts.AuthError):
        accounts.authenticate_api_key("forge_not-a-real-key")


def test_a_stripe_webhook_finds_the_account_it_is_about():
    """The case that makes the fallback mandatory rather than tidy: a webhook
    lands on whichever worker the balancer picks, and a plan grant dropped
    because that worker had not seen the signup is a paid upgrade that silently
    does not apply."""
    email = f"scale-{time.time_ns()}@example.com"
    acc = accounts.create_account(email, "correct horse battery")
    customer = f"cus_{time.time_ns()}"
    accounts.link_stripe(acc, customer, "sub_1")
    _forget_everything_cached()
    assert accounts.account_by_stripe_customer(customer).id == acc.id


def test_a_lookup_that_falls_through_indexes_what_it_found():
    """A miss should be paid once, not on every request."""
    email = f"scale-{time.time_ns()}@example.com"
    accounts.create_account(email, "correct horse battery")
    _forget_everything_cached()
    accounts.account_by_email(email)
    assert email in accounts._EMAIL_INDEX


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def test_oauth_state_is_shared_rather_than_process_local():
    """The flow spans two requests, and the callback lands wherever the balancer
    sends it. In memory it failed about half the time with 'invalid or expired
    sign-in attempt', which reads as a provider fault."""
    key = oauth._STATE_PREFIX + "state-under-test"
    persistence.save_ephemeral(key, {"provider_id": "github",
                                     "code_verifier": "v",
                                     "expires_at": time.time() + 60})
    # A different process reads the same row.
    assert persistence.load_job("nothing") is None  # unrelated kind, no collision
    taken = persistence.take_ephemeral(key)
    assert taken["code_verifier"] == "v"


def test_ephemeral_state_is_single_use():
    """Two concurrent redemptions of one handoff code must not both win."""
    key = oauth._HANDOFF_PREFIX + "handoff-under-test"
    persistence.save_ephemeral(key, {"token": "t", "account_id": "a",
                                     "expires_at": time.time() + 60})
    assert persistence.take_ephemeral(key) is not None
    assert persistence.take_ephemeral(key) is None


def test_expired_ephemeral_state_is_purged():
    key = oauth._STATE_PREFIX + "stale-state"
    persistence.save_ephemeral(key, {"provider_id": "github", "code_verifier": "v",
                                     "expires_at": 0})
    assert persistence.purge_ephemeral(time.time()) >= 1
    assert persistence.take_ephemeral(key) is None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def test_a_job_submitted_by_another_worker_is_visible_here():
    """Before this, a poll landing on the other worker was a 404 —
    indistinguishable from a job that never existed."""
    job = jobs.enqueue("release_package", "doc-scale", {"revision": None})
    # Nothing is held in memory to clear: the row *is* the job.
    assert jobs.get(job.id) is not None
    assert jobs.active_for_subject("doc-scale") is not None
    persistence.delete_job(job.id)


def test_two_workers_cannot_both_claim_one_job():
    job = jobs.enqueue("release_package", "doc-scale-2", {"revision": None})
    first = jobs.claim("worker-a")
    second = jobs.claim("worker-b")
    assert (first is not None) and (second is None)
    persistence.delete_job(job.id)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def test_a_document_write_reaches_a_process_that_never_cached_it():
    from conftest import http_part
    from app.services import cad_operations, document_service as docs

    doc_id = http_part(client, (0.05, 0.04, 0.01), resolution=32)
    cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
    revision = docs.get_document(doc_id).revision

    docs.DOCUMENTS.pop(doc_id, None)   # the worker that never saw the write
    reloaded = docs.get_document(doc_id)
    assert reloaded.material_id == "al-6061"
    # And the revision came with it, which is what makes an optimistic write from
    # the other worker check against the right number.
    assert reloaded.revision == revision


def test_an_expected_revision_check_reads_the_persisted_revision():
    """SCALE-02, as far as one process can assert it.

    The optimistic path is safe across workers because the number it compares
    is persisted. What is *not* yet safe is two writers that both omit the
    revision: the write lock is per process. That gap is stated in
    docs/SCALING-AUDIT.md and gates multi-worker rollout.
    """
    from conftest import http_part
    from app.services import cad_operations, document_service as docs
    from app.cad_kernel.core.feature_tree import walk_nodes

    doc_id = http_part(client, (0.05, 0.04, 0.01), resolution=32)
    stale = docs.get_document(doc_id).revision
    box = next(n["id"] for n in walk_nodes(docs.get_document(doc_id).feature_tree)
               if n.get("type") == "BoxFeature")
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": box, "params": {"size": {"x": 0.06, "y": 0.04, "z": 0.01}}})

    docs.DOCUMENTS.pop(doc_id, None)
    env = cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": box, "params": {"size": {"x": 0.07, "y": 0.04, "z": 0.01}}},
        expected_revision=stale)
    assert env["ok"] is False
    assert env["error"]["code"] == "PRECONDITION"


# ---------------------------------------------------------------------------
# Blobs
# ---------------------------------------------------------------------------

def test_a_generated_file_is_fetchable_from_a_store_that_never_wrote_it(tmp_path):
    from app.services import blobs

    previous = blobs.store()
    blobs.reset_store(blobs.FilesystemStore(tmp_path / "shared", cache_bytes=1024))
    try:
        blobs.put("doc-1", "abc", blobs.Blob(b"shared", "text/plain", "f.txt"))
        # A second store object over the same volume is what worker B is.
        other = blobs.FilesystemStore(tmp_path / "shared", cache_bytes=1024)
        assert other.get("doc-1", "abc").payload == b"shared"
    finally:
        blobs.reset_store(previous)
