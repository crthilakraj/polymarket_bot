import logging

from logging_config import configure_logging


def test_configure_logging_sets_root_handler():
    configure_logging()

    assert logging.getLogger().handlers
