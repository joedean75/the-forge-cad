"""Provider failures keep their cause across planner and whole-tree edits."""
from app.services.op_errors import OperationError


class LLMFailure(OperationError):
    def __init__(self, code: str, message: str, remedy: str, *, status=None, retryable=False):
        super().__init__(message)
        self.code, self.remedy = code, remedy
        self.status, self.retryable = status, retryable

    def to_dict(self):
        return {**super().to_dict(), "provider_status": self.status, "retryable": self.retryable}
