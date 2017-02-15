#!/usr/bin/env python
# -*- coding:Utf-8 -*-

from enum import IntEnum


class WebAccountResult(IntEnum):
    Timeout = 1
    UnknownException = 2
    ResponseNotSerializable = 3
    Failed = 4
