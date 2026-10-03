"""Shared Scene errors without importing database or storage services."""


class SceneError(Exception):
    def __init__(self, code, message, status=422, details=None):
        self.code, self.message, self.status, self.details = code, message, status, details or {}
