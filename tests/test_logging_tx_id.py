import pytest
import json
from python.harness import db
from python.harness.server import handle_logging_get_events

@pytest.mark.asyncio
async def test_logging_transaction_id_database_insertion():
    """
    NAME
           test_logging_transaction_id_database_insertion - verifies inserting tx_id in agent_log
           
    SYNOPSIS
           test_logging_transaction_id_database_insertion() -> None
           
    DESCRIPTION
           Initializes the db connection, logs a message with a transaction ID, and verifies
           that the record is successfully saved in agent_log.
    """
    # Initialize the db pool to run migrations
    await db.init_pool()
    try:
        test_tx_id = "0x8fa3d662110ea09eb58c6a2376ef7780"
        test_message = "MINT: 10.0 tokens minted successfully on local ledger."
        
        # Log to db with tx_id
        await db.log_to_db(
            level="INFO",
            message=test_message,
            trace={"mint_amount": 10.0},
            tx_id=test_tx_id
        )
        
        # Verify from PostgreSQL
        row = await db.fetchrow(
            "SELECT level, message, tx_id, trace FROM agent_log WHERE tx_id = $1",
            test_tx_id
        )
        assert row is not None
        assert row["level"] == "INFO"
        assert row["message"] == test_message
        assert row["tx_id"] == test_tx_id
        
        raw_trace = row["trace"]
        if isinstance(raw_trace, str):
            trace_data = json.loads(raw_trace)
        else:
            trace_data = raw_trace or {}
            
        assert trace_data.get("mint_amount") == 10.0
        
    finally:
        await db.close_pool()

@pytest.mark.asyncio
async def test_logging_get_events_rpc_response():
    """
    NAME
           test_logging_get_events_rpc_response - verifies logging.get_events returns tx_id
           
    SYNOPSIS
           test_logging_get_events_rpc_response() -> None
           
    DESCRIPTION
           Inserts a log entry with a tx_id, triggers the logging.get_events RPC handler,
           and asserts that the returned payload contains the transaction ID.
    """
    await db.init_pool()
    try:
        test_tx_id = "0x98bb77f12a3d069b1836173d"
        await db.log_to_db(
            level="WARN",
            message="Warning: blockchain sync delayed",
            tx_id=test_tx_id
        )
        
        # Call RPC handler
        params = {"limit": 10, "levels": ["WARN"]}
        rpc_result_str = await handle_logging_get_events(params, req_id=123)
        rpc_result = json.loads(rpc_result_str)
        
        assert "result" in rpc_result
        events = rpc_result["result"].get("events", [])
        
        # Find the logged event
        matching_event = None
        for ev in events:
            if ev.get("tx_id") == test_tx_id:
                matching_event = ev
                break
                
        assert matching_event is not None
        # WARNING, not WARN: levels are normalised on write, because the two
        # spellings coexisted -- 98 rows against 16,589 -- and any filter for
        # one silently missed the other. Filtering by "WARN" still works.
        assert matching_event["level"] == "WARNING"
        assert matching_event["message"] == "Warning: blockchain sync delayed"
        assert matching_event["tx_id"] == test_tx_id
        
    finally:
        await db.close_pool()
