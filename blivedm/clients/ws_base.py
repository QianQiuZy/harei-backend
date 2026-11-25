# -*- coding: utf-8 -*-
import asyncio
import enum
import json
import logging
import struct
import zlib
from typing import *

import aiohttp
import brotli

from .. import handlers, utils

logger = logging.getLogger('blivedm')

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36'
)

HEADER_STRUCT = struct.Struct('>I2H2I')
MAX_DECODED_BYTES = 16 * 1024 * 1024
MAX_COMPRESSION_DEPTH = 2


class HeaderTuple(NamedTuple):
    pack_len: int
    raw_header_size: int
    ver: int
    operation: int
    seq_id: int


# WS_BODY_PROTOCOL_VERSION
class ProtoVer(enum.IntEnum):
    NORMAL = 0
    HEARTBEAT = 1
    DEFLATE = 2
    BROTLI = 3


# go-common\app\service\main\broadcast\model\operation.go
class Operation(enum.IntEnum):
    HANDSHAKE = 0
    HANDSHAKE_REPLY = 1
    HEARTBEAT = 2
    HEARTBEAT_REPLY = 3
    SEND_MSG = 4
    SEND_MSG_REPLY = 5
    DISCONNECT_REPLY = 6
    AUTH = 7
    AUTH_REPLY = 8
    RAW = 9
    PROTO_READY = 10
    PROTO_FINISH = 11
    CHANGE_ROOM = 12
    CHANGE_ROOM_REPLY = 13
    REGISTER = 14
    REGISTER_REPLY = 15
    UNREGISTER = 16
    UNREGISTER_REPLY = 17
    # B站业务自定义OP
    # MinBusinessOp = 1000
    # MaxBusinessOp = 10000


# WS_AUTH
class AuthReplyCode(enum.IntEnum):
    OK = 0
    TOKEN_ERROR = -101


class InitError(Exception):
    """初始化失败"""


class AuthError(Exception):
    """认证失败"""


DEFAULT_RECONNECT_POLICY = utils.make_constant_retry_policy(1)


def _decode_batch(version: int, data: bytes, budget: int) -> bytes:
    if budget <= 0:
        raise ValueError('WebSocket message exceeds decoding budget')
    if version == ProtoVer.BROTLI:
        decoder = brotli.Decompressor()
        result = decoder.process(data, budget + 1)
        complete = decoder.is_finished()
    else:
        decoder = zlib.decompressobj()
        result = decoder.decompress(data, budget + 1)
        complete = decoder.eof
    if len(result) > budget or not complete:
        raise ValueError('Invalid or oversized compressed WebSocket message')
    return result


