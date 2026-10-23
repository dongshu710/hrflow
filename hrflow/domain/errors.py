"""统一业务异常。所有服务层抛出的可预期错误都带 code，便于 API 层直接映射成响应。"""


class BusinessError(Exception):
    def __init__(self, message: str, code: str = "business_error", details: list = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or []

    def to_dict(self) -> dict:
        return {"error": self.message, "code": self.code, "details": self.details}
