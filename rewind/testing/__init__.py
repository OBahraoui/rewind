"""Test doubles shared by the engine and service test suites."""

from rewind.testing.fakes import FakeCloudRun, FakeGitLab

__all__ = ["FakeCloudRun", "FakeGitLab"]
