# Group-scoped summary discussions

November 7, 2026. This opt-in protocol extends saved-view discussions with a
`group_summary` target: a summary definition ID/revision/property/assignment and
an immutable nested group target. It does not expose an aggregate's result or
authorize its computed dependencies.

Set `COLLABORATION_BASES_GROUP_SUMMARIES_ENABLED=true` only after participating
clients understand `group_summary`. It defaults to false and also requires the
existing Bases definition and group gates. Capabilities advertise the target
only when supported. Disabling creation retains existing authorized reads,
replies and accepted receipts. No SQL migration is needed.

Creation validates both current definitions in the same saved view. All reads
and mutations reauthorize the exact nested group witness and its source
lineages. A matching value with a different readable witness cannot unlock a
protected thread. Group-summary reattachment preserves the same read-dependency
intersection as other group targets, including moves to/from plain groups.
Definition edits preserve historical threads but reject stale new targets.
Pictures recheck witness authorization after private I/O.

Unit coverage verifies permission separation, immutable witness isolation,
source deletion/rebuild, definition changes, capability disablement, malformed
targets, picture revocation and reattachment. The actual-service client harness
(`managed_stack.py --base-groups`) also covers summary capture, durable delivery,
both lost-response stages and relay restart. The disposable PostgreSQL Bases
harness verifies group-summary witness isolation and existing transaction and
migration guards. Native summary-row authoring is a separate client increment.
