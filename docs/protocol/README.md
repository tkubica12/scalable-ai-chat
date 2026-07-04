# Protocol reference

The public protocol is documented in [Contracts](../Contracts.md).

Files in this folder are machine-checkable contract assets:

- `agent-ui-events.schema.json` - event envelope and event type profile.
- `examples/` - golden run event examples.
- `artifacts/a2ui-profile.schema.json` - approved declarative artifact profile.
- `artifacts/examples/` - golden artifact examples.

Validate them with:

```powershell
python scripts\validate_protocol_examples.py
python scripts\validate_artifact_examples.py
```
