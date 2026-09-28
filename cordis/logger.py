"""日志服务：``ctx.logger``。

用法::

    ctx.logger.info('加载完成，共 %d 个插件', count)   # 以当前 fiber 名记录
    log = ctx.logger('my-module')                      # 具名 logger
    log.error('请求失败', exc)                          # 统一 %-格式化

结构化日志会同时进入内存缓冲区（``buffer``）与所有已注册的 exporter，
控制台 exporter 默认只在启动器里安装。
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Any, Callable, Iterable

from .utils import CallerBound, DisposableList

__all__ = ['Logger', 'LoggerService', 'LoggerLevel', 'Message', 'Exporter', 'ConsoleExporter']


class LoggerLevel:
    ERROR = 0
    INFO = 1
    WARN = 2
    DEBUG = 3


class Message:
    """一条结构化日志记录。"""

    __slots__ = ('sn', 'ts', 'name', 'type', 'level', 'args')

    def __init__(self, sn: int, ts: float, name: str, type: str, level: int, args: tuple) -> None:
        self.sn = sn
        self.ts = ts
        self.name = name
        self.type = type
        self.level = level
        self.args = args


_FORMATTERS: dict[str, Callable[[Any], str]] = {
    's': lambda value: str(value),
    'd': lambda value: str(int(value)),
    'i': lambda value: str(int(value)),
    'f': lambda value: repr(float(value)),
    'o': lambda value: repr(value),
    'j': lambda value: repr(value),
}


def _format_args(args: tuple) -> str:
    if not args:
        return ''
    args = list(args)
    first = args[0]
    if isinstance(first, BaseException):
        text = ''.join(traceback.format_exception(type(first), first, first.__traceback__)).rstrip()
        args[0] = text
        args.insert(0, '%s')
    elif not isinstance(first, str):
        args.insert(0, '%o')

    format_string: str = args.pop(0)
    out: list[str] = []
    index = 0
    while index < len(format_string):
        char = format_string[index]
        if char == '%' and index + 1 < len(format_string):
            code = format_string[index + 1]
            index += 2
            if code == '%':
                out.append('%')
                continue
            formatter = _FORMATTERS.get(code)
            if formatter is not None and args:
                out.append(formatter(args.pop(0)))
            else:
                out.append('%' + code)
            continue
        out.append(char)
        index += 1
    for arg in args:
        out.append(' ' + (repr(arg) if isinstance(arg, (dict, list, tuple, set)) else str(arg)))
    return ''.join(out)


class Logger:
    """某个名字下的日志门面（``ctx.logger('name')`` 的返回值）。"""

    def __init__(self, name: str, service: 'LoggerService') -> None:
        self.name = name
        self._service = service

    def _log(self, type: str, level: int, args: tuple) -> None:
        self._service._emit(self.name, type, level, args)

    def error(self, format: Any = '', *args: Any) -> None:
        self._log('error', LoggerLevel.ERROR, (format, *args))

    def info(self, format: Any = '', *args: Any) -> None:
        self._log('info', LoggerLevel.INFO, (format, *args))

    def warn(self, format: Any = '', *args: Any) -> None:
        self._log('warn', LoggerLevel.WARN, (format, *args))

    def debug(self, format: Any = '', *args: Any) -> None:
        self._log('debug', LoggerLevel.DEBUG, (format, *args))


class Exporter:
    """日志出口：``export(message)`` 接收每一条通过过滤的记录。"""

    def __init__(
        self,
        export: Callable[[Message], None],
        colors: int = 0,
        levels: dict[str, int] | None = None,
        max_length: int = 10240,
    ) -> None:
        self.export = export
        self.colors = colors
        self.levels = levels or {}
        self.max_length = max_length


_LEVEL_STYLE = {
    'error': ('\x1b[31m', 'ERROR'),
    'warn': ('\x1b[33m', 'WARN '),
    'info': ('\x1b[32m', 'INFO '),
    'debug': ('\x1b[90m', 'DEBUG'),
}

_PALETTE = [36, 32, 33, 34, 35, 31]


def log_level_from_env(default: int = LoggerLevel.INFO) -> int:
    """读取 ``CORDIS_LOG_LEVEL``（error/warn/info/debug）决定默认日志级别。"""
    import os
    name = os.environ.get('CORDIS_LOG_LEVEL', '').lower().strip()
    mapping = {
        'error': LoggerLevel.ERROR,
        'warn': LoggerLevel.WARN,
        'warning': LoggerLevel.WARN,
        'info': LoggerLevel.INFO,
        'debug': LoggerLevel.DEBUG,
    }
    return mapping.get(name, default)


class ConsoleExporter(Exporter):
    """把日志打印到标准错误的默认 exporter。"""

    def __init__(self, levels: dict[str, int] | None = None) -> None:
        super().__init__(self._export, colors=1, levels=levels or {'default': LoggerLevel.INFO})
        self._stream = sys.stderr

    def _export(self, message: Message) -> None:
        text = _format_args(message.args)
        if self.colors:
            color = _PALETTE[hash(message.name) % len(_PALETTE)]
            mark, label = _LEVEL_STYLE.get(message.type, ('', message.type.upper()))
            stamp = time.strftime('%H:%M:%S', time.localtime(message.ts))
            line = f'\x1b[90m{stamp}\x1b[0m {mark}{label}\x1b[0m \x1b[{color}m{message.name}\x1b[0m {text}'
        else:
            stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(message.ts))
            line = f'{stamp} [{message.type}] {message.name}: {text}'
        print(line, file=self._stream)


class LoggerService(CallerBound):
    """``ctx.logger``：既是 logger 工厂（``ctx.logger('name')``），

    也可以直接调用（``ctx.logger.info(...)`` 使用当前 fiber 的名字）。
    """

    def __init__(self, ctx: Any) -> None:
        self._service_ctx = ctx
        self.buffer: list[Message] = []
        self.buffer_size = 1000
        self.exporters = DisposableList[Exporter]()
        self._sn_message = 0

    # ------------------------------------------------------------------ 出口
    def exporter(self, exporter: Exporter) -> Callable[[], Any]:
        """注册日志出口，随当前 fiber 卸载自动移除。"""

        def install():
            remove = self.exporters.push(exporter)
            return remove

        return self.ctx.effect(install, 'ctx.logger.exporter()')

    # ------------------------------------------------------------------ 记录
    def _emit(self, name: str, type: str, level: int, args: tuple) -> None:
        self._sn_message += 1
        message = Message(self._sn_message, time.time(), name, type, level, args)
        for exporter in list(self.exporters):
            threshold = exporter.levels.get(name, exporter.levels.get('default', LoggerLevel.INFO))
            if threshold < level:
                continue
            try:
                exporter.export(message)
            except Exception:  # pragma: no cover - 出口自身异常不应影响业务
                traceback.print_exc()
        self.buffer.append(message)
        if len(self.buffer) > self.buffer_size:
            del self.buffer[:len(self.buffer) - self.buffer_size]

    def _default_name(self) -> str:
        fiber = getattr(self.ctx, 'fiber', None)
        return getattr(fiber, 'name', 'root') or 'root'

    def __call__(self, name: str | None = None) -> Logger:
        return Logger(name or self._default_name(), self)

    def error(self, format: Any = '', *args: Any) -> None:
        self._emit(self._default_name(), 'error', LoggerLevel.ERROR, (format, *args))

    def info(self, format: Any = '', *args: Any) -> None:
        self._emit(self._default_name(), 'info', LoggerLevel.INFO, (format, *args))

    def warn(self, format: Any = '', *args: Any) -> None:
        self._emit(self._default_name(), 'warn', LoggerLevel.WARN, (format, *args))

    def debug(self, format: Any = '', *args: Any) -> None:
        self._emit(self._default_name(), 'debug', LoggerLevel.DEBUG, (format, *args))
