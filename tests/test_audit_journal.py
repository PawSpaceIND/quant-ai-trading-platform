from quant_ai.audit.journal import InMemoryAuditJournal


def test_hash_chained_audit_log_verifies() -> None:
    journal = InMemoryAuditJournal()
    first = journal.append("SIGNAL", {"symbol": "AAPL"})
    second = journal.append("RISK", {"approved": True})
    assert second.previous_hash == first.event_hash
    assert journal.verify_chain()
