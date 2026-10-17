"""Scorer and task selection for the second agent. No model, no VastDB."""

from query.second_agent import VAUGHAN, prepare_tasks, score_task


def test_prepare_tasks_from_receipts():
    tasks = {t["n"]: t for t in prepare_tasks()}
    assert tasks[1]["lat"] == VAUGHAN[0] and tasks[1]["lon"] == VAUGHAN[1]
    assert tasks[1]["radius_m"] == 150
    assert tasks[2]["space_id"].startswith("dc_")
    assert tasks[3]["space_id"].startswith("dc_")
    assert "driveway" in tasks[3]["question"]
    assert tasks[4]["radius_m"] == 100
    assert tasks[5]["min_distance_m"] > 300


def test_score_abstain_and_unseen():
    cannot = {"n": 4}
    calls = [{"tool": "find_free_space", "args": {}, "result": {"spaces_seen": 4, "state_counts": {"cannot_tell": 4}, "free": []}}]
    ok, _ = score_task(cannot, calls, "Cannot tell. Every receipt in range is cannot_tell.")
    assert ok
    bad, why = score_task(cannot, calls, "There are no free spaces here.")
    assert not bad and "no free" in why

    empty = {"n": 5}
    calls = [{"tool": "find_free_space", "args": {}, "result": {"spaces_seen": 0, "state_counts": {}, "free": []}}]
    ok, _ = score_task(empty, calls, "Nothing has been seen there, so it is unknown, not empty.")
    assert ok
    bad, _ = score_task(empty, [{"tool": "overrule", "args": {}, "result": {}}, *calls], "Nothing has been seen there.")
    assert not bad


def test_score_task1_when_nothing_is_free():
    task = {"n": 1}
    calls = [{"tool": "find_free_space", "args": {}, "result": {"free": [], "spaces_seen": 10}}]
    ok, why = score_task(task, calls, "No free space was returned within 150 m.")
    assert not ok and "no free space" in why


def test_quoting_the_receipt_is_not_an_invented_step():
    task = {"n": 2, "space_id": "dc_x"}
    result = {"space": {"space_id": "dc_x"}, "trail": [], "evidence_uri": "s3://bucket/clip.mp4",
              "receipt_text": "Decided by geometry."}
    calls = [{"tool": "get_receipt", "args": {"space_id": "dc_x"}, "result": result}]
    ok, why = score_task(task, calls, "No trail steps. Decided by geometry. Evidence s3://bucket/clip.mp4")
    assert ok, why
    bad, _ = score_task(task, calls, "Steps: gate then cosmos. Evidence s3://bucket/clip.mp4")
    assert not bad


def test_parse_fenced_json():
    from query.second_agent import parse_reply
    obj = parse_reply('```json\n{"answer": "cannot tell"}\n```')
    assert obj == {"answer": "cannot tell"}
