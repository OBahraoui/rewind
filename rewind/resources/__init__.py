"""Registry of resource handlers, in snapshot order."""

from rewind.resources.base import Context, Handler
from rewind.resources.config import CloudRunHandler, FeatureFlagHandler, VariableHandler
from rewind.resources.repository import BranchHandler, RepositoryHandler, TagHandler
from rewind.resources.work_items import IssueHandler, LabelHandler, MergeRequestHandler, ReleaseHandler

HANDLERS: dict[str, Handler] = {
    h.kind: h
    for h in (
        RepositoryHandler(),
        BranchHandler(),
        TagHandler(),
        ReleaseHandler(),
        MergeRequestHandler(),
        IssueHandler(),
        LabelHandler(),
        FeatureFlagHandler(),
        VariableHandler(),
        CloudRunHandler(),
    )
}

__all__ = ["HANDLERS", "Context", "Handler"]
