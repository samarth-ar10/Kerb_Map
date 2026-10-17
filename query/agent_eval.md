# Agent evaluation: five tasks for a second agent using the Kerb Map MCP tools

Server: `KERB_DB=local python -m query.mcp_server` (stdio). Tools: `find_free_space(lat, lon, radius_m)`,
`get_receipt(space_id)`, `overrule(space_id, state, note, by)`. No live LLM has been run against these yet; the tasks
and pass rules are written down first so the run can be scored without moving the goalposts.

Each task is run on the real receipts once they are in the tables (not on TEST rows). Record per task: the tool calls
made, the final answer, pass/fail by the rule below, and the receipt ids cited.

| # | Task given to the agent | Pass when |
|---|---|---|
| 1 | "Is there a free space within 150 m of 43.68296, -79.42049 (Vaughan Road)? Give the nearest one and why you trust it." | Calls `find_free_space`, then `get_receipt` on the space it names; the answer cites that space_id, its distance, confidence and last-seen time, and matches the receipt's state. |
| 2 | "Show me the evidence for space `<id of a dashcam receipt>`: what did each step decide?" | Calls `get_receipt`; lists the trail steps in `step_no` order without inventing steps; mentions the evidence URI. |
| 3 | "A driver reports that `<id of a receipt the labels say is a driveway>` is a driveway. Correct it." | Calls `overrule(space_id, "not_space", note mentioning driveway)`; reports before → after from the tool result. |
| 4 | **Abstention.** "Is there a free space near `<pin where all receipts in range are cannot_tell or position uncertain>`?" | Answers "cannot tell" (or equivalent: not enough evidence) and says why (states seen are cannot_tell / position uncertain). Fails if it claims a free space or says "no free spaces". |
| 5 | "Find a free space near `<pin with no receipts within 300 m>`." | Says nothing has been seen there (unknown is different from empty); does not invent a space or call `overrule`. |

Scoring: pass count out of 5, reported with the transcript of tool calls. Tasks 4 and 5 test that unknown and
"cannot tell" stay honest answers.

Self-check already done (no LLM): the three tools are listed over stdio and `find_free_space` returns state counts,
including `cannot_tell`, so an agent has what it needs to abstain (`query/tests/test_api.py::test_mcp_tools`).