class WebSocketClientBase:
    """
    基于WebSocket的客户端

    :param session: cookie、连接池
    :param heartbeat_interval: 发送心跳包的间隔时间（秒）
    """

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        heartbeat_interval: float = 30,
    ):
        if session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
            self._own_session = True
        else:
            self._session = session
            self._own_session = False
            assert self._session.loop is asyncio.get_event_loop()  # noqa

        self._heartbeat_interval = heartbeat_interval

        self._need_init_room = True
        self._handler: Optional[handlers.HandlerInterface] = None
        """消息处理器"""
        self._get_reconnect_interval: Callable[[int, int], float] = DEFAULT_RECONNECT_POLICY
        """重连间隔时间增长策略"""

        # 在调用init_room后初始化的字段
        self._room_id: Optional[int] = None

        # 在运行时初始化的字段
        self._websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        """WebSocket连接"""
        self._network_future: Optional[asyncio.Future] = None
        """网络协程的future"""
        self._heartbeat_timer_handle: Optional[asyncio.TimerHandle] = None
        """发心跳包定时器的handle"""

    @property
    def is_running(self) -> bool:
        """
        本客户端正在运行，注意调用stop后还没完全停止也算正在运行
        """
        return self._network_future is not None

    @property
    def room_id(self) -> Optional[int]:
        """
        房间ID，调用init_room后初始化
        """
        return self._room_id

    def set_handler(self, handler: Optional['handlers.HandlerInterface']):
        """
        设置消息处理器

        注意消息处理器和网络协程运行在同一个协程，如果处理消息耗时太长会阻塞接收消息。如果是CPU密集型的任务，建议将消息推到线程池处理；
        如果是IO密集型的任务，应该使用async函数，并且在handler里使用create_task创建新的协程

        :param handler: 消息处理器
        """
        self._handler = handler

    def set_reconnect_policy(self, get_reconnect_interval: Callable[[int, int], float]):
        """
        设置重连间隔时间增长策略

        :param get_reconnect_interval: 一个可调用对象，输入重试次数 (retry_count, total_retry_count)，返回间隔时间
        """
        self._get_reconnect_interval = get_reconnect_interval

    def start(self):
        """
        启动本客户端
        """
        if self.is_running:
            logger.warning('room=%s client is running, cannot start() again', self.room_id)
            return

        self._network_future = asyncio.create_task(self._network_coroutine_wrapper())

    def stop(self):
        """
        停止本客户端
        """
        if not self.is_running:
            logger.warning('room=%s client is stopped, cannot stop() again', self.room_id)
            return

        self._network_future.cancel()

    async def stop_and_close(self):
        """
        便利函数，停止本客户端并释放本客户端的资源，调用后本客户端将不可用
        """
        if self.is_running:
            self.stop()
            await self.join()
        await self.close()

    async def join(self):
        """
        等待本客户端停止
        """
        if not self.is_running:
            logger.warning('room=%s client is stopped, cannot join()', self.room_id)
            return

        await asyncio.shield(self._network_future)

    async def close(self):
        """
        释放本客户端的资源，调用后本客户端将不可用
        """
        if self.is_running:
            logger.warning('room=%s is calling close(), but client is running', self.room_id)

        # 如果session是自己创建的则关闭session
        if self._own_session:
            await self._session.close()

    async def init_room(self) -> bool:
        """
        初始化连接房间需要的字段

        :return: True代表没有降级，如果需要降级后还可用，重载这个函数返回True
        """
        raise NotImplementedError

    @staticmethod
    def _make_packet(data: Union[dict, str, bytes], operation: int) -> bytes:
        """
        创建一个要发送给服务器的包

        :param data: 包体JSON数据
        :param operation: 操作码，见Operation
        :return: 整个包的数据
        """
        if isinstance(data, dict):
            body = json.dumps(data).encode('utf-8')
        elif isinstance(data, str):
            body = data.encode('utf-8')
        else:
            body = data
        header = HEADER_STRUCT.pack(*HeaderTuple(
            pack_len=HEADER_STRUCT.size + len(body),
            raw_header_size=HEADER_STRUCT.size,
            ver=1,
            operation=operation,
            seq_id=1
        ))
        return header + body

    async def _network_coroutine_wrapper(self):
        """
        负责处理网络协程的异常，网络协程具体逻辑在_network_coroutine里
        """
        exc = None
        try:
            await self._network_coroutine()
        except asyncio.CancelledError:
            # 正常停止
            pass
        except Exception as e:
            logger.exception('room=%s _network_coroutine() finished with exception:', self.room_id)
            exc = e
        finally:
            logger.debug('room=%s _network_coroutine() finished', self.room_id)
            self._network_future = None

        if self._handler is not None:
            self._handler.on_client_stopped(self, exc)

    async def _network_coroutine(self):
        """
        网络协程，负责连接服务器、接收消息、解包
        """
        # retry_count在连接成功后会重置为0，total_retry_count不会
        retry_count = 0
        total_retry_count = 0
        while True:
            try:
                await self._on_before_ws_connect(retry_count)

                # 连接
                async with self._session.ws_connect(
                    self._get_ws_url(retry_count),
                    headers={'User-Agent': utils.USER_AGENT},  # web端的token也会签名UA
                    receive_timeout=self._heartbeat_interval + 5,
                ) as websocket:
                    self._websocket = websocket
                    await self._on_ws_connect()

                    # 处理消息
                    message: aiohttp.WSMessage
                    async for message in websocket:
                        await self._on_ws_message(message)
                        # 至少成功处理1条消息
                        retry_count = 0

            except InitError:
                logger.warning('room=%s initialization incomplete, waiting before retry', self.room_id)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # 掉线重连
                pass
            except AuthError:
                # 认证失败了，应该重新获取token再重连
                logger.exception('room=%d auth failed, trying init_room() again', self.room_id)
                self._need_init_room = True
            finally:
                self._websocket = None
                await self._on_ws_close()

            # 准备重连
            retry_count += 1
            total_retry_count += 1
            logger.warning(
                'room=%d is reconnecting, retry_count=%d, total_retry_count=%d',
                self.room_id, retry_count, total_retry_count
            )
            delay = self._get_reconnect_interval(retry_count, total_retry_count)
            if self._need_init_room:
                delay = max(delay, min(300, 5 * 2 ** min(retry_count - 1, 6)))
            await asyncio.sleep(delay)

    async def _on_before_ws_connect(self, retry_count):
        """
        在每次建立连接之前调用，可以用来初始化房间
        """
        if not self._need_init_room:
            return

        if not await self.init_room():
            raise InitError('init_room() failed')
        self._need_init_room = False

    def _get_ws_url(self, retry_count) -> str:
        """
        返回WebSocket连接的URL，可以在这里做故障转移和负载均衡
        """
        raise NotImplementedError

    async def _on_ws_connect(self):
        """
        WebSocket连接成功
        """
        await self._send_auth()
        self._heartbeat_timer_handle = asyncio.get_running_loop().call_later(
            self._heartbeat_interval, self._on_send_heartbeat
        )

    async def _on_ws_close(self):
        """
        WebSocket连接断开
        """
        if self._heartbeat_timer_handle is not None:
            self._heartbeat_timer_handle.cancel()
            self._heartbeat_timer_handle = None

    async def _send_auth(self):
        """
        发送认证包
        """
        raise NotImplementedError

    def _on_send_heartbeat(self):
        """
        定时发送心跳包的回调
        """
        if self._websocket is None or self._websocket.closed:
            self._heartbeat_timer_handle = None
            return

        self._heartbeat_timer_handle = asyncio.get_running_loop().call_later(
            self._heartbeat_interval, self._on_send_heartbeat
        )
        asyncio.create_task(self._send_heartbeat())

    async def _send_heartbeat(self):
        """
        发送心跳包
        """
        if self._websocket is None or self._websocket.closed:
            return

        try:
            await self._websocket.send_bytes(self._make_packet({}, Operation.HEARTBEAT))
        except (ConnectionResetError, aiohttp.ClientConnectionError) as e:
            logger.warning('room=%d _send_heartbeat() failed: %r', self.room_id, e)
        except Exception:  # noqa
            logger.exception('room=%d _send_heartbeat() failed:', self.room_id)

    async def _on_ws_message(self, message: aiohttp.WSMessage):
        """
        收到WebSocket消息

        :param message: WebSocket消息
        """
        if message.type != aiohttp.WSMsgType.BINARY:
            logger.warning('room=%d unknown websocket message type=%s', self.room_id, message.type)
            return

        try:
            await self._parse_ws_message(message.data)
        except AuthError:
            # 认证失败，让外层处理
            raise
        except Exception:  # noqa
            logger.exception('room=%d _parse_ws_message() error:', self.room_id)

    async def _parse_ws_message(self, data: bytes, budget: int = MAX_DECODED_BYTES, depth: int = 0) -> int:
        offset = 0
        while offset < len(data):
            if len(data) - offset < HEADER_STRUCT.size:
                raise ValueError('Incomplete WebSocket header')
            header = HeaderTuple(*HEADER_STRUCT.unpack_from(data, offset))
            if not HEADER_STRUCT.size <= header.raw_header_size <= header.pack_len <= len(data) - offset:
                raise ValueError('Invalid WebSocket packet length')
            start = offset + header.raw_header_size
            offset += header.pack_len
            if header.operation == Operation.HEARTBEAT_REPLY:
                # Some heartbeat replies omit echoed client data from their declared length.
                popularity = int.from_bytes(data[start:start + 4], 'big')
                self._handle_command({'cmd': '_HEARTBEAT', 'data': {'popularity': popularity}})
                break
            body = data[start:offset]
            if header.operation == Operation.AUTH_REPLY:
                code = json.loads(body)['code']
                if code != AuthReplyCode.OK:
                    raise AuthError(f'auth reply error, code={code}')
                await self._websocket.send_bytes(self._make_packet({}, Operation.HEARTBEAT))
            elif header.operation != Operation.SEND_MSG_REPLY:
                logger.warning('room=%d unknown message operation=%d', self.room_id, header.operation)
            elif header.ver in (ProtoVer.BROTLI, ProtoVer.DEFLATE):
                if depth >= MAX_COMPRESSION_DEPTH:
                    raise ValueError('Too many compressed WebSocket layers')
                decoded = await asyncio.to_thread(_decode_batch, header.ver, body, budget)
                budget = await self._parse_ws_message(decoded, budget - len(decoded), depth + 1)
            elif header.ver == ProtoVer.NORMAL:
                if body:
                    self._handle_command(json.loads(body))
            else:
                logger.warning('room=%d unknown protocol version=%d', self.room_id, header.ver)
        return budget

    def _handle_command(self, command: dict):
        """
        处理业务消息

        :param command: 业务消息
        """
        if self._handler is None:
            return
        try:
            # 为什么不做成异步的：
            # 1. 为了保持处理消息的顺序，这里不使用call_soon、create_task等方法延迟处理
            # 2. 如果支持handle使用async函数，用户可能会在里面处理耗时很长的异步操作，导致网络协程阻塞
            # 这里做成同步的，强制用户使用create_task或消息队列处理异步操作，这样就不会阻塞网络协程
            self._handler.handle(self, command)
        except Exception as e:
            logger.exception('room=%d _handle_command() failed', self.room_id, exc_info=e)
