"""Windows dev helper: a selector-loop factory for uvicorn.

psycopg's async mode needs a selector event loop, but uvicorn 0.36+ forces
the proactor loop on Windows. Use this module as uvicorn's loop factory:

    uvicorn wellisearch.app:app --loop wellisearch.loopfix:loop_factory

On Linux this is a no-op (the selector loop is already the default).
"""
from __future__ import annotations

import asyncio
import sys


def loop_factory(use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """uvicorn loop factory: the selector loop (psycopg async requires it),
    except subprocesses on Windows, which need the proactor loop."""
    # uvicorn passes a custom loop factory through untouched and asyncio.Runner
    # calls it with no args, expecting a loop INSTANCE back.
    # the proactor loop is the only one that supports subprocesses on Windows;
    # everything else runs fine on the selector loop (and psycopg requires it).
    if sys.platform == "win32":
        if use_subprocess:
            return asyncio.ProactorEventLoop()
        # the concrete WindowsSelectorEventLoop is private in 3.12+; the
        # public policy hands out an instance
        policy = asyncio.WindowsSelectorEventLoopPolicy()
        return policy.new_event_loop()
    return asyncio.SelectorEventLoop()
