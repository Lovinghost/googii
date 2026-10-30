import asyncio

from agent.events import EventJournal


async def test_event_journal_replays_in_sequence_and_closes() -> None:
    journal = EventJournal("run-1", "session-1")
    await journal.publish("run.started", {})
    await journal.publish("tool.completed", {"toolName": "read_file"})
    await journal.close()

    events = [event async for event in journal.stream(after_sequence=1)]

    assert [event.type for event in events] == ["tool.completed"]
    assert events[0].sequence == 2
    assert events[0].as_api_dict()["runId"] == "run-1"


async def test_event_journal_wakes_waiting_stream() -> None:
    journal = EventJournal("run-2", "session-2")

    async def collect():
        return [event async for event in journal.stream()]

    consumer = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await journal.publish("run.completed", {"content": "done"})
    await journal.close()

    events = await consumer
    assert [event.type for event in events] == ["run.completed"]
