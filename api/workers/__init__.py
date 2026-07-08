"""arq workers — the execution arm of the Console backend (Track 2, DESIGN §11).

A separate ``worker`` process consumes the jobs queue and runs the §5 build
pipeline; the API only enqueues. See ``build_worker``.
"""
