from services.streaming.core.config import env_get
import os
import json
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Dict, Optional
import redis.asyncio as redis
from services.otel_setup import current_trace_id, span

log = logging.getLogger("orch.event_bus")

class EventBus:
    """
    Elite Event Bus (Nível 5)
    ------------------------
    Unifies Pub/Sub for real-time reactivity and Redis Streams for 
    reliable persistence, replay, and consumer group orchestration.
    """
    _instance: Optional['EventBus'] = None
    _lock = asyncio.Lock()
    _connected = False

    @classmethod
    async def get_instance(cls, redis_url: str = None) -> 'EventBus':
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls(redis_url)
            return cls._instance

    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or env_get("REDIS_URL", default="redis://localhost:6379")
        self.redis: Optional[redis.Redis] = None

    @staticmethod
    def _stream_name(channel_or_stream: str) -> str:
        if channel_or_stream.startswith("sinc:stream:"):
            return channel_or_stream
        if channel_or_stream.startswith("stream:"):
            return f"sinc:{channel_or_stream}"
        return f"sinc:stream:{channel_or_stream}"

    @staticmethod
    def _pubsub_name(channel: str) -> str:
        if channel.startswith("sinc:pubsub:"):
            return channel
        return f"sinc:pubsub:{channel}"

    async def connect(self):
        async with self._lock:
            if self._connected and self.redis:
                return
            try:
                self.redis = redis.from_url(self.redis_url, decode_responses=True)
                await self.redis.ping()
                self._connected = True
                log.info(f"Elite EventBus connected to Redis at {self.redis_url}")
            except Exception as e:
                log.error(f"Elite EventBus connection failed: {e}")
                self._connected = False
                raise

    async def emit(self, channel: str, message: Dict[str, Any], stream: bool = True):
        """
        Emit an event. Use stream=True for critical tasks requiring reliability.
        """
        if not self.redis: await self.connect()
        trace_id = message.get("trace_id") or current_trace_id() or uuid.uuid4().hex[:12]
        payload = json.dumps({
            **message,
            "trace_id": trace_id,
            "emitted_at": message.get("emitted_at") or datetime.now(timezone.utc).isoformat()
        })

        with span("eventbus.emit", channel=channel, trace_id=trace_id, use_stream=stream, event_type=message.get("type", "unknown")):
            if stream:
                stream_name = self._stream_name(channel)
                # MAXLEN 5000 approximate keeps memory in check
                await self.redis.xadd(stream_name, {"data": payload}, maxlen=5000, approximate=True)

            # Always publish for real-time SSE and light listeners
            await self.redis.publish(self._pubsub_name(channel), payload)
        log.debug(f"Elite Event emitted [{channel}]: {message.get('type','unknown')}")

    async def publish(self, channel: str, message: Dict[str, Any], use_stream: bool = True):
        """Compatibility wrapper used by the new Python control plane."""
        await self.emit(channel, message, stream=use_stream)

    async def subscribe(self, channel: str, callback: Callable):
        """Simple Pub/Sub subscription wrapper."""
        if not self.redis: await self.connect()
        ps = self.redis.pubsub()
        await ps.subscribe(self._pubsub_name(channel))
        try:
            async for msg in ps.listen():
                if msg["type"] == "message":
                    data = json.loads(msg["data"])
                    if asyncio.iscoroutinefunction(callback): await callback(data)
                    else: callback(data)
        finally:
            await ps.unsubscribe(self._pubsub_name(channel))

    async def setup_consumer_group(self, stream_name: str, group_name: str):
        if not self.redis: await self.connect()
        stream_name = self._stream_name(stream_name)
        try:
            await self.redis.xgroup_create(stream_name, group_name, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e): raise

    async def create_consumer_group(self, stream_name: str, group_name: str):
        await self.setup_consumer_group(stream_name, group_name)

    async def read_group(
        self,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        *,
        count: int = 5,
        block_ms: int = 2000,
    ):
        if not self.redis:
            await self.connect()
        normalized_stream = self._stream_name(stream_name)
        await self.setup_consumer_group(normalized_stream, group_name)
        with span("eventbus.read_group", stream_name=normalized_stream, group_name=group_name, consumer_name=consumer_name, count=count, block_ms=block_ms):
            return await self.redis.xreadgroup(
                group_name,
                consumer_name,
                {normalized_stream: ">"},
                count=count,
                block=block_ms,
            )

    async def auto_claim(
        self,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        *,
        min_idle_time_ms: int,
        start_id: str = "0-0",
        count: int = 5,
    ):
        if not self.redis:
            await self.connect()
        normalized_stream = self._stream_name(stream_name)
        await self.setup_consumer_group(normalized_stream, group_name)
        return await self.redis.xautoclaim(
            normalized_stream,
            group_name,
            consumer_name,
            min_idle_time=min_idle_time_ms,
            start_id=start_id,
            count=count,
        )

    async def ack(self, stream_name: str, group_name: str, msg_id: str):
        if not self.redis:
            await self.connect()
        normalized_stream = self._stream_name(stream_name)
        with span("eventbus.ack", stream_name=normalized_stream, group_name=group_name, msg_id=msg_id):
            return await self.redis.xack(normalized_stream, group_name, msg_id)

    async def get_xstream_iterator(
        self,
        channel_or_stream: str,
        *,
        last_id: str = "$",
        block_ms: int = 15000,
        count: int = 50,
    ) -> AsyncGenerator[str, None]:
        if not self.redis:
            await self.connect()
        stream_name = self._stream_name(channel_or_stream)
        next_id = last_id or "$"
        while True:
            events = await self.redis.xread({stream_name: next_id}, count=count, block=block_ms)
            if not events:
                continue
            for _, messages in events:
                for msg_id, data in messages:
                    next_id = msg_id
                    payload = data.get("data", "{}")
                    yield f"id: {msg_id}\ndata: {payload}\n\n"

    async def consume(self, stream_name: str, group_name: str, consumer_name: str, callback: Callable):
        """
        High-reliability consumer loop. 
        Supports ACKs and automatic recovery of 'stale' messages via XAUTOCLAIM.
        """
        if not self.redis: await self.connect()
        normalized_stream = self._stream_name(stream_name)
        await self.setup_consumer_group(normalized_stream, group_name)
        
        log.info(f"Consumer {consumer_name} started for {normalized_stream} (Group: {group_name})")
        
        while True:
            try:
                # 1. Claim stale messages from peers who crashed (30s idle)
                claimed = await self.redis.xautoclaim(normalized_stream, group_name, consumer_name, 
                                                     min_idle_time=30000, start_id="0-0", count=5)
                # claimed is (next_id, [messages], [deleted_ids])
                for msg_id, data in claimed[1]:
                    await self._handle_msg(normalized_stream, group_name, msg_id, data, callback)

                # 2. Read new messages
                streams = await self.redis.xreadgroup(group_name, consumer_name, {normalized_stream: ">"}, count=5, block=2000)
                if streams:
                    for _, messages in streams:
                        for msg_id, data in messages:
                            await self._handle_msg(normalized_stream, group_name, msg_id, data, callback)
                
                await asyncio.sleep(0.1)
            except Exception as e:
                log.error(f"Consumer error in {normalized_stream}: {e}")
                await asyncio.sleep(2)

    async def _handle_msg(self, stream, group, msg_id, data, callback):
        try:
            payload = json.loads(data["data"])
            if asyncio.iscoroutinefunction(callback): await callback(payload)
            else: callback(payload)
            # Confirm success
            await self.redis.xack(stream, group, msg_id)
        except Exception as e:
            log.error(f"Failed to process message {msg_id}: {e}")
            # Optional: Move to DLQ (Dead Letter Queue) after X retries

async def get_event_bus() -> EventBus:
    return await EventBus.get_instance()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    async def test():
        bus = await get_event_bus()
        await bus.emit("test", {"type": "ping", "val": 1})
        print("Emitted.")
    asyncio.run(test())
