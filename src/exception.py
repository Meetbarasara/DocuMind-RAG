import sys

try:
    from .logger import logging
except ImportError:
    from src.logger import logging


def error_message_detail(error, error_detail: sys) -> str:
    """Build a detailed error message including file name and line number."""
    _, _, exc_tb = error_detail.exc_info()
    file_name = exc_tb.tb_frame.f_code.co_filename
    return (
        f"Error occurred in python script: {file_name} "
        f"at line number: {exc_tb.tb_lineno} "
        f"error message: {str(error)}"
    )


class CustomException(Exception):
    """Application-level exception with enriched traceback info."""

    def __init__(self, error_message, error_detail: sys):
        super().__init__(error_message)
        self.error_message = error_message_detail(error_message, error_detail)

    def __str__(self):
        return self.error_message


if __name__ == "__main__":
    try:
        a = 1 / 0
    except Exception as e:
        logging.info("Division by zero error occurred")
        raise CustomException(e, sys)