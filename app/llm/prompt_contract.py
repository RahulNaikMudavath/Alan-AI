"""Wire format shared by the extraction prompt builder and offline providers.

The prompt carries the conversation on a single marker line so a deterministic
fake provider can read it back without reimplementing prompt parsing. Only the
marker is shared; the full prompt text belongs to the extraction service.
"""

MESSAGES_MARKER = "MESSAGES_JSON:"
