"""Shared test plumbing. Not a test module: `test*.py` discovery skips it."""
import tempfile
from pathlib import Path
from unittest import mock

from agentgrid import chat


def isolate_chat_queue(case):
    """Point the chat queue's files at a temp dir for one test case.

    A ChatSession writes its queue through to ~/.agentgrid/chat-queue so a
    restart cannot lose a waiting message. Any test that makes one would write
    there too, in the real home, where the next real server would read it back.
    """
    folder = tempfile.TemporaryDirectory()
    case.addCleanup(folder.cleanup)
    patch = mock.patch.object(chat, "QUEUE_DIR", Path(folder.name))
    patch.start()
    case.addCleanup(patch.stop)
    return Path(folder.name)
