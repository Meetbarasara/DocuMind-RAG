import sys
from types import ModuleType
from typing import Optional

try:
    from .logger import logging
except ImportError:
    from src.logger import logging


def error_message_detail(error, error_detail: ModuleType) -> str:
    """Build a detailed error message including file name and line number."""
    _, _, exc_tb = error_detail.exc_info()
    file_name = exc_tb.tb_frame.f_code.co_filename
    return (
        f"Error occurred in python script: {file_name} "
        f"at line number: {exc_tb.tb_lineno} "
        f"error message: {str(error)}"
    )


class CustomException(Exception):
    """Application-level exception with enriched traceback info.

    Note: despite an earlier review flagging this feature as "effectively
    inert," it does fire correctly in this codebase's actual usage —
    every real call site raises CustomException from inside an
    ``except:`` block, where ``sys.exc_info()`` is still populated. The
    type hint below was simply wrong (it used the ``sys`` *module* itself
    as a type annotation); fixed to reflect what's actually expected: a
    module exposing ``exc_info()`` (i.e. ``sys``, or a stand-in for
    testing), or ``None``.
    """

    def __init__(self, error_message, error_detail: Optional[ModuleType] = None):
        super().__init__(error_message)
        # Use provided sys module or fall back to the imported one
        _sys = error_detail if error_detail is not None else sys
        try:
            self.error_message = error_message_detail(error_message, _sys)
        except (TypeError, AttributeError):
            # If no active traceback exists, just use the plain message
            self.error_message = str(error_message)

    def __str__(self):
        return self.error_message


if __name__ == "__main__":
    try:
        a = 1 / 0
    except Exception as e:
        logging.info("Division by zero error occurred")
        raise CustomException(e, sys)