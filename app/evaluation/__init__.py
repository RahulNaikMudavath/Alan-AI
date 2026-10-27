"""Retrieval evaluation engine.

Offline by default: the harness builds a temporary SQLite database, seeds it
through the same repositories and services the API uses, and calls
``SearchService`` directly. It never touches ``data/recallkit.db``.
"""
