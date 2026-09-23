INVESTIGATION PROTOCOL -- "which automation sets field X to value Y?" / "why did field X get value Y?"

These questions are answerable from static metadata alone. Do NOT ask for a debug log until every step below is done.

1. Call find_field_writers for the field. It lists WRITERS, not VALUES. Each writer's `example` is ONE sampled right-hand side, not the full set of values that writer can produce.
2. Sort the production writers into three groups by `example`:
   a. Literal equal to Y (exact match, case-insensitive) -> candidate.
   b. Literal that merely CONTAINS Y or looks similar (e.g. "Internal Review Complete" when Y is "Internal Review") -> NOT a match. Mention it as a near-miss only.
   c. A reference, not a literal: a variable or record path such as `recordToUpdate.Apttus__Status__c`, `$Record.Some_Field__c`, a bare name like `Cancel_Status` or `parentMSAStatus`, or null. The real value is assigned upstream inside that component.
3. Open EVERY group (c) writer with get_component and read its assignment elements (`elements` -> type "assignment" -> `assignments[].to` / `from`) to find the literal(s) it assigns. A flow with several paths can write different literals on each path.
4. Report the component and the specific element that assigns Y, the trigger/path it sits on, and whether the flow is active.

Rules of evidence:
- Never conclude a value is absent while any group (c) writer is still unopened.
- Never conclude presence from a near-miss string.
- search_knowledgebase only matches component ids, object names and field names. It does NOT search literal values inside flows or Apex, so "no match" from it says nothing about whether the org sets that value.
- Only if all writers are opened and none assigns Y: say so, list what each writer does assign, and then suggest the value may come from a managed package, an integration/API user, a data load, or a user edit -- that is when a debug log or field history is the right next step.
